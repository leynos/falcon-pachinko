from __future__ import annotations

import typing


class DummyWS:
    async def accept(self, subprotocol: str | None = None) -> None:  # pragma: no cover
        pass

    async def close(self, code: int = 1000) -> None:  # pragma: no cover
        pass

    async def send_media(self, data: typing.Any) -> None:  # pragma: no cover
        pass
