"""Envelope payload validation for both handler selection modes."""

import msgspec as ms
import pytest

from falcon_pachinko import WebSocketLike, WebSocketResource, handles_message
from falcon_pachinko.unittests.helpers import DummyWS


class Payload(ms.Struct):
    """Structured payload used to exercise conversion."""

    value: int


class RecordingResource(WebSocketResource):
    """Record selected handlers and rejected frames."""

    def __init__(self) -> None:
        self.seen: list[object] = []
        self.fallback: list[str | bytes] = []

    async def on_unhandled(self, ws: WebSocketLike, message: str | bytes) -> None:
        """Record the rejected frame.

        Parameters
        ----------
        ws : WebSocketLike
            The connection receiving the frame.
        message : str | bytes
            The original frame.
        """
        self.fallback.append(message)

    async def on_integer(self, ws: WebSocketLike, payload: int) -> None:
        """Record the delivered payload.

        Parameters
        ----------
        ws : WebSocketLike
            The connection receiving the frame.
        payload : object
            The converted payload.
        """
        self.seen.append(payload)

    async def on_struct(self, ws: WebSocketLike, payload: Payload) -> None:
        """Record the delivered payload.

        Parameters
        ----------
        ws : WebSocketLike
            The connection receiving the frame.
        payload : object
            The converted payload.
        """
        self.seen.append(payload)

    async def on_optional(self, ws: WebSocketLike, payload: int | None) -> None:
        """Record the delivered payload.

        Parameters
        ----------
        ws : WebSocketLike
            The connection receiving the frame.
        payload : object
            The converted payload.
        """
        self.seen.append(payload)

    async def on_defaulted(self, ws: WebSocketLike, payload: int = 7) -> None:
        """Record the delivered payload.

        Parameters
        ----------
        ws : WebSocketLike
            The connection receiving the frame.
        payload : object
            The converted payload.
        """
        self.seen.append(payload)

    @handles_message("decorated_integer")
    async def decorated_integer(self, ws: WebSocketLike, payload: int) -> None:
        """Record the delivered payload.

        Parameters
        ----------
        ws : WebSocketLike
            The connection receiving the frame.
        payload : object
            The converted payload.
        """
        self.seen.append(payload)

    @handles_message("decorated_struct")
    async def decorated_struct(self, ws: WebSocketLike, payload: Payload) -> None:
        """Record the delivered payload.

        Parameters
        ----------
        ws : WebSocketLike
            The connection receiving the frame.
        payload : object
            The converted payload.
        """
        self.seen.append(payload)

    @handles_message("decorated_optional")
    async def decorated_optional(self, ws: WebSocketLike, payload: int | None) -> None:
        """Record the delivered payload.

        Parameters
        ----------
        ws : WebSocketLike
            The connection receiving the frame.
        payload : object
            The converted payload.
        """
        self.seen.append(payload)

    @handles_message("decorated_defaulted")
    async def decorated_defaulted(self, ws: WebSocketLike, payload: int = 7) -> None:
        """Record the delivered payload.

        Parameters
        ----------
        ws : WebSocketLike
            The connection receiving the frame.
        payload : object
            The converted payload.
        """
        self.seen.append(payload)

    @handles_message("lenient", strict=False)
    async def lenient(self, ws: WebSocketLike, payload: int) -> None:
        """Record the delivered payload.

        Parameters
        ----------
        ws : WebSocketLike
            The connection receiving the frame.
        payload : object
            The converted payload.
        """
        self.seen.append(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tag",
    [
        "integer",
        "struct",
        "defaulted",
        "decorated_integer",
        "decorated_struct",
        "decorated_defaulted",
        "lenient",
    ],
)
@pytest.mark.parametrize("suffix", ["", ', "payload": null'])
async def test_missing_payload_rejects_required_types(tag: str, suffix: str) -> None:
    """Check the envelope contract for this handler and payload."""
    resource = RecordingResource()
    resource.bind_default_hook_manager()
    raw = f'{{"type": "{tag}"{suffix}}}'
    await resource.dispatch(DummyWS(), raw)
    assert resource.fallback == [raw], "required payloads must be rejected"
    assert not resource.seen, "rejected payloads must not reach handlers"


@pytest.mark.asyncio
@pytest.mark.parametrize("tag", ["optional", "decorated_optional"])
@pytest.mark.parametrize("suffix", ["", ', "payload": null'])
async def test_missing_payload_accepts_optional_types(tag: str, suffix: str) -> None:
    """Check the envelope contract for this handler and payload."""
    resource = RecordingResource()
    resource.bind_default_hook_manager()
    await resource.dispatch(DummyWS(), f'{{"type": "{tag}"{suffix}}}')
    assert resource.seen == [None], "optional payloads must receive None"
    assert not resource.fallback, "valid payloads must not be rejected"


@pytest.mark.asyncio
@pytest.mark.parametrize("tag", ["integer", "decorated_integer"])
async def test_non_null_payload_is_converted(tag: str) -> None:
    """Check the envelope contract for this handler and payload."""
    resource = RecordingResource()
    resource.bind_default_hook_manager()
    await resource.dispatch(DummyWS(), f'{{"type": "{tag}", "payload": 5}}')
    assert resource.seen == [5], "valid integers must reach handlers"
    assert not resource.fallback, "valid payloads must not be rejected"
