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


class DuplicateHandlerRegistrationError(Exception):
    """Raised when attempting to register a duplicate message handler."""

    pass
