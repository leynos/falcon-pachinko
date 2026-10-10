"""Custom exceptions used by the websocket resource package."""

from __future__ import annotations


class HandlerSignatureError(TypeError):
    """Raised when a handler function has an invalid signature."""

    def __init__(self, func_name: str) -> None:
        super().__init__(f"Handler {func_name} must accept self, ws, and a payload")


class HandlerNotAsyncError(TypeError):
    """Raised when a handler function is not async."""

    def __init__(self, func_qualname: str) -> None:
        super().__init__(f"Handler {func_qualname} must be async")


class SignatureInspectionError(RuntimeError):
    """Raised when a handler's signature can't be inspected."""

    def __init__(self, func_qualname: str) -> None:
        super().__init__(f"Cannot inspect signature for handler {func_qualname}")


class DuplicateHandlerRegistrationError(RuntimeError):
    """Raised when a message handler is registered more than once."""

    pass


class ReservedHandlerRegistrationError(RuntimeError):
    """Raised when a lifecycle callback is registered as a message handler.

    Lifecycle callbacks such as ``on_connect``, ``on_disconnect`` and
    ``on_unhandled`` are driven by the connection lifecycle, not by peer
    frames. Allowing a peer tag to name one would let a remote client invoke
    cleanup on a live connection.
    """

    def __init__(self, message_type: str, func_qualname: str) -> None:
        super().__init__(
            f"Cannot register lifecycle callback {func_qualname} as a message "
            f"handler for {message_type!r}; reserved lifecycle names are not "
            "dispatchable"
        )
