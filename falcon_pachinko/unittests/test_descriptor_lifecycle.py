"""Coroutine-valued descriptor families cannot launder a lifecycle callback.

``on_connect``, ``on_disconnect`` and ``on_unhandled`` are reserved: no peer
tag may select them, and no registration path may put them or an alias of
them into the peer-reachable handler registry. The sibling suites cover the
``classmethod``/``staticmethod`` case and the plain-name and ``partial``
cases; this module covers the remaining standard-library descriptor families
that hold a coroutine but are not themselves one:

- ``functools.partialmethod``
- ``functools.singledispatchmethod``
- ``functools.cached_property``
- ``property``

Each can hide the lifecycle implementation behind a class-dictionary object
while a *plain* alias of the same coroutine sits under a neutral ``on_*``
name. The alias passes the coroutine test and the reserved-name check, so
only an identity comparison that peels the descriptor can connect it back to
the reserved entry and refuse it. ``functools.partialmethod`` is worse than
the rest: from Python 3.13 ``inspect.iscoroutinefunction`` unwraps it, so the
raw descriptor passes the conventional allowlist's admission test on those
interpreters — the reserved name would be dispatchable outright.

Both API channels are exercised, because the reserved check guards both:

- the conventional dispatcher, whose allowlist is built at class creation,
  and
- manual registration through ``add_handler``.

The shapes live together here because they share one cause (a descriptor
family the unwrapping helper must know about) and one fix: the helper's
family list. Adding a family means adding its rows to the tables below.
"""

from __future__ import annotations

import functools
import typing as typ

import msgspec.json as msjson
import pytest

from falcon_pachinko import WebSocketLike, WebSocketResource
from falcon_pachinko.exceptions import ReservedHandlerRegistrationError
from falcon_pachinko.handlers import is_lifecycle_callback
from falcon_pachinko.unittests.helpers import DummyWS

if typ.TYPE_CHECKING:
    import collections.abc as cabc

# The recorder is module-level so the *same* coroutine object can be bound as
# a descriptor under the reserved name and aliased as a plain function under
# a neutral one. An inline ``async def`` would give the two entries distinct
# objects, and the identity comparison would then have nothing to connect.
#
# The trailing parameters carry defaults because ``property`` and
# ``functools.cached_property`` are typed as one-argument getters. Defaults
# keep the recorder dispatch-callable — the dispatcher passes the connection
# and close code positionally — while satisfying that signature, so the
# doubles do not need a suppression to instance-check. A direct getter call
# would see the default, but these tests never make one.
descriptor_calls: list[int | None] = []


async def _record_descriptor_close(  # ruff: ignore[unused-async]  # lifecycle must be awaitable
    self: object,
    ws: WebSocketLike | None = None,
    close_code: int | None = None,
) -> None:
    """Record the close code a descriptor-wrapped lifecycle supplied."""
    descriptor_calls.append(close_code)


class _FallbackRecorder(WebSocketResource):
    """Record the frames that reach the fallback instead of a handler.

    Every resource below differs only in how it declares ``on_disconnect``,
    so the fallback recording lives here once.
    """

    fallback: list[str | bytes]

    def __init__(self) -> None:
        self.fallback = []

    async def on_unhandled(self, ws: WebSocketLike, message: str | bytes) -> None:
        """Record the original frame handed to the fallback."""
        self.fallback.append(message)


class PartialmethodLifecycleResource(_FallbackRecorder):
    """Define the lifecycle as a ``functools.partialmethod``, aliased plainly.

    The neutral ``on_bye`` alias holds the same coroutine the reserved entry
    wraps. On Python 3.13 and later the raw descriptor itself satisfies the
    coroutine test, so ``on_disconnect`` would also be admitted to the
    allowlist unless the predicate unwraps it.
    """

    on_disconnect = functools.partialmethod(_record_descriptor_close)
    on_bye = _record_descriptor_close


class InheritedPartialmethodLifecycleResource(PartialmethodLifecycleResource):
    """Child inheriting a ``partialmethod`` lifecycle and its plain alias.

    The child defines neither entry, so the MRO walk has to unwrap the
    parent's descriptor before the identity comparison can connect the
    inherited alias to the lifecycle implementation.
    """


class SingledispatchLifecycleResource(_FallbackRecorder):
    """Define the lifecycle as a ``functools.singledispatchmethod``."""

    on_disconnect = functools.singledispatchmethod(_record_descriptor_close)
    on_bye = _record_descriptor_close


class CachedPropertyLifecycleResource(_FallbackRecorder):
    """Define the lifecycle as a ``functools.cached_property``."""

    on_disconnect = functools.cached_property(_record_descriptor_close)
    on_bye = _record_descriptor_close


class PropertyLifecycleResource(_FallbackRecorder):
    """Define the lifecycle as a ``property``, aliased plainly."""

    on_disconnect = property(_record_descriptor_close)
    on_bye = _record_descriptor_close


# Each row is one resource class and one peer tag. ``bye`` and its case
# variants exercise the neutral alias under the reserved callback's own
# coroutine; ``disconnect`` exercises the reserved name after the convention
# normalises it. ``partialmethod`` carries the extra case variants and the
# inherited row because it is the family that also escapes the allowlist's
# admission test on Python 3.13 and later.
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resource_cls", "tag"),
    [
        (PartialmethodLifecycleResource, "bye"),
        (PartialmethodLifecycleResource, "Bye"),
        (PartialmethodLifecycleResource, "bYe"),
        (PartialmethodLifecycleResource, "disconnect"),
        (InheritedPartialmethodLifecycleResource, "bye"),
        (SingledispatchLifecycleResource, "bye"),
        (SingledispatchLifecycleResource, "disconnect"),
        (CachedPropertyLifecycleResource, "bye"),
        (CachedPropertyLifecycleResource, "disconnect"),
        (PropertyLifecycleResource, "bye"),
        (PropertyLifecycleResource, "disconnect"),
    ],
    ids=[
        "partialmethod-bye",
        "partialmethod-Bye",
        "partialmethod-bYe",
        "partialmethod-disconnect",
        "inherited-partialmethod-bye",
        "singledispatchmethod-bye",
        "singledispatchmethod-disconnect",
        "cached_property-bye",
        "cached_property-disconnect",
        "property-bye",
        "property-disconnect",
    ],
)
async def test_descriptor_lifecycle_is_not_peer_selectable(
    resource_cls: type[_FallbackRecorder], tag: str
) -> None:
    """A descriptor-wrapped lifecycle stays unreachable from a peer frame.

    The frame must reach the fallback with its original bytes, and the
    lifecycle implementation must record no close code.
    """
    descriptor_calls.clear()
    r = resource_cls()
    r.bind_default_hook_manager()
    raw = msjson.encode({"type": tag, "payload": 1000})
    await r.dispatch(DummyWS(), raw)
    assert not descriptor_calls, (
        f"{resource_cls.__name__} must not run on_disconnect for {tag!r}: "
        f"{descriptor_calls}"
    )
    assert r.fallback == [raw], f"{resource_cls.__name__} must fall back for {tag!r}"


def test_add_handler_rejects_raw_partialmethod_descriptor() -> None:
    """The ``partialmethod`` descriptor itself is refused.

    The descriptor stored in the class dictionary is not the coroutine and
    its ``__name__`` is not reserved, so only unwrapping ``func`` connects it
    to the lifecycle implementation the MRO walk holds. On Python 3.13 and
    later it also passes the coroutine test itself, so the admission test
    would otherwise let the reserved name through outright.
    """
    with pytest.raises(
        ReservedHandlerRegistrationError, match="_record_descriptor_close"
    ):
        PartialmethodLifecycleResource.add_handler(
            "bye",
            PartialmethodLifecycleResource.__dict__["on_disconnect"],
            strict=False,
        )


def test_add_handler_rejects_bound_partialmethod_view() -> None:
    """The generated class view of a ``partialmethod`` is refused.

    Reading ``on_disconnect`` through the class yields a ``_method`` wrapper
    (a fresh one on each read from Python 3.14) whose name is not reserved;
    only following its backlink to the descriptor, then to ``func``, reaches
    the lifecycle implementation.
    """
    with pytest.raises(
        ReservedHandlerRegistrationError, match="_record_descriptor_close"
    ):
        PartialmethodLifecycleResource.add_handler(
            "bye", PartialmethodLifecycleResource.on_disconnect, strict=False
        )


def test_add_handler_rejects_inherited_partialmethod_alias() -> None:
    """A child cannot register the alias of the lifecycle it inherited.

    The child defines neither entry, so the MRO walk has to unwrap the
    parent's descriptor before the identity comparison can connect the
    inherited alias to the reserved implementation.
    """
    with pytest.raises(
        ReservedHandlerRegistrationError, match="_record_descriptor_close"
    ):
        InheritedPartialmethodLifecycleResource.add_handler(
            "bye", InheritedPartialmethodLifecycleResource.on_bye, strict=False
        )


@pytest.mark.parametrize(
    "wrapper",
    [
        functools.partialmethod,
        functools.singledispatchmethod,
        functools.cached_property,
        property,
    ],
    ids=["partialmethod", "singledispatchmethod", "cached_property", "property"],
)
def test_add_handler_rejects_neutral_alias_of_descriptor_lifecycle(
    wrapper: cabc.Callable[[typ.Any], typ.Any],
) -> None:
    """A neutrally named alias behind any wrapped family is refused.

    ``on_bye`` is an ordinary coroutine with an ordinary name, so only
    identity against the attribute the descriptor family holds (``func`` or
    ``fget``) can refuse it. Registering it would put the live connection's
    disconnect handler in the peer-reachable registry under the tag ``bye``.
    """

    class DescriptorResource(WebSocketResource):
        """Resource aliasing the coroutine behind a descriptor family."""

        on_disconnect = wrapper(_record_descriptor_close)
        on_bye = _record_descriptor_close

    with pytest.raises(
        ReservedHandlerRegistrationError, match="_record_descriptor_close"
    ):
        DescriptorResource.add_handler("bye", DescriptorResource.on_bye)


def test_lifecycle_predicate_recognises_every_descriptor_family() -> None:
    """The predicate recognises every coroutine-valued descriptor family.

    Each entry is genuinely the lifecycle callback of its owner: the reserved
    entry is the descriptor the class dictionary holds, and the neutral entry
    is the plain coroutine the descriptor wraps. A descriptor object is not
    the coroutine itself, so only peeling the family reveals that the
    implementation is the same object the neutrally named alias holds.
    """

    class PartialmethodLifecycle(WebSocketResource):
        """Define the callback as a ``functools.partialmethod``."""

        on_disconnect = functools.partialmethod(_record_descriptor_close)
        on_bye = _record_descriptor_close

    class SingledispatchLifecycle(WebSocketResource):
        """Define the callback as a ``functools.singledispatchmethod``."""

        on_disconnect = functools.singledispatchmethod(_record_descriptor_close)
        on_bye = _record_descriptor_close

    class CachedPropertyLifecycle(WebSocketResource):
        """Define the callback as a ``functools.cached_property``."""

        on_disconnect = functools.cached_property(_record_descriptor_close)
        on_bye = _record_descriptor_close

    class PropertyLifecycle(WebSocketResource):
        """Define the callback as a ``property``."""

        on_disconnect = property(_record_descriptor_close)
        on_bye = _record_descriptor_close

    # Each shape is checked against the owner that actually carries it: the
    # descriptor aliases only exist on their own classes, and asking a
    # different class to recognise them would test the wrong MRO.
    rejected = {
        "partialmethod descriptor": (
            PartialmethodLifecycle,
            PartialmethodLifecycle.__dict__["on_disconnect"],
        ),
        "partialmethod alias": (
            PartialmethodLifecycle,
            PartialmethodLifecycle.__dict__["on_bye"],
        ),
        "singledispatchmethod descriptor": (
            SingledispatchLifecycle,
            SingledispatchLifecycle.__dict__["on_disconnect"],
        ),
        "singledispatchmethod alias": (
            SingledispatchLifecycle,
            SingledispatchLifecycle.__dict__["on_bye"],
        ),
        "cached_property descriptor": (
            CachedPropertyLifecycle,
            CachedPropertyLifecycle.__dict__["on_disconnect"],
        ),
        "cached_property alias": (
            CachedPropertyLifecycle,
            CachedPropertyLifecycle.__dict__["on_bye"],
        ),
        "property descriptor": (
            PropertyLifecycle,
            PropertyLifecycle.__dict__["on_disconnect"],
        ),
        "property alias": (PropertyLifecycle, PropertyLifecycle.__dict__["on_bye"]),
    }
    for label, (owner, callable_) in rejected.items():
        assert is_lifecycle_callback(owner, callable_), (
            f"{label} must be recognised as a lifecycle callback"
        )
