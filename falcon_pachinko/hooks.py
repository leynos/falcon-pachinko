"""Utilities for registering and executing WebSocket lifecycle hooks.

The multi-tiered hook system allows applications to register callbacks that
wrap the WebSocket lifecycle at both the router (global) and resource levels.
Hooks execute in an "onion-style" order so that outer layers (global hooks)
run before inner ones and after hooks unwind in reverse order. This mirrors the
design outlined in :doc:`/falcon-websocket-extension-design` and keeps
cross-cutting concerns explicit and testable.
"""

from __future__ import annotations

import dataclasses as dc
import enum
import inspect
import typing as typ

import typing_extensions as tpe

if typ.TYPE_CHECKING:  # pragma: no cover - imported for type hints only
    import falcon

    from .protocols import WebSocketLike
    from .resource import WebSocketResource


HookCallable = typ.Callable[["HookContext"], typ.Awaitable[None] | None]


if typ.TYPE_CHECKING:

    class _StrEnumBase(str, enum.Enum):
        """Type-checking base when :class:`enum.StrEnum` is unavailable."""

        pass
else:
    try:
        _StrEnumBase = typ.cast("type[enum.Enum]", enum.StrEnum)  # type: ignore[attr-defined]
    except AttributeError:  # pragma: no cover - executed only on Python < 3.11

        class _StrEnumBase(str, enum.Enum):
            """Compatibility shim when :class:`enum.StrEnum` is unavailable."""

            pass


class HookEvent(_StrEnumBase):
    """Supported WebSocket lifecycle hook events."""

    BEFORE_CONNECT = "before_connect"
    AFTER_CONNECT = "after_connect"
    BEFORE_RECEIVE = "before_receive"
    AFTER_RECEIVE = "after_receive"
    BEFORE_DISCONNECT = "before_disconnect"


EventType = HookEvent | str


class _HookContextKwargs(typ.TypedDict, total=False):
    """Optional fields forwarded when constructing :class:`HookContext`."""

    req: falcon.Request
    ws: WebSocketLike
    params: dict[str, object]
    raw: str | bytes
    result: bool
    error: BaseException
    close_code: int


_SUPPORTED_EVENTS = frozenset(event.value for event in HookEvent)


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

    event: EventType
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

    def add(self, event: EventType, hook: HookCallable) -> None:
        """Register ``hook`` for the given ``event``."""
        event_name = str(event)
        if event_name not in _SUPPORTED_EVENTS:
            msg = f"Unsupported hook event: {event!r}"
            raise ValueError(msg)
        if not callable(hook):
            msg = "hook must be callable"
            raise TypeError(msg)
        self._registry[event_name].append(hook)

    def iter(self, event: EventType) -> tuple[HookCallable, ...]:
        """Return the hooks registered for ``event``."""
        event_name = str(event)
        if event_name not in _SUPPORTED_EVENTS:
            msg = f"Unsupported hook event: {event!r}"
            raise ValueError(msg)
        local = tuple(self._registry[event_name])
        parent_hooks = self._parent.iter(event_name) if self._parent is not None else ()
        return parent_hooks + local

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
        self, event: EventType, context: HookContext, *, reverse: bool = False
    ) -> None:
        event_name = str(event)
        layers: list[tuple[WebSocketResource | None, tuple[HookCallable, ...]]] = [
            (None, self._global_hooks.iter(event_name))
        ]
        for resource in self._resources:
            layers.append((resource, resource.hooks.iter(event_name)))
            if resource is context.target:
                break
        else:  # pragma: no cover - defensive guard
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
        return

    async def _notify_before_event(
        self,
        event: EventType,
        target: WebSocketResource,
        **kwargs: tpe.Unpack[_HookContextKwargs],
    ) -> HookContext:
        """Dispatch before-* lifecycle events."""
        context = HookContext(
            event=str(event),
            target=target,
            resource=None,
            **kwargs,
        )
        await self._run_hooks(event, context)
        return context

    async def notify_before_connect(
        self,
        target: WebSocketResource,
        *,
        req: falcon.Request,
        ws: WebSocketLike,
        params: dict[str, object],
    ) -> HookContext:
        """Fire ``before_connect`` hooks and return the shared context."""
        return await self._notify_before_event(
            HookEvent.BEFORE_CONNECT, target, req=req, ws=ws, params=params
        )

    async def notify_after_connect(self, context: HookContext) -> None:
        """Run ``after_connect`` hooks using ``context``."""
        context.event = HookEvent.AFTER_CONNECT
        await self._run_hooks(HookEvent.AFTER_CONNECT, context, reverse=True)
        return

    async def notify_before_receive(
        self,
        target: WebSocketResource,
        *,
        ws: WebSocketLike,
        raw: str | bytes,
    ) -> HookContext:
        """Run ``before_receive`` hooks and return the shared context."""
        return await self._notify_before_event(
            HookEvent.BEFORE_RECEIVE, target, ws=ws, raw=raw
        )

    async def notify_after_receive(self, context: HookContext) -> None:
        """Run ``after_receive`` hooks using ``context``."""
        context.event = HookEvent.AFTER_RECEIVE
        await self._run_hooks(HookEvent.AFTER_RECEIVE, context, reverse=True)
        return

    async def notify_before_disconnect(
        self,
        target: WebSocketResource,
        *,
        ws: WebSocketLike,
        close_code: int,
    ) -> HookContext:
        """Run ``before_disconnect`` hooks and return the shared context."""
        return await self._notify_before_event(
            HookEvent.BEFORE_DISCONNECT, target, ws=ws, close_code=close_code
        )
