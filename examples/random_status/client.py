import json
import sys

import websocket


def on_message(ws: websocket.WebSocketApp, message: str) -> None:
    print("<", message)


def on_open(ws: websocket.WebSocketApp) -> None:
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
