"""Real-socket Falcon ASGI server support for WebSocket tests."""

from __future__ import annotations

import asyncio
import collections.abc as cabc
import socket
import typing as typ
from contextlib import asynccontextmanager

import falcon
import falcon.asgi

from .client import TraceEvent, WebSocketSession, WebSocketTestClient

if typ.TYPE_CHECKING:
    from falcon.asgi import WebSocket

type ASGIApplication = cabc.Callable[..., cabc.Awaitable[None]]


class _ClientOptions(typ.TypedDict, total=False):
    """Options accepted by :class:`WebSocketTestClient`."""

    default_headers: cabc.Mapping[str, str]
    subprotocols: cabc.Sequence[str]
    open_timeout: float
    capture_trace: bool
    trace_factory: cabc.Callable[[], list[TraceEvent]]
    allow_insecure: bool


class _ConnectOptions(typ.TypedDict, total=False):
    """Options accepted by :meth:`WebSocketTestClient.connect`."""

    headers: cabc.Mapping[str, str]
    subprotocols: cabc.Sequence[str]
    trace: list[TraceEvent] | bool | None


class ServerAdapter(typ.Protocol):
    """Minimal adapter contract for a server that serves an ASGI app."""

    @property
    def task(self) -> asyncio.Task[None]:
        """The task that owns the server's serve loop."""

    async def start(self, app: ASGIApplication, sock: socket.socket) -> None:
        """Start serving and return after lifespan and listener readiness."""

    async def stop(self) -> None:
        """Stop the listener gracefully."""


class LiveServerStartupError(RuntimeError):
    """Raised when the ASGI server does not become ready before its deadline."""


class LiveServerShutdownError(RuntimeError):
    """Raised when the ASGI server does not stop before its deadline."""


class LiveServerError(ExceptionGroup):
    """Group unacknowledged exceptions raised while a live server was active."""

    def __new__(cls, errors: cabc.Sequence[Exception]) -> LiveServerError:
        """Create an exception group from unacknowledged server failures."""
        return super().__new__(cls, "Live WebSocket server failures", list(errors))

    def __init__(self, errors: cabc.Sequence[Exception]) -> None:
        super().__init__("Live WebSocket server failures", list(errors))

    @typ.override
    def derive(self, exceptions: cabc.Sequence[Exception]) -> LiveServerError:
        """Keep filtered exception groups in the public live-server type."""
        return LiveServerError(exceptions)


class LiveWebSocketServer:
    """Run a Falcon ASGI app on an ephemeral localhost port for test clients.

    The default Uvicorn adapter is imported lazily. Applications with a custom
    ``Exception`` error handler should register it after startup and call
    :meth:`record_error` from that handler so teardown can report failures
    that Falcon handles internally.
    """

    # pylint: disable-next=too-many-arguments  # Keep the public startup options explicit.
    def __init__(  # ruff: ignore[too-many-arguments] -- documented constructor API
        self,
        app: falcon.asgi.App,
        *,
        adapter: ServerAdapter | None = None,
        startup_timeout: float = 10.0,
        shutdown_timeout: float = 5.0,
        capture_errors: bool = True,
    ) -> None:
        self._app = app
        self._adapter = adapter
        self._startup_timeout = startup_timeout
        self._shutdown_timeout = shutdown_timeout
        self._capture_errors = capture_errors
        self._errors: list[Exception] = []
        self._sessions: set[WebSocketSession] = set()
        self._socket: socket.socket | None = None
        self._entered = False

    @property
    def base_url(self) -> str:
        """The WebSocket origin assigned to this server instance."""
        sock = self._socket
        if sock is None:
            msg = "base_url is available after the live server starts"
            raise RuntimeError(msg)
        port = sock.getsockname()[1]
        return f"ws://127.0.0.1:{port}"

    @property
    def errors(self) -> tuple[Exception, ...]:
        """A read-only snapshot of unacknowledged server failures."""
        return tuple(self._errors)

    def pop_errors(self) -> tuple[Exception, ...]:
        """Return and acknowledge all currently captured server failures."""
        errors = tuple(self._errors)
        self._errors.clear()
        return errors

    # pylint: disable-next=trivial-attribute-wrapper  # Public callback target for custom Falcon error handlers.
    def record_error(self, ex: Exception) -> None:
        """Record an application failure for assertion or teardown reporting."""
        self._errors.append(ex)

    @staticmethod
    async def wait_for[ResultT](
        awaitable: cabc.Awaitable[ResultT],
        timeout: float,  # ruff: ignore[async-function-with-timeout] -- required API
    ) -> ResultT:
        """Wait for an awaitable for at most ``timeout`` seconds."""
        return await asyncio.wait_for(awaitable, timeout=timeout)

    def client(self, **options: typ.Unpack[_ClientOptions]) -> WebSocketTestClient:
        """Build a real-socket client configured for this local ``ws://`` URL."""
        client_options: _ClientOptions = {**options, "allow_insecure": True}
        return WebSocketTestClient(self.base_url, **client_options)

    @asynccontextmanager
    async def connect(
        self, path: str, **kwargs: typ.Unpack[_ConnectOptions]
    ) -> cabc.AsyncIterator[WebSocketSession]:
        """Open and track a client session until its context exits."""
        async with self.client().connect(path, **kwargs) as session:
            self._sessions.add(session)
            try:
                yield session
            finally:
                self._sessions.discard(session)

    async def __aenter__(self) -> LiveWebSocketServer:
        """Bind an ephemeral socket and wait for the adapter's readiness."""
        if self._entered:
            msg = "a LiveWebSocketServer instance can only be entered once"
            raise RuntimeError(msg)
        self._entered = True
        self._bind_socket()
        try:
            self._prepare_startup()
            await self._wait_for_startup()
        except asyncio.CancelledError as exc:
            try:
                await self._abort_startup()
            except Exception as cleanup_error:  # ruff: ignore[blind-except] -- retain cancellation
                exc.add_note(
                    f"Live server startup cleanup also failed: {cleanup_error!r}"
                )
            finally:
                self._close_socket()
            raise
        except Exception as exc:
            try:
                await self._abort_startup()
            except Exception as cleanup_error:  # ruff: ignore[blind-except] -- retain startup failure
                exc.add_note(
                    f"Live server startup cleanup also failed: {cleanup_error!r}"
                )
            finally:
                self._close_socket()
            msg = "Falcon ASGI server failed to start"
            raise LiveServerStartupError(msg) from exc
        return self

    def _prepare_startup(self) -> None:
        """Install error capture and select the default server adapter."""
        if self._capture_errors:
            self._app.add_error_handler(Exception, self._handle_exception)
        if self._adapter is None:
            from ._uvicorn import _UvicornAdapter

            self._adapter = _UvicornAdapter(shutdown_timeout=self._shutdown_timeout)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> bool:
        """Close clients and the server, then report unacknowledged failures."""
        try:
            await self._stop_server()
        finally:
            self._close_socket()

        if exc is not None:
            for error in self.errors:
                exc.add_note(f"Live WebSocket server also failed: {error!r}")
            return False
        if self._errors:
            raise LiveServerError(self.errors)
        return False

    def _bind_socket(self) -> None:
        """Reserve and retain an ephemeral loopback listening socket."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", 0))
            sock.listen()
            sock.setblocking(False)  # ruff: ignore[boolean-positional-value-in-call] -- socket API
        except Exception:
            sock.close()
            raise
        self._socket = sock

    def _close_socket(self) -> None:
        """Release the prebound listener after adapter shutdown."""
        sock = self._socket
        self._socket = None
        if sock is not None:
            sock.close()

    def _asgi_app(self) -> ASGIApplication:
        """Wrap the app so exceptions escaping Falcon remain observable."""
        app = typ.cast("ASGIApplication", self._app)
        if not self._capture_errors:
            return app

        async def capture_escaped_errors(
            scope: object, receive: object, send: object
        ) -> None:
            try:
                await app(scope, receive, send)
            except Exception as exc:
                self.record_error(exc)
                raise

        return capture_escaped_errors

    # pylint: disable-next=too-many-arguments,too-many-positional-arguments  # Match Falcon's error-handler callback contract.
    async def _handle_exception(  # ruff: ignore[too-many-arguments, too-many-positional-arguments] -- Falcon callback API
        self,
        req: falcon.Request,
        resp: falcon.Response | None,
        ex: Exception,
        params: cabc.Mapping[str, object],
        ws: WebSocket | None = None,
    ) -> None:
        """Capture a responder failure and return a generic transport error."""
        del req, params
        self.record_error(ex)
        if ws is not None:
            if not ws.closed:
                await ws.close(code=1011)
            return
        if resp is None:
            msg = "Falcon did not provide a response for an HTTP error"
            raise RuntimeError(msg)
        resp.status = falcon.HTTP_500
        resp.text = "Internal Server Error"

    def _validate_adapter(self) -> ServerAdapter:
        """Return the selected adapter or report an invalid adapter object."""
        adapter = self._adapter
        if adapter is None:
            msg = "a server adapter must be configured before startup"
            raise RuntimeError(msg)
        return adapter

    async def _wait_for_startup(self) -> None:
        """Race adapter readiness against early serve-task completion."""
        adapter = self._validate_adapter()
        sock = self._socket
        if sock is None:
            msg = "the listening socket was not created"
            raise RuntimeError(msg)
        start_task = asyncio.create_task(adapter.start(self._asgi_app(), sock))
        serve_task = adapter.task
        try:
            await self._await_startup_tasks(start_task, serve_task)
        except asyncio.CancelledError:
            await self._cancel_start_task(start_task)
            raise
        except Exception:
            await self._cancel_start_task(start_task)
            raise

    async def _await_startup_tasks(
        self,
        start_task: asyncio.Task[None],
        serve_task: asyncio.Task[None],
    ) -> None:
        """Wait for readiness and reject a serve task that exits too early."""
        done, _pending = await asyncio.wait(
            {start_task, serve_task},
            timeout=self._startup_timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            msg = "ASGI server startup timed out"
            raise TimeoutError(msg)
        if serve_task in done:
            self._raise_serve_task_failure(serve_task, "before readiness")
        await start_task
        if serve_task.done():
            self._raise_serve_task_failure(serve_task, "during startup")

    @staticmethod
    def _raise_serve_task_failure(task: asyncio.Task[None], stage: str) -> typ.NoReturn:
        """Raise why the serve task ended before the server became ready."""
        if task.cancelled():
            msg = f"server task was cancelled {stage}"
            raise RuntimeError(msg)
        error = task.exception()
        if error is not None:
            raise error
        msg = f"server task exited {stage}"
        raise RuntimeError(msg)

    async def _cancel_start_task(self, task: asyncio.Task[None]) -> None:
        """Cancel and consume a pending adapter startup request."""
        if not task.done():
            task.cancel()
        await self._consume_cancelled_task(task)

    async def _abort_startup(self) -> None:
        """Stop a partially-started adapter and consume its task outcome."""
        adapter = self._adapter
        if adapter is None:
            return
        try:
            await self.wait_for(adapter.stop(), self._shutdown_timeout)
        except Exception:  # ruff: ignore[blind-except] -- stop a partially started server
            task = adapter.task
            if not task.done():
                task.cancel()
        await self._cancel_or_consume_task(adapter.task)

    async def _stop_server(self) -> None:
        """Close sessions, stop the adapter and collect serve-task failures."""
        for session in tuple(self._sessions):
            try:
                await session.close()
            except Exception as exc:  # ruff: ignore[blind-except] -- retain session cleanup failures
                self.record_error(exc)
            finally:
                self._sessions.discard(session)

        adapter = self._adapter
        if adapter is None:
            return
        try:
            async with asyncio.timeout(self._shutdown_timeout):
                await adapter.stop()
                await asyncio.shield(adapter.task)
        except TimeoutError as exc:
            self.record_error(LiveServerShutdownError("ASGI server shutdown timed out"))
            await self._cancel_or_consume_task(adapter.task)
            exc.add_note("The live server task was cancelled after the deadline")
        except asyncio.CancelledError:
            await self._cancel_or_consume_task(adapter.task)
            raise
        except Exception as exc:  # ruff: ignore[blind-except] -- report adapter failure at teardown
            self.record_error(exc)
            await self._cancel_or_consume_task(adapter.task)

    async def _cancel_or_consume_task(self, task: asyncio.Task[None]) -> None:
        """Cancel a remaining server task and retrieve any final exception."""
        if not task.done():
            task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), self._shutdown_timeout)
        except TimeoutError:
            self.record_error(
                LiveServerShutdownError("server task did not finish after cancellation")
            )
        except asyncio.CancelledError:
            if not task.cancelled():
                raise
        except Exception:  # ruff: ignore[blind-except] -- consume the failed task outcome
            # A startup or serve-task failure has already been chained or
            # recorded by the operation that requested cancellation.
            return

    @staticmethod
    async def _consume_cancelled_task(task: asyncio.Task[None]) -> None:
        """Wait for a task cancelled by this harness without leaking errors."""
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if not task.cancelled():
                raise
        except Exception:  # ruff: ignore[blind-except] -- consume the failed task outcome
            # Startup's chained exception or shutdown's recorded error is
            # already the actionable failure; consuming it prevents warnings.
            return
