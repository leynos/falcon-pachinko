"""Public WebSocket connection lifecycle and accepted-session handle."""

from __future__ import annotations

import asyncio
import collections.abc as cabc
import dataclasses as dc
import enum
import typing as typ
import warnings

import falcon

from ._session import _report_session_failure, _wait_for_shielded_task
from ._session_driver import SessionDriverContext, finalize_session, run_session

if typ.TYPE_CHECKING:
    from ._session import SessionState
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

    __slots__ = ("_close_code", "_phase", "_session", "_websocket")

    def __init__(self, websocket: WebSocketLike, session: SessionState) -> None:
        self._websocket = websocket
        self._session = session
        self._phase = ConnectionPhase.PRE_ACCEPT
        self._close_code: int | None = None

        if bool(getattr(websocket, "closed", False)):
            self._phase = ConnectionPhase.CLOSED
        elif getattr(websocket, "unaccepted", True) is False:
            self._phase = ConnectionPhase.ACCEPTED
            self._session.establish()

    @property
    def phase(self) -> ConnectionPhase:
        """Current connection phase; callers cannot modify it directly."""
        return self._phase

    @property
    def close_code(self) -> int | None:
        """First local or peer close code observed by this guard."""
        return self._close_code

    @property
    def closed(self) -> bool:
        """Whether the lifecycle or its underlying transport is closed."""
        return self._phase is ConnectionPhase.CLOSED or bool(
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

        kwargs: dict[str, object] = {}
        if options is not None:
            if options.subprotocol is not None:
                kwargs["subprotocol"] = options.subprotocol
            if options.headers is not None:
                kwargs["headers"] = options.headers
        accept = typ.cast(
            "cabc.Callable[..., cabc.Awaitable[None]]", self._websocket.accept
        )
        try:
            await accept(**kwargs)
        except (Exception, asyncio.CancelledError):
            if getattr(self._websocket, "unaccepted", True) is False:
                self._mark_accepted()
            raise
        if getattr(self._websocket, "unaccepted", None) is True:
            msg = "WebSocket transport returned from accept() without accepting"
            raise LifecycleError(msg)
        self._mark_accepted()

    async def close(self, code: int | None = 1000, reason: str | None = None) -> None:
        """Reject before acceptance or close an accepted connection once."""
        if self._phase is ConnectionPhase.CLOSED:
            return

        effective_code = 1000 if code is None else code
        close = typ.cast(
            "cabc.Callable[..., cabc.Awaitable[None]]", self._websocket.close
        )
        kwargs: dict[str, object] = {"code": effective_code}
        if reason is not None:
            kwargs["reason"] = reason
        try:
            await close(**kwargs)
        except (Exception, asyncio.CancelledError):
            if bool(getattr(self._websocket, "closed", False)):
                self._mark_closed(effective_code)
            raise
        self._mark_closed(effective_code)

    async def send_media(self, media: object, **kwargs: object) -> None:
        """Send structured media after acceptance."""
        self._require_accepted("send_media")
        await self._run_transport("send_media", media, **kwargs)

    async def send_text(self, message: str) -> None:
        """Send a text frame after acceptance when the transport supports it."""
        self._require_accepted("send_text")
        if callable(method := getattr(self._websocket, "send_text", None)):
            await typ.cast("cabc.Callable[[str], cabc.Awaitable[None]]", method)(
                message
            )
        else:
            await self.send_media(message)

    async def send_bytes(self, payload: bytes) -> None:
        """Send a binary frame after acceptance when the transport supports it."""
        self._require_accepted("send_bytes")
        method = getattr(self._websocket, "send_bytes", None)
        if not callable(method):
            method = getattr(self._websocket, "send_data", None)
        if callable(method):
            await typ.cast("cabc.Callable[[bytes], cabc.Awaitable[None]]", method)(
                payload
            )
        else:
            await self.send_media(payload)

    async def send_json(self, payload: object) -> None:
        """Send JSON after acceptance when the transport supports it."""
        self._require_accepted("send_json")
        if callable(method := getattr(self._websocket, "send_json", None)):
            await typ.cast("cabc.Callable[[object], cabc.Awaitable[None]]", method)(
                payload
            )
        else:
            await self.send_media(payload)

    async def receive_media(self) -> object:
        """Receive structured media after acceptance."""
        self._require_accepted("receive_media")
        try:
            return await self._run_transport("receive_media")
        except falcon.WebSocketDisconnected as exc:
            self._record_disconnect(exc)
            raise

    async def _receive_payload(self) -> object:
        """Read one raw frame for router dispatch without changing media APIs."""
        self._require_accepted("receive_media")
        try:
            receive_payload = getattr(self._websocket, "receive_payload", None)
            if callable(receive_payload):
                return await typ.cast(
                    "cabc.Callable[[], cabc.Awaitable[object]]", receive_payload
                )()
            return await self._run_transport("receive_media")
        except falcon.WebSocketDisconnected as exc:
            self._record_disconnect(exc)
            raise

    async def receive_text(self) -> str:
        """Receive a text frame after acceptance."""
        self._require_accepted("receive_text")
        try:
            if callable(method := getattr(self._websocket, "receive_text", None)):
                message = await typ.cast(
                    "cabc.Callable[[], cabc.Awaitable[object]]", method
                )()
            else:
                message = await self._run_transport("receive_media")
        except falcon.WebSocketDisconnected as exc:
            self._record_disconnect(exc)
            raise
        if not isinstance(message, str):
            msg = "expected a text WebSocket frame"
            raise TypeError(msg)
        return message

    async def receive_bytes(self) -> bytes:
        """Receive a binary frame after acceptance when the transport supports it."""
        self._require_accepted("receive_bytes")
        try:
            method = getattr(self._websocket, "receive_bytes", None)
            if not callable(method):
                method = getattr(self._websocket, "receive_data", None)
            if callable(method):
                message = await typ.cast(
                    "cabc.Callable[[], cabc.Awaitable[object]]", method
                )()
            else:
                message = await self._run_transport("receive_media")
        except falcon.WebSocketDisconnected as exc:
            self._record_disconnect(exc)
            raise
        if not isinstance(message, bytes):
            msg = "expected a binary WebSocket frame"
            raise TypeError(msg)
        return message

    async def receive_json(self, payload_type: type[object] | None = None) -> object:
        """Receive JSON after acceptance when the transport supports it."""
        self._require_accepted("receive_json")
        try:
            if callable(method := getattr(self._websocket, "receive_json", None)):
                return await typ.cast(
                    "cabc.Callable[..., cabc.Awaitable[object]]", method
                )(payload_type)
            return await self.receive_media()
        except falcon.WebSocketDisconnected as exc:
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
        if self._phase is ConnectionPhase.ACCEPTED:
            return
        if self._phase is ConnectionPhase.PRE_ACCEPT:
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
        if self._close_code is None:
            self._close_code = code
        if self._session.close_code is None:
            self._session.close_code = self._close_code
        self._phase = ConnectionPhase.CLOSED

    def _record_disconnect(self, error: falcon.WebSocketDisconnected) -> None:
        """Record the peer code when the receive API reports a disconnect."""
        code = getattr(error, "code", None)
        code = code if isinstance(code, int) else None
        if self._close_code is None:
            self._close_code = code
        if self._session.close_code is None:
            self._session.close_code = self._close_code
        self._phase = ConnectionPhase.CLOSED


class WebSocketSession:
    """Handle an accepted server-side connection through finalization.

    This router session is distinct from the client facade returned by
    ``WebSocketTestClient.connect()``.
    """

    def __init__(self, context: SessionDriverContext) -> None:
        self._resource = context.resource
        self._resources = tuple(context.resources)
        self._websocket: LifecycleGuard = context.websocket
        self._hook_manager = context.hook_manager
        self._dispatch = context.dispatch
        self._session_state = context.session_state
        self._params = dict(context.params)
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
        context = SessionDriverContext(
            resource=self._resource,
            resources=self._resources,
            websocket=self._websocket,
            dispatch=self._dispatch,
            session_state=self._session_state,
            hook_manager=self._hook_manager,
            params=self._params,
        )
        run_error: BaseException | None = None
        try:
            await run_session(context, self._finalize)
        except BaseException as error:
            run_error = error
            raise
        finally:
            if not run_completion.done():
                run_completion.set_result(run_error)

    async def aclose(self, code: int = 1000) -> None:
        """Close and finalize the session once, including manual sessions.

        Parameters
        ----------
        code : int
            Close code sent when the transport remains open.

        Raises
        ------
        asyncio.CancelledError
            Cancellation received while waiting for the session to finish.

        Notes
        -----
        If ``run()`` is active in another task, this method closes the
        transport and waits for that task to finish without a timeout.
        Closing the socket does not interrupt unrelated work in
        ``on_session_start()``. To bound shutdown, cancel and await the run
        task before or concurrently with calling ``aclose()``.
        """
        close_error = await _capture_error(self._websocket.close(code))
        run_error: BaseException | None = None

        run_completion = self._run_completion
        if run_completion is not None and self._run_task is not asyncio.current_task():
            run_error = await asyncio.shield(run_completion)

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
            context = SessionDriverContext(
                resource=self._resource,
                resources=self._resources,
                websocket=self._websocket,
                dispatch=self._dispatch,
                session_state=self._session_state,
                hook_manager=self._hook_manager,
                params=self._params,
                primary_error=error,
                finalization_error=finalization_error,
            )
            self._finalization_task = asyncio.create_task(finalize_session(context))
        interruptions = await self._wait_for_finalization()
        try:
            self._finalization_task.result()
        except asyncio.CancelledError as task_error:
            self._handle_finalization_task_error(task_error, error, interruptions)
        except Exception as task_error:  # ruff: ignore[blind-except] - cleanup errors
            self._handle_finalization_task_error(task_error, error, interruptions)
        self._handle_finalization_interruptions(error, interruptions)

    async def _wait_for_finalization(self) -> list[asyncio.CancelledError]:
        """Wait for shared cleanup despite repeated cancellation requests."""
        task = self._finalization_task
        if task is None:
            msg = "WebSocket finalization has not started"
            raise LifecycleError(msg)
        return await _wait_for_shielded_task(task)

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
