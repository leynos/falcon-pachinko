"""WebSocket resource handling and message dispatching functionality.

This module provides the :class:`WebSocketResource` base class used to
implement WebSocket handlers. Subclasses may optionally define a
``schema`` attribute referencing a :func:`typing.Union` of
``msgspec.Struct`` types. When present, incoming messages are decoded
according to this tagged union and dispatched based on the message tag,
enabling high-performance, schema-driven routing without extra
boilerplate.
"""

from __future__ import annotations

import functools
import inspect
import typing as typ

if typ.TYPE_CHECKING:  # pragma: no cover - imported for type hints
    import collections.abc as cabc
    import re

    import falcon

    from .protocols import WebSocketLike

from .dispatcher import dispatch
from .handlers import Handler, HandlerInfo, _HandlesMessageDescriptor
from .hooks import HookCollection, HookManager
from .schema import populate_struct_handlers, validate_schema_types


class WebSocketResource:
    """Base class for WebSocket handlers.

    Subclasses may optionally define a :attr:`schema` attribute referencing a
    :func:`typing.Union` of :class:`msgspec.Struct` types. When provided,
    incoming messages are decoded using this tagged union and dispatched based
    on the message tag. This enables high-performance, schema-driven routing
    without additional boilerplate.
    """

    handlers: typ.ClassVar[dict[str, HandlerInfo]]
    _struct_handlers: typ.ClassVar[dict[type, HandlerInfo]] = {}
    schema: type | None = None
    hooks: HookCollection = HookCollection()

    def add_subroute(
        self,
        path: str,
        resource: type[WebSocketResource] | cabc.Callable[..., WebSocketResource],
        *,
        args: tuple[typ.Any, ...] = (),
        kwargs: dict[str, typ.Any] | None = None,
    ) -> None:
        """Register ``resource`` to handle a nested ``path``.

        This method mutates the instance's ``_subroutes`` list. Resource
        instances are expected to be per-connection objects and must not be
        shared across threads.
        """
        if kwargs is None:
            kwargs = {}
        if not callable(resource):
            raise TypeError("resource must be callable")  # noqa: TRY003

        from .router import _canonical_path, _compile_prefix_template

        canonical = _canonical_path(path)
        pattern = _compile_prefix_template(canonical)
        factory = functools.partial(resource, *args, **kwargs)

        subroutes: list[tuple[re.Pattern[str], cabc.Callable[..., WebSocketResource]]]
        subroutes = getattr(self, "_subroutes", [])
        for existing, _ in subroutes:
            if existing.pattern == pattern.pattern:
                msg = f"subroute path {path!r} already registered"
                raise ValueError(msg)

        subroutes.append((pattern, factory))
        self._subroutes = subroutes

    def get_child_context(self) -> dict[str, object]:
        """Return kwargs to be forwarded to the next child resource.

        Override this hook to explicitly share context or dependencies with a
        nested resource. The returned mapping is applied to the child's
        constructor. If ``state`` is included, its value will replace the
        connection-scoped ``state`` passed to the child; otherwise, the parent
        state is propagated automatically.
        """
        return {}

    @property
    def state(self) -> cabc.MutableMapping[str, typ.Any]:
        """Per-connection state mapping."""
        if not hasattr(self, "_state"):
            self._state = {}
        return self._state

    @state.setter
    def state(self, mapping: cabc.MutableMapping[str, typ.Any]) -> None:
        required_methods = ("__getitem__", "__setitem__", "__iter__")
        if not all(hasattr(mapping, method) for method in required_methods):
            msg = (
                "state must be a mapping-like object implementing "
                f"{required_methods}, got {type(mapping).__name__}"
            )
            raise TypeError(msg)
        self._state = mapping

    def __init_subclass__(cls, **kwargs: object) -> None:
        """Initialize subclass handler mappings."""
        super().__init_subclass__(**kwargs)
        existing = getattr(cls, "handlers", {})
        handlers = cls._collect_base_handlers()
        handlers.update(existing)
        cls._apply_overrides(handlers)
        cls.handlers = handlers
        cls._init_schema_registry()
        parent_hooks = getattr(cls, "hooks", None)
        if isinstance(parent_hooks, HookCollection):
            cls.hooks = HookCollection.inherit(parent_hooks)
        else:
            cls.hooks = HookCollection()

    @classmethod
    def _collect_base_handlers(cls) -> dict[str, HandlerInfo]:
        combined: dict[str, HandlerInfo] = {}
        for base in cls.__mro__[1:]:
            base_handlers = getattr(base, "handlers", None)
            if base_handlers:
                combined.update(base_handlers)
        return combined

    @classmethod
    def _apply_overrides(cls, handlers: dict[str, HandlerInfo]) -> None:
        shadowed = {
            name
            for name, obj in cls.__dict__.items()
            if not isinstance(obj, _HandlesMessageDescriptor)
            and inspect.iscoroutinefunction(obj)
        }
        for msg_type, info in list(handlers.items()):
            if info.handler.__name__ in shadowed:
                new_handler = typ.cast("Handler", cls.__dict__[info.handler.__name__])
                handlers[msg_type] = HandlerInfo(
                    new_handler, info.payload_type, info.strict
                )

    @classmethod
    def _init_schema_registry(cls) -> None:
        cls._struct_handlers = {}
        schema = getattr(cls, "schema", None)
        if schema is None:
            return
        validate_schema_types(schema)
        cls._struct_handlers = populate_struct_handlers(cls)

    async def on_connect(
        self, req: falcon.Request, ws: WebSocketLike, **params: object
    ) -> bool:
        """Decide whether the connection should be accepted after handshake.

        Called after the WebSocket handshake is complete to determine
        acceptance.

        Parameters
        ----------
        req : falcon.Request
            The incoming HTTP request associated with the WebSocket handshake
        ws : WebSocketLike
            The WebSocket connection object
        **params : object
            Additional parameters relevant to the connection

        Returns
        -------
        bool
            ``True`` to accept the WebSocket connection; ``False`` to reject it
        """
        return True

    async def on_disconnect(self, ws: WebSocketLike, close_code: int) -> None:
        """Handle cleanup or custom logic when the connection is closed.

        Parameters
        ----------
        ws : WebSocketLike
            The WebSocket connection instance
        close_code : int
            The close code indicating the reason for disconnection
        """

    async def on_unhandled(self, ws: WebSocketLike, message: str | bytes) -> None:
        """Handle incoming messages that don't match any handler.

        This method acts as a catch-all for messages that fail decoding or do
        not map to a registered handler. Override it to implement custom
        fallback behaviour for such cases.

        Parameters
        ----------
        ws : WebSocketLike
            The WebSocket connection instance
        message : str or bytes
            The raw message received
        """

    @classmethod
    def add_handler(
        cls,
        message_type: str,
        handler: Handler,
        *,
        payload_type: type | None = None,
        strict: bool = True,
    ) -> None:
        """Register ``handler`` for ``message_type``."""
        cls.handlers[message_type] = HandlerInfo(handler, payload_type, strict)

    async def dispatch(self, ws: WebSocketLike, raw: str | bytes) -> None:
        """Decode ``raw`` and route it to the appropriate handler."""
        manager = getattr(self, "_hook_manager", None)
        if manager is None:
            manager = HookManager(global_hooks=HookCollection(), resources=(self,))
            self._hook_manager = manager  # type: ignore[attr-defined]
        context = await manager.notify_before_receive(self, ws=ws, raw=raw)
        try:
            await dispatch(self, ws, raw)
        except Exception as exc:
            context.error = exc
            await manager.notify_after_receive(context)
            raise
        await manager.notify_after_receive(context)
