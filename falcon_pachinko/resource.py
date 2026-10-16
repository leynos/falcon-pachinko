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

import asyncio
import collections.abc as cabc
import functools
import inspect
import typing as typ
from contextlib import asynccontextmanager, suppress

if typ.TYPE_CHECKING:  # pragma: no cover - imported for type hints
    import re

    import falcon

    from .protocols import WebSocketLike

from ._session import SessionPhase
from .dispatcher import dispatch
from .exceptions import ReservedHandlerRegistrationError
from .handlers import (
    Handler,
    HandlerInfo,
    _HandlesMessageDescriptor,
    handler_qualname,
    is_lifecycle_callback,
)
from .hooks import HookCollection, HookManager
from .schema import populate_struct_handlers, validate_schema_types


class WebSocketResource:
    """Base class for WebSocket handlers.

    Subclasses may optionally define a :attr:`schema` attribute referencing a
    :func:`typing.Union` of :class:`msgspec.Struct` types. When provided,
    incoming messages are decoded using this tagged union and dispatched based
    on the message tag. This enables high-performance, schema-driven routing
    without additional boilerplate.

    Message handlers are either registered explicitly with
    :func:`~falcon_pachinko.handles_message` or discovered by naming
    convention. In both cases the lifecycle callbacks ``on_connect``,
    ``on_disconnect`` and ``on_unhandled`` are reserved: peer tags can never
    select them, and registering one as a message handler raises
    :class:`~falcon_pachinko.exceptions.ReservedHandlerRegistrationError`.
    """

    handlers: typ.ClassVar[dict[str, HandlerInfo]]
    _struct_handlers: typ.ClassVar[dict[type, HandlerInfo]] = {}
    #: Attribute names the conventional dispatcher may resolve. Built once per
    #: class by :meth:`_init_conventional_handler_names`; the base class lists
    #: nothing, so ``WebSocketResource`` itself never resolves a peer tag.
    _conventional_handler_names: typ.ClassVar[frozenset[str]] = frozenset()
    schema: type | None = None
    hooks: typ.ClassVar[HookCollection] = HookCollection()
    _hook_manager: HookManager | None

    def bind_hook_manager(self, manager: HookManager) -> None:
        """Associate ``manager`` with this resource instance."""
        self._hook_manager = manager

    @property
    def has_router_session(self) -> bool:
        """Whether a router receive session currently owns this resource.

        This is true from connection setup through disconnect cleanup, including
        while the session is still connecting. The cleanup promise itself begins
        only after ``on_connect`` approves or the resource accepts the socket.
        """
        manager = getattr(self, "_hook_manager", None)
        session = manager.session if manager is not None else None
        return session is not None and session.phase is not SessionPhase.CLOSED

    def bind_default_hook_manager(self) -> HookManager:
        """Bind a standalone manager for direct resource usage."""
        manager = HookManager(global_hooks=HookCollection(), resources=(self,))
        self.bind_hook_manager(manager)
        return manager

    def _require_hook_manager(self) -> HookManager:
        """Return the bound manager or raise a configuration error."""
        manager = getattr(self, "_hook_manager", None)
        if manager is None:
            msg = (
                "HookManager is not bound to this resource. Use "
                "bind_hook_manager() after instantiation or dispatch via a "
                "WebSocketRouter."
            )
            raise RuntimeError(msg)
        return manager

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

        Raises
        ------
        TypeError
            If ``resource`` is not callable.
        ValueError
            If ``path`` compiles to a pattern already registered on this
            resource.
        """
        if kwargs is None:
            kwargs = {}
        if not callable(resource):
            msg = "resource must be callable"
            raise TypeError(msg)

        from ._route_support import _canonical_path
        from ._uri_template import _compile_prefix_template

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

    def get_child_context(self) -> dict[str, object]:  # ruff: ignore[no-self-use] - overridable hook
        """Return kwargs to be forwarded to the next child resource.

        Override this hook to explicitly share context or dependencies with a
        nested resource. The returned mapping is applied to the child's
        constructor. If ``state`` is included, its value will replace the
        connection-scoped ``state`` passed to the child; otherwise, the parent
        state is propagated automatically.

        Returns
        -------
        dict[str, object]
            Keyword arguments for the child resource's constructor. Empty by
            default.
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
        # Probing for __getitem__/__setitem__/__iter__ admitted any subscriptable
        # iterable: a list satisfies all three and was silently accepted as
        # state. MutableMapping registers only genuine mappings.
        if not isinstance(mapping, cabc.MutableMapping):
            msg = f"state must be a MutableMapping, got {type(mapping).__name__}"
            raise TypeError(msg)
        self._state = mapping

    def __init_subclass__(cls, **kwargs: object) -> None:
        """Initialize subclass handler mappings."""
        super().__init_subclass__(**kwargs)
        existing = getattr(cls, "handlers", {})
        handlers = cls._collect_base_handlers()
        handlers.update(existing)
        cls._apply_overrides(handlers)
        cls._validate_handler_registry(handlers)
        cls.handlers = handlers
        cls._init_schema_registry()
        cls._init_conventional_handler_names()

        # Locate the nearest ancestor with a hook registry so that subclasses
        # observe later parent registrations without sharing mutable state.
        parent_hooks = None
        for base in cls.__mro__[1:]:
            candidate = getattr(base, "hooks", None)
            if isinstance(candidate, HookCollection):
                parent_hooks = candidate
                break

        cls.hooks = HookCollection.inherit(parent_hooks)

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
            handler_name = getattr(info.handler, "__name__", None)
            if handler_name and handler_name in shadowed:
                handlers[msg_type] = HandlerInfo(
                    cls.__dict__[handler_name], info.payload_type, info.strict
                )

    @classmethod
    def _init_conventional_handler_names(cls) -> None:
        """Record the ``on_*`` names the conventional dispatcher may resolve.

        The set is fixed when the class is created so that dispatch never
        consults an attribute the class did not intend to expose. Resolution
        walks the MRO, so inheritance is honoured.

        ``getattr_static`` is used rather than ``getattr`` so that a
        descriptor an application happens to name ``on_*`` cannot run, or
        raise, while a resource class is being created. It returns the raw
        attribute, so only a plain coroutine function is admitted:
        ``@staticmethod`` and ``@classmethod`` return the descriptor object
        rather than the function, and both fail the coroutine test. That is
        deliberate: a ``staticmethod`` has no ``self``, so the dispatcher
        cannot pass the resource the registered handler signature promises,
        and a ``classmethod`` receives the class in that position. Both
        shapes are rejected here rather than failing at call time.

        ``functools.partialmethod`` is excluded explicitly. From Python 3.13
        the coroutine test unwraps it, so the raw descriptor would satisfy
        the test while class access yields a generated wrapper rather than
        the function recorded here — admission would then vary with the
        interpreter version. Keeping the family out makes the allowlist
        identical on every supported runtime. The predicate still unwraps it
        for the reserved-name check, so a lifecycle callback cannot hide
        behind one.
        """
        names = {
            name
            for name in dir(cls)
            if name.startswith("on_")
            and not isinstance(
                member := inspect.getattr_static(cls, name, None),
                functools.partialmethod,
            )
            and inspect.iscoroutinefunction(member)
            and not is_lifecycle_callback(cls, member)
        }
        cls._conventional_handler_names = frozenset(names)

    @classmethod
    def _validate_handler_registry(cls, handlers: dict[str, HandlerInfo]) -> None:
        """Reject registered handlers that implement a lifecycle callback."""
        for message_type, info in handlers.items():
            if is_lifecycle_callback(cls, info.handler):
                raise ReservedHandlerRegistrationError(
                    message_type, handler_qualname(info.handler)
                )

    @classmethod
    def _init_schema_registry(cls) -> None:
        cls._struct_handlers = {}
        schema = getattr(cls, "schema", None)
        if schema is None:
            return
        validate_schema_types(schema)
        cls._struct_handlers = populate_struct_handlers(cls)

    async def on_connect(  # ruff: ignore[no-self-use] - overridable hook; subclasses use ``self``
        self, req: falcon.Request, ws: WebSocketLike, **params: object
    ) -> bool:
        """Decide whether the router should accept the WebSocket connection.

        The router calls this during connection setup. The ``after_connect``
        hooks run after this method and before the router calls ``accept()``.
        Use :meth:`on_accepted` for setup that requires an accepted socket. If
        this method sends a frame, explicitly ``await ws.accept()`` first.

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

    async def on_accepted(self, ws: WebSocketLike) -> None:
        """Perform connection setup after the router accepts the socket.

        Parameters
        ----------
        ws : WebSocketLike
            The accepted WebSocket connection.

        Notes
        -----
        The router calls this immediately after acceptance and before its
        receive loop. Perform accept-dependent setup and initial application
        I/O here or in message handlers. :meth:`on_connect` may send only after
        explicitly awaiting ``ws.accept()``.
        """

    async def on_disconnect(self, ws: WebSocketLike, close_code: int) -> None:
        """Handle cleanup for a router-owned WebSocket session.

        With ``WebSocketRouter(receive_loop=True)``, after an accepting
        ``on_connect``, the router attempts this callback once after the
        ``before_disconnect`` hooks. This includes cleanup when
        ``after_connect`` or ``accept`` fails. A peer close code is passed
        through, or 1006 when the peer close code is unavailable or acceptance
        does not complete. An ordinary exception from ``on_accepted`` occurs
        after acceptance: the socket closes with 1011, and cleanup receives
        1011. Cancellation uses 1006 as cleanup metadata and propagates without
        sending a close frame with that reserved code. If cancellation
        interrupts peer-close cleanup, remaining cleanup switches to 1006; a
        callback already invoked is not attempted again. Unexpected receive or
        handler failures close with 1011 and pass 1011 here. These failures
        propagate after cleanup.

        Rejected connections and failed ``on_connect`` calls do not invoke this
        method. Cancellation inside this callback does not cause a second
        callback attempt. With the default ``receive_loop=False``, the
        application owns session reception and cleanup.

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
        """Register ``handler`` for ``message_type``.

        Raises
        ------
        ReservedHandlerRegistrationError
            If ``handler`` implements a reserved lifecycle callback. A
            lifecycle callback is reachable through the connection lifecycle
            only, so a peer tag must never be able to invoke one.
        """
        if is_lifecycle_callback(cls, handler):
            raise ReservedHandlerRegistrationError(
                message_type, handler_qualname(handler)
            )
        cls.handlers[message_type] = HandlerInfo(handler, payload_type, strict)

    async def dispatch(self, ws: WebSocketLike, raw: str | bytes) -> None:
        """Decode ``raw`` and route it to the appropriate handler."""
        manager = self._require_hook_manager()
        if not manager.can_receive(ws):
            return
        async with _receive_hooks(manager, self, ws=ws, raw=raw) as should_dispatch:
            if not should_dispatch:
                return
            await dispatch(self, ws, raw)


@asynccontextmanager
async def _receive_hooks(
    manager: HookManager,
    target: WebSocketResource,
    *,
    ws: WebSocketLike,
    raw: str | bytes,
) -> cabc.AsyncGenerator[bool, None]:
    """Balance before/after receive hooks while guarding original errors."""
    context = await manager.notify_before_receive(target, ws=ws, raw=raw)
    should_dispatch = manager.can_receive(ws)
    try:
        yield should_dispatch
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        context.error = exc
        if manager.can_receive(ws):
            with suppress(Exception):
                await manager.notify_after_receive(context)
        raise
    # Every handler above re-raises, so reaching here means the body succeeded.
    if manager.can_receive(ws):
        await manager.notify_after_receive(context)
