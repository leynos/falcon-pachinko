"""Shared builders for the reference application's lifecycle tests."""

from __future__ import annotations

import typing as typ

import msgspec.json as msjson

from examples.reference_app.resources import TaskStreamResource
from examples.reference_app.services import (
    AnnouncementFeed,
    AuditTrail,
    WorkspaceRepository,
)
from falcon_pachinko.websocket import WebSocketConnectionManager
from tests._stubs import RequestStub

if typ.TYPE_CHECKING:
    import falcon

    from falcon_pachinko.protocols import WebSocketLike

_WORKSPACE = "atlas"
_PROJECT = "triage"


def _build_resource(
    conn_mgr: WebSocketConnectionManager | None = None,
) -> tuple[TaskStreamResource, WorkspaceRepository, AuditTrail, AnnouncementFeed]:
    """Construct a ``TaskStreamResource`` with fresh, real collaborators."""
    repo = WorkspaceRepository()
    audit = AuditTrail()
    feed = AnnouncementFeed()
    resource = TaskStreamResource(
        workspace_repo=repo,
        audit_trail=audit,
        announcement_feed=feed,
        conn_mgr=conn_mgr or WebSocketConnectionManager(),
    )
    return resource, repo, audit, feed


def _connect_request() -> falcon.Request:
    """Return a request double carrying the reference app's user header."""
    stub = RequestStub(
        "/ws/workspaces/atlas/projects/triage/tasks",
        headers={"x-user": "riley"},
    )
    return typ.cast("falcon.Request", stub)


async def _dispatch(
    resource: TaskStreamResource, ws: WebSocketLike, payload: object
) -> None:
    """Bind a standalone hook manager and dispatch an encoded ``payload``."""
    resource.bind_default_hook_manager()
    await resource.dispatch(ws, msjson.encode(payload))
