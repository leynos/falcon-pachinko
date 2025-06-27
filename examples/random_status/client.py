"""WebSocket client for the ``random_status`` demo.

This script connects to the example server and sends a ``status`` message
using the `falcon-pachinko` protocol. Any responses are printed to
standard output.

Dependencies
------------
* websocket-client
"""

from __future__ import annotations

import json
import sys

import websocket


def on_message(ws: websocket.WebSocketApp, message: str) -> None:
    """
    Handle incoming messages from the WebSocket server.

    Prints each received message to standard output, prefixed with "<".
    """
    print("<", message)


def on_open(ws: websocket.WebSocketApp) -> None:
    """
    Send a status message to the WebSocket server when the connection is opened.

    The status text is taken from the first command-line argument, or defaults to "hello" if not provided. The message is sent as a JSON object with "type" set to "status" and "payload" containing the status text.
    """
    status = sys.argv[1] if len(sys.argv) > 1 else "hello"
    msg = json.dumps({"type": "status", "payload": {"text": status}})
    ws.send(msg)


if __name__ == "__main__":
    wsapp = websocket.WebSocketApp(
        "ws://localhost:8000/ws",
        on_open=on_open,
        on_message=on_message,
    )
    wsapp.run_forever()
