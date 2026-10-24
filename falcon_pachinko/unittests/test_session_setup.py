"""Focused contract tests for the shared WebSocket setup helper."""

from __future__ import annotations

import typing as typ

import pytest

from falcon_pachinko._session import SessionPhase, SessionState
from falcon_pachinko._session_setup import ConnectionSetup
from falcon_pachinko.hooks import HookCollection, HookContext, HookEvent, HookManager
from falcon_pachinko.unittests.helpers import make_req
from falcon_pachinko.unittests.router_session_helpers import (
    SessionResource,
    SessionSocket,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    import falcon


def _setup(
    resource: SessionResource,
    hooks: HookCollection | None = None,
    route_params: dict[str, str] | None = None,
    is_accepted: cabc.Callable[[object], bool] | None = None,
) -> tuple[ConnectionSetup, falcon.Request, SessionSocket]:
    """Build a setup owner with the same dependencies as either router mode."""
    request = typ.cast("falcon.Request", make_req("/session"))
    socket = SessionSocket()
    setup = ConnectionSetup(
        hook_manager=HookManager(
            global_hooks=hooks or HookCollection(), resources=[resource]
        ),
        resource=resource,
        req=request,
        ws=socket,
        route_params=route_params or {},
        is_unaccepted=lambda ws: typ.cast("SessionSocket", ws).unaccepted,
        is_accepted=is_accepted,
    )
    return setup, request, socket


@pytest.mark.asyncio
async def test_prepare_copies_route_params_for_mutable_hooks() -> None:
    """Before-connect hooks can rewrite parameters without mutating the route."""
    resource = SessionResource()
    hooks = HookCollection()
    route_params = {"room": "initial"}

    def rewrite_params(context: HookContext) -> None:
        assert context.params is not None, "before-connect context must expose params"
        context.params["room"] = "rewritten"

    hooks.add(HookEvent.BEFORE_CONNECT, rewrite_params)
    setup, request, socket = _setup(resource, hooks, route_params)
    context = await setup.prepare()

    assert context.req is request, "setup must retain the original request"
    assert context.ws is socket, "setup must retain the prepared websocket"
    assert context.params == {"room": "rewritten"}, (
        "before-connect hooks must be able to rewrite route parameters"
    )
    assert route_params == {"room": "initial"}, "route captures must not be mutated"


@pytest.mark.asyncio
async def test_connect_failure_runs_after_hook_and_preserves_original() -> None:
    """The compatibility setup path retains its original failure behavior."""
    original = RuntimeError("connect failed")

    class FailingResource(SessionResource):
        async def on_connect(self, req: object, ws: object, **params: object) -> bool:
            del req, ws, params
            raise original

    resource = FailingResource()
    contexts: list[HookContext] = []
    hooks = HookCollection()
    hooks.add(HookEvent.AFTER_CONNECT, contexts.append)
    setup, _request, _socket = _setup(resource, hooks)
    await setup.prepare()

    with pytest.raises(RuntimeError) as raised:
        await setup.execute()

    assert raised.value is original, "the original connect failure must propagate"
    assert len(contexts) == 1, "after-connect must run once after connect failure"
    assert contexts[0].error is original, "after-connect must receive the failure"
    assert contexts[0].result is False, "failed setup must report a false result"


@pytest.mark.asyncio
async def test_accepted_connect_and_after_hook_failures_are_primary_first() -> None:
    """The session path groups setup failures only after observed acceptance."""
    original = RuntimeError("connect failed")
    after_error = LookupError("after-connect failed")

    class FailingResource(SessionResource):
        async def on_connect(self, req: object, ws: object, **params: object) -> bool:
            del req, ws, params
            raise original

    resource = FailingResource()
    hooks = HookCollection()

    def fail_after_connect(_context: HookContext) -> None:
        raise after_error

    hooks.add(HookEvent.AFTER_CONNECT, fail_after_connect)
    setup, _request, _socket = _setup(
        resource,
        hooks,
        is_accepted=lambda _ws: True,
    )
    await setup.prepare()

    with pytest.raises(ExceptionGroup) as raised:
        await setup.execute()

    assert raised.value.exceptions == (original, after_error), (
        "the connect failure must precede the after-connect failure"
    )


@pytest.mark.asyncio
async def test_connect_approval_precedes_after_hook_and_router_acceptance() -> None:
    """An approved setup owns cleanup before after-connect hooks run."""
    resource = SessionResource()
    session = SessionState()
    observations: list[SessionPhase] = []
    hooks = HookCollection()
    hooks.add(
        HookEvent.AFTER_CONNECT,
        lambda _context: observations.append(session.phase),
    )
    setup, _request, socket = _setup(resource, hooks)
    setup.on_approval = session.establish
    await setup.prepare()
    await setup.execute()
    await setup.finalize()

    assert observations == [SessionPhase.ESTABLISHED], (
        "approval must establish the session before after-connect hooks"
    )
    assert session.phase is SessionPhase.ESTABLISHED, (
        "approved setup must retain the established session phase"
    )
    assert socket.accepted, "the router must accept an approved unaccepted socket"
