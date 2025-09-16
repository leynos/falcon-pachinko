"""Utilities for registering and executing WebSocket lifecycle hooks.

The multi-tiered hook system allows applications to register callbacks that
wrap the WebSocket lifecycle at both the router (global) and resource levels.
Hooks execute in an "onion-style" order so that outer layers (global hooks)
run before inner ones and after hooks unwind in reverse order. This mirrors the
design outlined in :mod:`docs/falcon-websocket-extension-design.md` and keeps
cross-cutting concerns explicit and testable.
"""

from __future__ import annotations

import dataclasses as dc
import inspect
import typing as typ

if typ.TYPE_CHECKING:  # pragma: no cover - imported for type hints only
    import falcon

    from .protocols import WebSocketLike
    from .resource import WebSocketResource


HookCallable = typ.Callable[["HookContext"], typ.Awaitable[None] | None]

_SUPPORTED_EVENTS = (
    "before_connect",
    "after_connect",
    "before_receive",
    "after_receive",
    "before_disconnect",
)


@dc.dataclass(slots=True)
class HookContext:
    """Context object passed to hook callbacks.

    Parameters
    ----------
    event:
        Name of the lifecycle event currently being processed.
    target:
        The innermost resource for the current connection or message.
    resource:
        The resource whose hooks are executing. ``None`` for global hooks.
    req:
        Request associated with the connection attempt. Only populated for
        connection hooks.
    ws:
        WebSocket instance associated with the event.
    params:
        Route parameters passed to :meth:`WebSocketResource.on_connect`.
    raw:
        Raw message payload supplied to :meth:`WebSocketResource.dispatch`.
    result:
        Result produced by the wrapped handler, such as the boolean returned by
        ``on_connect``. ``None`` if not applicable.
    error:
        Exception raised by the wrapped handler, if any.
    close_code:
        Optional close code supplied when a disconnect hook fires.
    """

    event: str
    target: WebSocketResource
    resource: WebSocketResource | None
    req: falcon.Request | None = None
    ws: WebSocketLike | None = None
    params: dict[str, object] | None = None
    raw: str | bytes | None = None
    result: bool | None = None
    error: BaseException | None = None
    close_code: int | None = None


class HookCollection:
    """Registry for lifecycle hooks tied to a particular scope."""

    def __init__(self, initial: dict[str, list[HookCallable]] | None = None) -> None:
        self._registry: dict[str, list[HookCallable]] = {
            event: list(initial.get(event, ())) if initial else []
            for event in _SUPPORTED_EVENTS
        }

    def add(self, event: str, hook: HookCallable) -> None:
        """Register ``hook`` for the given ``event``."""
        if event not in _SUPPORTED_EVENTS:
            msg = f"Unsupported hook event: {event!r}"
            raise ValueError(msg)
        if not callable(hook):
            msg = "hook must be callable"
            raise TypeError(msg)
        self._registry[event].append(hook)

    def iter(self, event: str) -> tuple[HookCallable, ...]:
        """Return the hooks registered for ``event``."""
        if event not in _SUPPORTED_EVENTS:
            msg = f"Unsupported hook event: {event!r}"
            raise ValueError(msg)
        return tuple(self._registry[event])

    @classmethod
    def clone_from(cls, other: HookCollection | None) -> HookCollection:
        """Return a deep copy of ``other`` or an empty collection."""
        if other is None:
            return cls()
        return cls({event: list(other._registry[event]) for event in _SUPPORTED_EVENTS})


class HookManager:
    """Coordinate hook execution across router and resource tiers."""

    def __init__(
        self,
        *,
        global_hooks: HookCollection,
        resources: typ.Sequence[WebSocketResource],
    ) -> None:
        if not resources:
            msg = "HookManager requires at least one resource"
            raise ValueError(msg)
        self._global_hooks = global_hooks
        self._resources = tuple(resources)
        self._index = {
            id(resource): idx for idx, resource in enumerate(self._resources)
        }

    def _iter_resources(
        self, target: WebSocketResource
    ) -> tuple[WebSocketResource, ...]:
        try:
            idx = self._index[id(target)]
        except KeyError as exc:  # pragma: no cover - defensive programming
            msg = "target resource not managed by this HookManager"
            raise ValueError(msg) from exc
        return self._resources[: idx + 1]

    async def _invoke(
        self, hooks: tuple[HookCallable, ...], context: HookContext
    ) -> None:
        for hook in hooks:
            result = hook(context)
            if inspect.isawaitable(result):
                await typ.cast("typ.Awaitable[None]", result)

    async def _run_before(self, event: str, context: HookContext) -> None:
        context.resource = None
        await self._invoke(self._global_hooks.iter(event), context)
        for resource in self._iter_resources(context.target):
            context.resource = resource
            await self._invoke(resource.hooks.iter(event), context)
        context.resource = None

    async def _run_after(self, event: str, context: HookContext) -> None:
        for resource in reversed(self._iter_resources(context.target)):
            context.resource = resource
            await self._invoke(resource.hooks.iter(event), context)
        context.resource = None
        await self._invoke(self._global_hooks.iter(event), context)

    async def notify_before_connect(
        self,
        target: WebSocketResource,
        *,
        req: falcon.Request,
        ws: WebSocketLike,
        params: dict[str, object],
    ) -> HookContext:
        """Fire ``before_connect`` hooks and return the shared context."""
        context = HookContext(
            event="before_connect",
            target=target,
            resource=None,
            req=req,
            ws=ws,
            params=params,
        )
        await self._run_before("before_connect", context)
        return context

    async def notify_after_connect(self, context: HookContext) -> None:
        """Run ``after_connect`` hooks using ``context``."""
        context.event = "after_connect"
        await self._run_after("after_connect", context)

    async def notify_before_receive(
        self,
        target: WebSocketResource,
        *,
        ws: WebSocketLike,
        raw: str | bytes,
    ) -> HookContext:
        """Run ``before_receive`` hooks and return the shared context."""
        context = HookContext(
            event="before_receive",
            target=target,
            resource=None,
            ws=ws,
            raw=raw,
        )
        await self._run_before("before_receive", context)
        return context

    async def notify_after_receive(self, context: HookContext) -> None:
        """Run ``after_receive`` hooks using ``context``."""
        context.event = "after_receive"
        await self._run_after("after_receive", context)

    async def notify_before_disconnect(
        self,
        target: WebSocketResource,
        *,
        ws: WebSocketLike,
        close_code: int,
    ) -> HookContext:
        """Run ``before_disconnect`` hooks and return the shared context."""
        context = HookContext(
            event="before_disconnect",
            target=target,
            resource=None,
            ws=ws,
            close_code=close_code,
        )
        await self._run_before("before_disconnect", context)
        return context
