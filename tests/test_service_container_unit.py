"""Unit tests for the ServiceContainer helper."""

from __future__ import annotations

import typing as typ

import pytest

import falcon_pachinko.di as di
from falcon_pachinko.di import ServiceContainer, ServiceNotFoundError


class _SentinelResource:
    """Example resource used to verify injection behaviour."""

    def __init__(self, *, value: object) -> None:
        self.value = value


def test_register_and_resolve_returns_service() -> None:
    """Registered services should be retrievable by name."""
    container = ServiceContainer()
    sentinel = object()

    container.register("sentinel", sentinel)

    assert container.resolve("sentinel") is sentinel


def test_resolve_missing_raises_service_not_found() -> None:
    """Missing dependencies should raise the dedicated exception."""
    container = ServiceContainer()

    with pytest.raises(ServiceNotFoundError) as excinfo:
        container.resolve("missing")

    assert isinstance(excinfo.value, ServiceNotFoundError)
    assert excinfo.value.name == "missing"


def test_create_resource_injects_registered_dependencies() -> None:
    """Registered keyword dependencies should be injected automatically."""
    container = ServiceContainer()
    sentinel = object()
    container.register("value", sentinel)

    resource = container.create_resource(_SentinelResource)

    assert isinstance(resource, _SentinelResource)
    assert resource.value is sentinel


def test_create_resource_reuses_cached_signatures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Signature reflection should happen once per target callable."""
    container = ServiceContainer()
    container.register("value", object())
    calls: list[object] = []
    original = di.inspect.signature

    def _tracking_signature(target: typ.Callable[..., object]) -> object:
        calls.append(target)
        return original(target)

    monkeypatch.setattr(di.inspect, "signature", _tracking_signature)

    container.create_resource(_SentinelResource)
    container.create_resource(_SentinelResource)

    assert calls.count(_SentinelResource) == 1
