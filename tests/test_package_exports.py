"""Public package exports stay distinct from implementation imports."""

import falcon_pachinko as package
from falcon_pachinko import hooks, protocols, resource


def test_package_reexports_the_supported_hook_and_resource_names() -> None:
    """The package initializer exposes the owning modules' public objects."""
    assert package.HookCollection is hooks.HookCollection, (
        "HookCollection must be re-exported from its owning module"
    )
    assert package.HookContext is hooks.HookContext, (
        "HookContext must be re-exported from its owning module"
    )
    assert package.HookManager is hooks.HookManager, (
        "HookManager must be re-exported from its owning module"
    )
    assert package.WebSocketLike is protocols.WebSocketLike, (
        "WebSocketLike must be re-exported from its owning module"
    )
    assert package.WebSocketResource is resource.WebSocketResource, (
        "WebSocketResource must be re-exported from its owning module"
    )
    assert {
        "HookCollection",
        "HookContext",
        "HookManager",
        "WebSocketLike",
        "WebSocketResource",
    } <= set(package.__all__), "all supported names must appear in package.__all__"
