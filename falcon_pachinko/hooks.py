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

from typing_extensions import Unpack

if typ.TYPE_CHECKING:  # pragma: no cover - imported for type hints only
    import falcon

    from .protocols import WebSocketLike
    from .resource import WebSocketResource


HookCallable = typ.Callable[["HookContext"], typ.Awaitable[None] | None]


class _HookContextKwargs(typ.TypedDict, total=False):
    req: falcon.Request | None
    ws: WebSocketLike | None
    params: dict[str, object] | None
    raw: str | bytes | None
    result: bool | None
    error: BaseException | None
    close_code: int | None


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

    def __init__(
        self,
        initial: dict[str, list[HookCallable]] | None = None,
        *,
        parent: HookCollection | None = None,
    ) -> None:
        self._registry: dict[str, list[HookCallable]] = {
            event: list(initial.get(event, ())) if initial else []
            for event in _SUPPORTED_EVENTS
        }
        self._parent: HookCollection | None = parent

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
        local = tuple(self._registry[event])
        if self._parent is None:
            return local
        parent_hooks = self._parent.iter(event)
        if not parent_hooks:
            return local
        return parent_hooks + local

    @classmethod
    def clone_from(cls, other: HookCollection | None) -> HookCollection:
        """Return a deep copy of ``other`` or an empty collection."""
        if other is None:
            return cls()
        return cls(
            {event: list(other._registry[event]) for event in _SUPPORTED_EVENTS},
            parent=other._parent,
        )

    @classmethod
    def inherit(cls, parent: HookCollection | None) -> HookCollection:
        """Return a new collection whose iteration includes ``parent`` hooks."""
        return cls(parent=parent)


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
        self._resources = list(resources)

    async def _run_hooks(
        self, event: str, context: HookContext, *, reverse: bool = False
    ) -> None:
        layers: list[tuple[WebSocketResource | None, tuple[HookCallable, ...]]] = [
            (None, self._global_hooks.iter(event))
        ]
        for resource in self._resources:
            layers.append((resource, resource.hooks.iter(event)))
            if resource is context.target:
                break
        else:  # pragma: no cover - defensive programming
            msg = "target resource not managed by this HookManager"
            raise ValueError(msg)

        if reverse:
            layers.reverse()

        for resource, hooks in layers:
            context.resource = resource
            for hook in hooks:
                result = hook(context)
                if inspect.isawaitable(result):
                    await typ.cast("typ.Awaitable[None]", result)
        context.resource = None

    async def _dispatch_event(
        self,
        event: str,
        *,
        target: WebSocketResource,
        reverse: bool = False,
        context: HookContext | None = None,
        **kwargs: Unpack[_HookContextKwargs],
    ) -> HookContext:
        """Create or reuse ``context`` before executing ``event`` hooks."""
        ctx = context
        if ctx is None:
            ctx = HookContext(event=event, target=target, resource=None, **kwargs)
        else:
            ctx.event = event
        await self._run_hooks(event, ctx, reverse=reverse)
        return ctx

    async def notify_before_connect(
        self,
        target: WebSocketResource,
        *,
        req: falcon.Request,
        ws: WebSocketLike,
        params: dict[str, object],
    ) -> HookContext:
        """Fire ``before_connect`` hooks and return the shared context."""
        return await self._dispatch_event(
            "before_connect",
            target=target,
            req=req,
            ws=ws,
            params=params,
        )

    async def notify_after_connect(self, context: HookContext) -> None:
        """Run ``after_connect`` hooks using ``context``."""
        await self._dispatch_event(
            "after_connect",
            target=context.target,
            reverse=True,
            context=context,
        )

    async def notify_before_receive(
        self,
        target: WebSocketResource,
        *,
        ws: WebSocketLike,
        raw: str | bytes,
    ) -> HookContext:
        """Run ``before_receive`` hooks and return the shared context."""
        return await self._dispatch_event(
            "before_receive",
            target=target,
            ws=ws,
            raw=raw,
        )

    async def notify_after_receive(self, context: HookContext) -> None:
        """Run ``after_receive`` hooks using ``context``."""
        await self._dispatch_event(
            "after_receive",
            target=context.target,
            reverse=True,
            context=context,
        )

    async def notify_before_disconnect(
        self,
        target: WebSocketResource,
        *,
        ws: WebSocketLike,
        close_code: int,
    ) -> HookContext:
        """Run ``before_disconnect`` hooks and return the shared context."""
        return await self._dispatch_event(
            "before_disconnect",
            target=target,
            ws=ws,
            close_code=close_code,
        )
