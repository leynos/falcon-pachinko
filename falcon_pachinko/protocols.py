"""Protocol definitions used across the package."""

from __future__ import annotations

import collections.abc as cabc
import typing as typ

from ._disconnect import _PeerDisconnectedError

type AcceptHeaders = cabc.Mapping[str, str] | cabc.Iterable[tuple[str, str]]


def _accept_arguments(
    subprotocol: str | None,
    headers: AcceptHeaders | None,
) -> dict[str, object]:
    """Build generic handshake keywords from the supplied protocol options."""
    kwargs: dict[str, object] = {}
    if subprotocol is not None:
        kwargs["subprotocol"] = subprotocol
    if headers is not None:
        kwargs["headers"] = headers
    return kwargs


class WebSocketLike(typ.Protocol):
    """Minimal interface for a guarded WebSocket connection."""

    async def accept(
        self,
        subprotocol: str | None = None,
        headers: AcceptHeaders | None = None,
    ) -> None:
        """Accept the WebSocket handshake."""

    async def close(self, code: int | None = 1000, reason: str | None = None) -> None:
        """Close the WebSocket connection."""

    async def send_media(self, media: object, /) -> None:
        """Send structured data over the connection."""

    async def send_text(self, message: str) -> None:
        """Send one unencoded text frame over the connection."""

    async def receive_media(self) -> object:
        """Receive structured data from the connection."""

    async def receive_text(self) -> str:
        """Receive one text frame from an accepted connection."""


type DisconnectClassifier = cabc.Callable[
    [BaseException], _PeerDisconnectedError | None
]
type FrameReceiver = cabc.Callable[[WebSocketLike], cabc.Awaitable[str | bytes]]
