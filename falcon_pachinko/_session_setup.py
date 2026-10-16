"""Share the connection setup sequence between router session modes."""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from ._session_cleanup import raise_session_failures

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    import falcon

    from .hooks import HookContext, HookManager
    from .protocols import WebSocketLike
    from .resource import WebSocketResource


class _ConnectionSetupOrderError(RuntimeError):
    """Raised when a setup phase is called before its required predecessor."""


@dc.dataclass(slots=True)
class ConnectionSetup:
    """Carry one resource's setup state and its acceptance policy."""

    hook_manager: HookManager
    resource: WebSocketResource
    req: falcon.Request
    ws: WebSocketLike
    route_params: cabc.Mapping[str, str]
    is_unaccepted: cabc.Callable[[WebSocketLike], bool]
    is_accepted: cabc.Callable[[WebSocketLike], bool] | None = None
    on_approval: cabc.Callable[[], None] | None = None
    context: HookContext | None = dc.field(default=None, init=False)
    should_accept: bool = dc.field(default=False, init=False)
    _approval_seen: bool = dc.field(default=False, init=False, repr=False)

    async def prepare(self) -> HookContext:
        """Resolve route parameters and run before-connect hooks."""
        params: dict[str, object] = dict(self.route_params)
        context = await self.hook_manager.notify_before_connect(
            self.resource,
            req=self.req,
            ws=self.ws,
            params=params,
        )
        # Before-connect hooks may replace the parameters or leave the route copy.
        if context.params is None:
            context.params = params
        self.context = context
        return context

    async def execute(self) -> bool:
        """Run on-connect and retain approval before later setup can fail."""
        context = self._prepared_context()
        params = context.params if context.params is not None else {}
        try:
            self.should_accept = await self.resource.on_connect(
                self.req, self.ws, **params
            )
        except Exception as exc:
            context.error = exc
            context.result = False
            self._record_approval(has_approval=self._socket_is_accepted())
            try:
                await self.hook_manager.notify_after_connect(context)
            except BaseException as after_error:
                # Only the session-owning mode promises cleanup after acceptance.
                if self.is_accepted is not None and self.is_accepted(self.ws):
                    raise_session_failures(exc, (after_error,), ())
                raise
            raise
        context.result = self.should_accept
        self._record_approval(
            has_approval=self.should_accept or self._socket_is_accepted()
        )
        return self.should_accept

    async def finalize(self) -> bool:
        """Run after-connect hooks and complete acceptance or rejection."""
        context = self._prepared_context()
        context.result = self.should_accept
        await self.hook_manager.notify_after_connect(context)
        self._record_approval(
            has_approval=self.should_accept or self._socket_is_accepted()
        )
        if not self.should_accept:
            await self.ws.close()
            return True
        if self.is_unaccepted(self.ws):
            await self.ws.accept()
        return True

    def _prepared_context(self) -> HookContext:
        """Return setup context after the ordered before-connect phase."""
        if self.context is None:
            raise _ConnectionSetupOrderError
        return self.context

    def _socket_is_accepted(self) -> bool:
        """Use the session path's stricter public Falcon acceptance probe."""
        return self.is_accepted is not None and self.is_accepted(self.ws)

    def _record_approval(self, *, has_approval: bool) -> None:
        """Establish cleanup once approval or resource acceptance is known."""
        if has_approval and not self._approval_seen:
            self._approval_seen = True
            if self.on_approval is not None:
                self.on_approval()
