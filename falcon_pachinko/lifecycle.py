"""Public WebSocket connection lifecycle and accepted-session handle."""

from __future__ import annotations

import asyncio
import collections.abc as cabc
import dataclasses as dc
import enum
import typing as typ
import warnings

import falcon

from ._disconnect import _PeerDisconnectedError
from ._observability import _observe_lifecycle
from ._session import _report_session_failure, _wait_for_shielded_task
from ._session_driver import SessionDriverContext, finalize_session, run_session
from ._session_socket import _accept_arguments

if typ.TYPE_CHECKING:
    from ._session import SessionState
    from .observability import LifecycleObserver
    from .protocols import WebSocketLike
    from .resource import WebSocketResource


type AcceptHeaders = cabc.Mapping[str, str] | cabc.Iterable[tuple[str, str]]


async def _capture_error(operation: cabc.Awaitable[object]) -> BaseException | None:
    """Return an operation failure so cleanup can continue before raising it."""
    try:
        await operation
    except asyncio.CancelledError as error:
        return error
    except Exception as error:  # ruff: ignore[blind-except] - adapter failures
        return error
    return None


class LifecycleError(RuntimeError):
    """Raised when a WebSocket operation is used in the wrong lifecycle phase."""


class ConnectionPhase(enum.StrEnum):
    """Observable phases of one routed WebSocket connection."""

    PRE_ACCEPT = "pre_accept"
    ACCEPTED = "accepted"
    CLOSED = "closed"


@dc.dataclass(frozen=True, slots=True)
class AcceptOptions:
    """Options returned by ``on_connect`` for the router's accept call."""

    subprotocol: str | None = None
    headers: AcceptHeaders | None = None

    def __bool__(self) -> typ.Literal[True]:
        """Return true because options always approve acceptance."""
        return True


class LifecycleGuard:
    """Enforce pre-accept, accepted, and closed operations on one transport."""

    __slots__ = (
        "_accepting",
        "_close_code",
        "_close_started",
        "_observer",
        "_phase",
        "_session",
        "_websocket",
    )

    def __init__(
        self,
        websocket: WebSocketLike,
        session: SessionState,
        *,
        observer: LifecycleObserver | None = None,
    ) -> None:
        """Guard one transport and retain its optional lifecycle observer.

        Parameters
        ----------
        websocket : WebSocketLike
            Transport whose acceptance and data operations are guarded.
        session : SessionState
            Shared exactly-once disconnect state for this connection.
        observer : LifecycleObserver or None, default=None
            Optional synchronous host metrics and tracing adapter.
        """
        self._websocket = websocket
        self._observer = observer
        self._session = session
        self._phase = ConnectionPhase.PRE_ACCEPT
        self._accepting = False
        self._close_code: int | None = None
        self._close_started = False

        if bool(getattr(websocket, "closed", False)):
            self._phase = ConnectionPhase.CLOSED
        elif getattr(websocket, "unaccepted", True) is False:
            self._phase = ConnectionPhase.ACCEPTED
            self._session.establish()

    @property
    def phase(self) -> ConnectionPhase:
        """Current connection phase; callers cannot modify it directly."""
        if self._session.peer_disconnected:
            return ConnectionPhase.CLOSED
        return self._phase

    @property
    def close_code(self) -> int | None:
        """First local or peer close code observed by this guard."""
        return self._close_code

    @property
    def closed(self) -> bool:
        """Whether the lifecycle or its underlying transport is closed."""
        return self.phase is ConnectionPhase.CLOSED or bool(
            getattr(self._websocket, "closed", False)
        )

    def __getattr__(self, name: str) -> object:
        """Forward transport extensions while guarded methods stay explicit."""
        return getattr(self._websocket, name)

    async def accept(
        self,
        subprotocol: str | None = None,
        headers: AcceptHeaders | None = None,
    ) -> None:
        """Keep the deprecated resource-owned acceptance path for migration."""
        warnings.warn(
            "WebSocket acceptance is router-owned; return AcceptOptions from "
            "on_connect instead",
            DeprecationWarning,
            stacklevel=2,
        )
        await self._accept(AcceptOptions(subprotocol, headers))

    async def _accept(self, options: AcceptOptions | None = None) -> None:
        """Accept once, recording the transition even for a partial transport error."""
        if self._phase is not ConnectionPhase.PRE_ACCEPT:
            msg = f"cannot accept a WebSocket in phase {self._phase.value!r}"
            raise LifecycleError(msg)
        if self._accepting:
            msg = "cannot accept a WebSocket while acceptance is already in progress"
            raise LifecycleError(msg)

        # Set this before the transport await: concurrent callers must not both
        # pass the phase check while the first handshake is still in flight.
        self._accepting = True
        try:
            with _observe_lifecycle(
                self._observer, "acceptance", failure_category="transport"
            ) as observation:
                try:
                    await self._accept_transport(options)
                finally:
                    observation.set_close_code(self._session.close_code)
        finally:
            self._accepting = False

    async def _accept_transport(self, options: AcceptOptions | None) -> None:
        """Complete transport acceptance and preserve partial transitions."""
        kwargs = _accept_arguments(
            options.subprotocol if options is not None else None,
            options.headers if options is not None else None,
        )
        accept = typ.cast(
            "cabc.Callable[..., cabc.Awaitable[None]]", self._websocket.accept
        )
        try:
            await accept(**kwargs)
        except (Exception, asyncio.CancelledError):
            self._record_partial_acceptance()
            raise
        if getattr(self._websocket, "unaccepted", None) is True:
            msg = "WebSocket transport returned from accept() without accepting"
            raise LifecycleError(msg)
        self._mark_accepted()

    def _record_partial_acceptance(self) -> None:
        """Record a transport that accepted before failing or cancellation."""
        if getattr(self._websocket, "unaccepted", True) is False:
            self._mark_accepted()

    async def close(self, code: int | None = 1000, reason: str | None = None) -> None:
        """Reject before acceptance or close an accepted connection once."""
        if self._close_started or bool(getattr(self._websocket, "closed", False)):
            return

        effective_code = 1000 if code is None else code
        close = typ.cast(
            "cabc.Callable[..., cabc.Awaitable[None]]", self._websocket.close
        )
        kwargs: dict[str, object] = {"code": effective_code}
        if reason is not None:
            kwargs["reason"] = reason
        self._close_started = True
        try:
            await close(**kwargs)
        except (Exception, asyncio.CancelledError):
            if bool(getattr(self._websocket, "closed", False)):
                self._mark_closed(effective_code)
            else:
                self._close_started = False
            raise
        self._mark_closed(effective_code)

    async def send_media(self, media: object, *args: object, **kwargs: object) -> None:
        """Send structured media after acceptance."""
        self._require_accepted("send_media")
        await self._run_transport("send_media", media, *args, **kwargs)

    async def send_text(self, message: str) -> None:
        """Send a text frame after acceptance when the transport supports it."""
        self._require_accepted("send_text")
        await self._send_with_media_fallback("send_text", message)

    async def send_bytes(self, payload: bytes) -> None:
        """Send a binary frame after acceptance when the transport supports it."""
        self._require_accepted("send_bytes")
        if (method := self._transport_method("send_bytes", "send_data")) is not None:
            await typ.cast("cabc.Callable[[bytes], cabc.Awaitable[None]]", method)(
                payload
            )
        else:
            await self.send_media(payload)

    async def send_json(self, payload: object) -> None:
        """Send JSON after acceptance when the transport supports it."""
        self._require_accepted("send_json")
        await self._send_with_media_fallback("send_json", payload)

    async def _send_with_media_fallback(
        self, method_name: str, payload: object
    ) -> None:
        """Use a typed transport method when available, else send media."""
        if (method := self._transport_method(method_name)) is not None:
            await typ.cast("cabc.Callable[[object], cabc.Awaitable[None]]", method)(
                payload
            )
        else:
            await self.send_media(payload)

    def _transport_method(self, *names: str) -> object | None:
        """Return the first callable transport method in preference order."""
        for name in names:
            if callable(method := getattr(self._websocket, name, None)):
                return method
        return None

    async def receive_media(self) -> object:
        """Receive structured media after acceptance."""
        self._require_accepted("receive_media")
        return await self._receive_with_disconnect_recording(
            self._run_transport("receive_media")
        )

    async def _receive_payload(self) -> object:
        """Read one raw frame for router dispatch without changing media APIs."""
        self._require_accepted("receive_media")
        if (receive_payload := self._transport_method("receive_payload")) is not None:
            operation = typ.cast(
                "cabc.Callable[[], cabc.Awaitable[object]]", receive_payload
            )()
        else:
            operation = self._run_transport("receive_media")
        return await self._receive_with_disconnect_recording(operation)

    async def receive_text(self) -> str:
        """Receive a text frame after acceptance."""
        return await self._receive_typed_frame(
            "receive_text", ("receive_text",), str, "text"
        )

    async def receive_bytes(self) -> bytes:
        """Receive a binary frame after acceptance when the transport supports it."""
        return await self._receive_typed_frame(
            "receive_bytes", ("receive_bytes", "receive_data"), bytes, "binary"
        )

    async def _receive_typed_frame[FrameT: str | bytes](
        self,
        operation_name: str,
        method_names: tuple[str, ...],
        expected_type: type[FrameT],
        frame_name: str,
    ) -> FrameT:
        """Receive and validate one typed frame with media fallback."""
        self._require_accepted(operation_name)
        if (method := self._transport_method(*method_names)) is not None:
            operation = typ.cast("cabc.Callable[[], cabc.Awaitable[object]]", method)()
        else:
            operation = self._run_transport("receive_media")
        message = await self._receive_with_disconnect_recording(operation)
        if not isinstance(message, expected_type):
            msg = f"expected a {frame_name} WebSocket frame"
            raise TypeError(msg)
        return message

    async def receive_json(self, payload_type: type[object] | None = None) -> object:
        """Receive JSON after acceptance when the transport supports it."""
        self._require_accepted("receive_json")
        if callable(method := getattr(self._websocket, "receive_json", None)):
            operation = typ.cast("cabc.Callable[..., cabc.Awaitable[object]]", method)(
                payload_type
            )
            return await self._receive_with_disconnect_recording(operation)
        return await self.receive_media()

    async def _receive_with_disconnect_recording(
        self, operation: cabc.Awaitable[object]
    ) -> object:
        """Record the peer close state when a receive operation disconnects."""
        try:
            return await operation
        except (falcon.WebSocketDisconnected, _PeerDisconnectedError) as exc:
            self._record_disconnect(exc)
            raise

    async def _run_transport(
        self, name: str, *args: object, **kwargs: object
    ) -> object:
        """Invoke one forwarded async transport method."""
        method = typ.cast(
            "cabc.Callable[..., cabc.Awaitable[object]]", getattr(self._websocket, name)
        )
        return await method(*args, **kwargs)

    def _require_accepted(self, operation: str) -> None:
        """Reject data operations before setup has accepted the connection."""
        if self.phase is ConnectionPhase.ACCEPTED:
            return
        if self.phase is ConnectionPhase.PRE_ACCEPT:
            msg = (
                f"{operation}() is unavailable before WebSocket acceptance; "
                "move active-session work to on_session_start()"
            )
        else:
            msg = f"{operation}() is unavailable after the WebSocket is closed"
        raise LifecycleError(msg)

    def _mark_accepted(self) -> None:
        """Record the one transition into an accepted session."""
        self._phase = ConnectionPhase.ACCEPTED
        self._session.establish()

    def _mark_closed(self, code: int | None) -> None:
        """Record closure and preserve the first close code."""
        self._close_started = True
        self._record_close_code_and_phase(code)

    def _record_close_code_and_phase(self, code: int | None) -> None:
        """Keep the first observed close code and mark the lifecycle closed."""
        if self._close_code is None:
            self._close_code = code
        if self._session.close_code is None:
            self._session.close_code = self._close_code
        self._phase = ConnectionPhase.CLOSED

    def _record_disconnect(self, error: BaseException) -> None:
        """Record the peer code when the receive API reports a disconnect."""
        self._session.peer_disconnected = True
        code = getattr(error, "code", None)
        code = code if isinstance(code, int) else None
        self._record_close_code_and_phase(code)


class WebSocketSession:
    """Handle an accepted server-side connection through finalization.

    This router session is distinct from the client facade returned by
    ``WebSocketTestClient.connect()``.
    """

    def __init__(self, context: SessionDriverContext) -> None:
        """Retain the accepted connection and its one-run finalization state.

        Parameters
        ----------
        context : SessionDriverContext
            Selected resources, transport, hooks, state, and optional observer.
        """
        self._resource = context.resource
        self._resources = tuple(context.resources)
        self._websocket: LifecycleGuard = context.websocket
        self._hook_manager = context.hook_manager
        self._dispatch = context.dispatch
        self._session_state = context.session_state
        self._params = dict(context.params)
        self._observer = context.observer
        self._run_task: asyncio.Task[object] | None = None
        self._run_completion: asyncio.Future[BaseException | None] | None = None
        self._finalization_task: asyncio.Task[None] | None = None

    @property
    def resource(self) -> WebSocketResource:
        """Innermost resource selected for this connection."""
        return self._resource

    @property
    def ws(self) -> LifecycleGuard:
        """Guarded socket for this accepted session."""
        return self._websocket

    async def run(self) -> None:
        """Run post-accept setup, receive dispatch, and disconnect cleanup.

        Raises
        ------
        LifecycleError
            If the connection is not accepted or this session was already run.
        """
        if self._websocket.phase is not ConnectionPhase.ACCEPTED:
            msg = "WebSocketSession.run() requires an accepted connection"
            raise LifecycleError(msg)
        if self._run_task is not None:
            msg = "WebSocketSession.run() may only be called once"
            raise LifecycleError(msg)

        self._run_task = typ.cast("asyncio.Task[object]", asyncio.current_task())
        run_completion: asyncio.Future[BaseException | None] = (
            asyncio.get_running_loop().create_future()
        )
        self._run_completion = run_completion
        context = self._driver_context()
        run_error: BaseException | None = None
        with _observe_lifecycle(
            self._observer, span_name="falcon_pachinko.websocket.session"
        ) as observation:
            try:
                await run_session(context, self._finalize)
            except BaseException as error:
                run_error = error
                raise
            finally:
                observation.set_close_code(self._session_state.close_code)
                if not run_completion.done():
                    run_completion.set_result(run_error)

    async def aclose(self, code: int = 1000) -> None:
        """Close and finalize the session once, including manual sessions.

        Parameters
        ----------
        code : int
            Close code sent when the transport remains open.

        Notes
        -----
        If ``run()`` is active in another task, this method closes the
        transport and waits for that task to finish without a timeout.
        Closing the socket does not interrupt unrelated work in
        ``on_session_start()``. To bound shutdown, cancel and await the run
        task before or concurrently with calling ``aclose()``. Cancellation
        while waiting for the run task propagates to the caller.
        """
        close_error = await _capture_error(self._websocket.close(code))
        run_error = await self._wait_for_run_completion()
        await self._finish_aclose(run_error, close_error)

    async def _wait_for_run_completion(self) -> BaseException | None:
        """Wait for another task's active run without cancelling that run."""
        completion = self._run_completion
        if completion is None or self._run_task is asyncio.current_task():
            return None
        return await asyncio.shield(completion)

    async def _finish_aclose(
        self,
        run_error: BaseException | None,
        close_error: BaseException | None,
    ) -> None:
        """Finalize after close and preserve the session error precedence."""
        try:
            await self._finalize(run_error, close_error)
        except (Exception, asyncio.CancelledError):
            if run_error is None:
                raise
        if run_error is not None:
            raise run_error
        if close_error is not None:
            raise close_error

    async def _finalize(
        self,
        error: BaseException | None,
        finalization_error: BaseException | None = None,
    ) -> None:
        """Share one cleanup task across the session loop and explicit close."""
        if self._finalization_task is None:
            context = self._driver_context(error, finalization_error)
            self._finalization_task = asyncio.create_task(finalize_session(context))
        interruptions = await self._wait_for_finalization()
        try:
            self._finalization_task.result()
        except asyncio.CancelledError as task_error:
            self._handle_finalization_task_error(task_error, error, interruptions)
        except Exception as task_error:  # ruff: ignore[blind-except] - cleanup errors
            self._handle_finalization_task_error(task_error, error, interruptions)
        self._handle_finalization_interruptions(error, interruptions)

    def _driver_context(
        self,
        primary_error: BaseException | None = None,
        finalization_error: BaseException | None = None,
    ) -> SessionDriverContext:
        """Create a driver view with this session's retained lifecycle state."""
        return SessionDriverContext(
            resource=self._resource,
            resources=self._resources,
            websocket=self._websocket,
            dispatch=self._dispatch,
            session_state=self._session_state,
            hook_manager=self._hook_manager,
            params=self._params,
            primary_error=primary_error,
            finalization_error=finalization_error,
            observer=self._observer,
        )

    async def _wait_for_finalization(self) -> list[asyncio.CancelledError]:
        """Wait for shared cleanup despite repeated cancellation requests."""
        task = self._finalization_task
        if task is None:
            msg = "WebSocket finalization has not started"
            raise LifecycleError(msg)
        return await _wait_for_shielded_task(task, cancel_on_interrupt=True)

    @staticmethod
    def _handle_finalization_task_error(
        task_error: BaseException,
        session_error: BaseException | None,
        interruptions: list[asyncio.CancelledError],
    ) -> typ.NoReturn:
        """Apply session-error precedence to a completed cleanup task."""
        if interruptions:
            if session_error is None:
                if not isinstance(task_error, asyncio.CancelledError):
                    _report_session_failure(
                        task_error,
                        "WebSocket finalization failed during cancellation",
                    )
                    raise interruptions[0] from task_error
            elif not isinstance(session_error, asyncio.CancelledError):
                _report_finalization_interruptions(interruptions)
        raise task_error

    @staticmethod
    def _handle_finalization_interruptions(
        session_error: BaseException | None,
        interruptions: list[asyncio.CancelledError],
    ) -> None:
        """Propagate caller cancellation unless a session error takes priority."""
        if not interruptions:
            return
        if isinstance(session_error, asyncio.CancelledError):
            raise session_error from None
        if session_error is None:
            raise interruptions[0] from None
        _report_finalization_interruptions(interruptions)


def _report_finalization_interruptions(
    interruptions: list[asyncio.CancelledError],
) -> None:
    """Report cancellation requests hidden by an earlier session error."""
    message = "WebSocket finalization was cancelled after a session error"
    for interruption in interruptions:
        _report_session_failure(interruption, message)


__all__ = [
    "AcceptOptions",
    "ConnectionPhase",
    "LifecycleError",
    "LifecycleGuard",
    "WebSocketSession",
]
