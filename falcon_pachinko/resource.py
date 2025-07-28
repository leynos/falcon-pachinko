"""WebSocket resource handling and message dispatching."""

from __future__ import annotations

import functools
import inspect
import typing

if typing.TYPE_CHECKING:  # pragma: no cover - imported for type hints
    import collections.abc as cabc
    import re

    import falcon

    from .protocols import WebSocketLike

from .dispatcher import dispatch
from .handlers import Handler, HandlerInfo, _HandlesMessageDescriptor
from .schema import populate_struct_handlers, validate_schema_types


class WebSocketResource:
    """Base class for WebSocket handlers."""

    handlers: typing.ClassVar[dict[str, HandlerInfo]]
    _struct_handlers: typing.ClassVar[dict[type, HandlerInfo]] = {}
    schema: type | None = None

    def add_subroute(
        self,
        path: str,
        resource: type[WebSocketResource] | cabc.Callable[..., WebSocketResource],
        *,
        args: tuple[typing.Any, ...] = (),
        kwargs: dict[str, typing.Any] | None = None,
    ) -> None:
        """Register ``resource`` to handle a nested ``path``."""
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

    @property
    def state(self) -> cabc.MutableMapping[str, typing.Any]:
        """Per-connection state mapping."""
        if not hasattr(self, "_state"):
            self._state = {}
        return self._state

    @state.setter
    def state(self, mapping: cabc.MutableMapping[str, typing.Any]) -> None:
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
                new_handler = typing.cast(
                    "Handler", cls.__dict__[info.handler.__name__]
                )
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
        """Accept or reject the connection after handshake."""
        return True

    async def on_disconnect(self, ws: WebSocketLike, close_code: int) -> None:
        """Handle cleanup when the connection closes."""

    async def on_unhandled(self, ws: WebSocketLike, message: str | bytes) -> None:
        """Fallback handler for unrecognized messages."""

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
        await dispatch(self, ws, raw)
