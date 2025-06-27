"""Tests for the @handles_message decorator functionality.

This module contains comprehensive tests for the @handles_message decorator,
which is used to register message handlers for WebSocket resources. The tests
cover various scenarios including:

- Basic decorator functionality and message dispatching
- Error handling for duplicate handlers and invalid signatures
- Inheritance behavior with parent and child resources
- Method overriding with and without decorators
- Type annotation handling for payload parameters
"""

from __future__ import annotations

import typing

import msgspec
import msgspec.json as msgspec_json
import pytest

from falcon_pachinko import WebSocketLike, WebSocketResource, handles_message
from falcon_pachinko.unittests.helpers import DummyWS


class PingPayload(msgspec.Struct):
    """A simple message payload structure for testing ping messages."""

    text: str


class DecoratedResource(WebSocketResource):
    """A WebSocket resource with a decorated message handler for testing."""

    def __init__(self) -> None:
        """Initialize the resource with an empty list to track seen messages."""
        self.seen: list[str] = []

    @handles_message("ping")
    async def handle_ping(self, ws: WebSocketLike, payload: PingPayload) -> None:
        """Handle ping messages by recording the text payload.

        Parameters
        ----------
        ws : WebSocketLike
            The WebSocket connection
        payload : PingPayload
            The ping message payload containing text
        """
        self.seen.append(payload.text)


@pytest.mark.asyncio
async def test_decorator_registers_handler() -> None:
    """Test that the @handles_message decorator properly registers a message handler.

    This test verifies that:
    1. A decorated method is registered as a handler for the specified message type
    2. The handler is correctly invoked when a matching message is dispatched
    3. The payload is properly deserialized and passed to the handler
    """
    r = DecoratedResource()
    raw = msgspec_json.encode({"type": "ping", "payload": {"text": "hi"}})
    await r.dispatch(DummyWS(), raw)
    assert r.seen == ["hi"]


def test_duplicate_handler_raises() -> None:
    """Test that registering duplicate handlers for the same message type raises error.

    This test ensures that attempting to register multiple handlers for the same
    message type results in a RuntimeError with an appropriate error message.
    """
    with pytest.raises(RuntimeError, match="Duplicate handler"):

        class BadResource(WebSocketResource):  # pyright: ignore[reportUnusedClass]
            @handles_message("dup")
            async def h1(self, ws: WebSocketLike, payload: object) -> None: ...

            @handles_message("dup")
            async def h2(self, ws: WebSocketLike, payload: object) -> None: ...


def test_missing_payload_param_raises() -> None:
    """Test that handlers missing the required payload parameter raise a TypeError.

    This test verifies that the decorator validates handler method signatures
    and raises an error when the required payload parameter is missing.
    """
    with pytest.raises(TypeError):

        class BadSig(WebSocketResource):  # pyright: ignore[reportUnusedClass]
            @handles_message("oops")  # pyright: ignore[reportArgumentType]
            async def bad(self, ws: WebSocketLike) -> None: ...


class ParentResource(WebSocketResource):
    """A parent WebSocket resource class with a decorated message handler.

    This class is used to test inheritance behavior of message handlers.
    """

    @handles_message("parent")
    async def parent(self, ws: WebSocketLike, payload: object) -> None:
        """Handle parent messages.

        Parameters
        ----------
        ws : WebSocketLike
            The WebSocket connection
        payload : typing.Any
            The message payload
        """
        ...


class ChildResource(ParentResource):
    """A child WebSocket resource that inherits from ParentResource.

    This class tests that:
    1. Child classes inherit parent message handlers
    2. Child classes can define their own additional handlers
    3. Child classes can override parent handlers without decoration
    """

    def __init__(self) -> None:
        """Initialize the child resource with a list to track invoked handlers."""
        self.invoked: list[str] = []

    @handles_message("child")
    async def child(self, ws: WebSocketLike, payload: object) -> None:
        """Handle child-specific messages.

        Parameters
        ----------
        ws : WebSocketLike
            The WebSocket connection
        payload : typing.Any
            The message payload
        """
        self.invoked.append("child")

    async def parent(self, ws: WebSocketLike, payload: object) -> None:  # pyright: ignore[reportIncompatibleVariableOverride]
        """Override the parent handler to record invocation.

        Parameters
        ----------
        ws : WebSocketLike
            The WebSocket connection
        payload : typing.Any
            The message payload
        """
        # override to record
        self.invoked.append("parent")


class DecoratedOverride(ParentResource):
    """A resource that overrides a parent handler using the decorator.

    This class tests that child classes can override parent handlers
    by re-decorating methods with the same message type.
    """

    @handles_message("parent")
    async def parent(self, ws: WebSocketLike, payload: object) -> None:
        """Override the parent handler with decoration.

        Parameters
        ----------
        ws : WebSocketLike
            The WebSocket connection
        payload : typing.Any
            The message payload
        """
        self.invoked = "decorated"


@pytest.mark.asyncio
async def test_handlers_inherited() -> None:
    """Test that child classes inherit message handlers from parent classes.

    This test verifies that:
    1. Child classes inherit decorated handlers from parent classes
    2. Child classes can define their own additional handlers
    3. Both inherited and child-specific handlers work correctly
    4. Method overrides without decoration still work as handlers
    """
    r = ChildResource()
    await r.dispatch(DummyWS(), msgspec_json.encode({"type": "parent"}))
    await r.dispatch(DummyWS(), msgspec_json.encode({"type": "child"}))
    assert r.invoked == ["parent", "child"]


@pytest.mark.asyncio
async def test_decorated_override() -> None:
    """Test that child classes can override parent handlers using decoration.

    This test verifies that when a child class re-decorates a method with
    the same message type as a parent handler, the child's handler takes
    precedence over the parent's handler.
    """
    r = DecoratedOverride()
    await r.dispatch(DummyWS(), msgspec_json.encode({"type": "parent"}))
    assert r.invoked == "decorated"


def test_unresolved_annotation_is_ignored() -> None:
    """Test that unresolved type annotations are handled gracefully.

    This test verifies that when a handler method has a payload parameter
    with a type annotation that cannot be resolved at runtime, the system
    gracefully handles this by setting the payload type to None in the
    handler registry, rather than raising an error.
    """

    class UnknownAnnoResource(WebSocketResource):
        """A resource with an unresolved payload type annotation."""

        @handles_message("unknown")
        async def handler(
            self,
            ws: WebSocketLike,
            payload: "UnknownPayload",  # type: ignore  # noqa: F821, UP037
        ) -> None:
            """Handle messages with unresolved payload type.

            Parameters
            ----------
            ws : WebSocketLike
                The WebSocket connection
            payload : UnknownPayload
                The message payload with unresolved type
            """
            ...

    assert UnknownAnnoResource.handlers["unknown"][1] is None
