"""Observable contracts for public handler and WebSocket exceptions."""

from falcon_pachinko.exceptions import (
    HandlerNotAsyncError,
    HandlerSignatureError,
    SignatureInspectionError,
)
from falcon_pachinko.websocket import (
    InvalidWebSocketRoutePathError,
    PartialWebSocketInstallError,
    WebSocketConnectionNotFoundError,
    WebSocketResourceNotFoundError,
)


def test_handler_exceptions_keep_their_type_and_message_contracts() -> None:
    """Handler validation failures retain their separate type and messages."""
    signature = HandlerSignatureError("handle_message")
    not_async = HandlerNotAsyncError("ChatResource.handle_message")
    inspection = SignatureInspectionError("ChatResource.handle_message")

    assert isinstance(signature, TypeError), "signature errors must remain TypeError"
    assert signature.args == (
        "Handler handle_message must accept self, ws, and a payload",
    ), "signature validation must preserve its message"
    assert isinstance(not_async, TypeError), (
        "async handler errors must remain TypeError"
    )
    assert not_async.args == ("Handler ChatResource.handle_message must be async",), (
        "the async-handler diagnostic must remain stable"
    )
    assert isinstance(inspection, RuntimeError), (
        "signature inspection errors must remain RuntimeError"
    )
    assert inspection.args == (
        "Cannot inspect signature for handler ChatResource.handle_message",
    ), "the signature-inspection diagnostic must remain stable"


def test_websocket_exceptions_keep_their_type_and_message_contracts() -> None:
    """WebSocket errors preserve base classes and their distinct path formats."""
    partial = PartialWebSocketInstallError()
    invalid_path = InvalidWebSocketRoutePathError("/bad path")
    missing_resource = WebSocketResourceNotFoundError("/missing")
    missing_connection = WebSocketConnectionNotFoundError("connection-7")

    assert isinstance(partial, RuntimeError), (
        "partial installs must remain RuntimeError"
    )
    assert partial.args == ("Partial WebSocket install detected; aborting.",), (
        "the partial-install diagnostic must remain stable"
    )
    assert isinstance(invalid_path, ValueError), "invalid paths must remain ValueError"
    assert invalid_path.args == ("Invalid WebSocket route path: '/bad path'",), (
        "invalid paths must retain their quoted path diagnostic"
    )
    assert isinstance(missing_resource, ValueError), (
        "missing resources must remain ValueError"
    )
    assert missing_resource.args == (
        "No WebSocket resource registered for path: /missing",
    ), "missing-resource errors must preserve their path diagnostic"
    assert isinstance(missing_connection, KeyError), (
        "missing connections must remain KeyError"
    )
    assert missing_connection.args == ("Unknown connection ID: 'connection-7'",), (
        "missing-connection errors must preserve the quoted identifier"
    )
