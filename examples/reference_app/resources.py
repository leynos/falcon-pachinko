"""WebSocket resources powering the reference application example."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import secrets
import typing as typ

import msgspec as ms

from falcon_pachinko import (
    WebSocketConnectionManager,
    WebSocketLike,
    WebSocketResource,
    handles_message,
)
from falcon_pachinko.hooks import HookContext, HookEvent

from .services import Task, TaskCreationParams

if typ.TYPE_CHECKING:  # pragma: no cover - typing helpers
    import collections.abc as cabc

    import falcon

    from .services import AnnouncementFeed, AuditTrail, WorkspaceRepository

__all__ = [
    "AddTask",
    "AssignTask",
    "BroadcastNote",
    "CompleteTask",
    "ListTasks",
    "ProjectResource",
    "TaskStreamResource",
    "WorkspaceResource",
    "register_reference_hooks",
]


class AddTask(ms.Struct, tag="task.add"):
    """Message that creates a new task inside the active project."""

    task_id: str
    title: str
    assignee: str | None = None


class CompleteTask(ms.Struct, tag="task.complete"):
    """Message that marks an existing task as completed."""

    task_id: str


class AssignTask(ms.Struct, tag="task.assign"):
    """Message that reassigns a task to a different collaborator."""

    task_id: str
    assignee: str


class ListTasks(ms.Struct, tag="task.list"):
    """Message requesting the current task list snapshot."""

    include_completed: bool = True


class BroadcastNote(ms.Struct, tag="session.note"):
    """Session-scoped annotation broadcast to all workspace members."""

    text: str


TaskSchema = AddTask | CompleteTask | AssignTask | ListTasks | BroadcastNote


def _workspace_room(workspace_id: str) -> str:
    return f"workspace:{workspace_id}"


_PENDING_AUDIT_RECORDS = "_reference_pending_audit_records"


def _remember_connection_audit(
    resource: WebSocketResource, record: dict[str, object]
) -> None:
    """Keep setup audit records until a connection ID is available."""
    pending = resource.state.setdefault(_PENDING_AUDIT_RECORDS, [])
    if isinstance(pending, list):
        pending.append(record)


class WorkspaceResource(WebSocketResource):
    """Entry point for workspace-scoped routes."""

    def __init__(
        self,
        *,
        workspace_repo: WorkspaceRepository,
        audit_trail: AuditTrail,
        announcement_feed: AnnouncementFeed,
        conn_mgr: WebSocketConnectionManager,
    ) -> None:
        self._repo = workspace_repo
        self._audit = audit_trail
        self._feed = announcement_feed
        self._conn_mgr = conn_mgr
        self.add_subroute("projects/{project_id}", ProjectResource)

    def get_child_context(self) -> dict[str, object]:
        """Provide constructor kwargs for the nested project resource."""
        return {
            "workspace_repo": self._repo,
            "audit_trail": self._audit,
            "announcement_feed": self._feed,
            "conn_mgr": self._conn_mgr,
        }


class ProjectResource(WebSocketResource):
    """Intermediate resource responsible for project validation."""

    def __init__(
        self,
        *,
        workspace_repo: WorkspaceRepository,
        audit_trail: AuditTrail,
        announcement_feed: AnnouncementFeed,
        conn_mgr: WebSocketConnectionManager,
    ) -> None:
        self._repo = workspace_repo
        self._audit = audit_trail
        self._feed = announcement_feed
        self._conn_mgr = conn_mgr
        self.add_subroute("tasks", TaskStreamResource)

    def get_child_context(self) -> dict[str, object]:
        """Share dependencies with the nested task stream resource."""
        return {
            "workspace_repo": self._repo,
            "audit_trail": self._audit,
            "announcement_feed": self._feed,
            "conn_mgr": self._conn_mgr,
        }


@dc.dataclass(frozen=True, slots=True)
class TaskOperationConfig:
    """Configuration for a task operation handler."""

    repo_operation: cabc.Callable[..., cabc.Awaitable[Task]]
    response_type: str
    payload_builder: cabc.Callable[[Task], dict[str, object]]


@dc.dataclass(frozen=True, slots=True)
class _TaskStreamSession:
    """Carry the connection identity and workspace setup values together."""

    conn_id: str
    workspace_id: str
    project_id: str
    user: str


class TaskStreamResource(WebSocketResource):
    """Final resource that handles the bidirectional task stream."""

    schema = TaskSchema

    def __init__(
        self,
        *,
        workspace_repo: WorkspaceRepository,
        audit_trail: AuditTrail,
        announcement_feed: AnnouncementFeed,
        conn_mgr: WebSocketConnectionManager,
    ) -> None:
        self._repo = workspace_repo
        self._audit = audit_trail
        self._feed = announcement_feed
        self._conn_mgr = conn_mgr
        self._conn_id: str | None = None
        self._complete_config = TaskOperationConfig(
            repo_operation=self._repo.complete_task,
            response_type="task.completed",
            payload_builder=lambda task: {
                "task_id": task.task_id,
                "completed": task.completed,
            },
        )
        self._assign_config = TaskOperationConfig(
            repo_operation=self._repo.assign_task,
            response_type="task.assigned",
            payload_builder=lambda task: {
                "task_id": task.task_id,
                "assignee": task.assigned_to,
            },
        )

    async def on_connect(
        self,
        req: falcon.Request,
        ws: WebSocketLike,
        *,
        workspace_id: str,
        project_id: str,
    ) -> bool:
        """Attach the websocket to the workspace-wide room."""
        session = _TaskStreamSession(
            conn_id=secrets.token_hex(12),
            workspace_id=workspace_id,
            project_id=project_id,
            user=req.get_header("X-User") or req.get_param("user", default="guest"),
        )
        await self._conn_mgr.add_connection(session.conn_id, ws)
        self._conn_id = session.conn_id
        await self._open_session(session, ws)
        return True

    async def _open_session(
        self,
        session: _TaskStreamSession,
        ws: WebSocketLike,
    ) -> None:
        """Prepare the workspace, accept the socket, and publish readiness."""
        is_accepted = False
        try:
            await self._prepare_workspace_session(session)
            is_accepted = await self._accept_session(ws)
            await self._associate_pending_audits(session)
            await self._record_open_session(session)
            await self._send_session_ready(ws, session)
        except BaseException as error:
            await self._rollback_unaccepted_setup(
                session, ws, is_accepted=is_accepted, error=error
            )
            raise

    async def _prepare_workspace_session(self, session: _TaskStreamSession) -> None:
        """Join the workspace and audit setup before the handshake."""
        await self._conn_mgr.join_room(
            session.conn_id, _workspace_room(session.workspace_id)
        )
        self.state.setdefault("workspace_id", session.workspace_id)
        self.state["project_id"] = session.project_id
        self.state["user"] = session.user

    async def _associate_pending_audits(self, session: _TaskStreamSession) -> None:
        """Associate route setup records only after acceptance establishes a session."""
        pending_records = self.state.pop(_PENDING_AUDIT_RECORDS, [])
        if isinstance(pending_records, list):
            await self._audit.associate_connection(pending_records, session.conn_id)

    async def _record_open_session(self, session: _TaskStreamSession) -> None:
        """Record the accepted connection after route audits are associated."""
        await self._audit.record(
            "session.open",
            connection=session.conn_id,
            workspace=session.workspace_id,
            project=session.project_id,
            user=self.state["user"],
        )

    @staticmethod
    async def _accept_session(ws: WebSocketLike) -> bool:
        """Observe acceptance before later setup can fail."""
        await ws.accept()
        return getattr(ws, "unaccepted", False) is False

    @staticmethod
    async def _send_session_ready(
        ws: WebSocketLike,
        session: _TaskStreamSession,
    ) -> None:
        """Publish readiness only after the websocket handshake succeeds."""
        await ws.send_media({
            "type": "session.ready",
            "payload": {
                "workspace": session.workspace_id,
                "project": session.project_id,
            },
        })

    async def _rollback_unaccepted_setup(
        self,
        session: _TaskStreamSession,
        ws: WebSocketLike,
        *,
        is_accepted: bool,
        error: BaseException,
    ) -> None:
        """Roll back unless an accepted router session owns cleanup."""
        was_accepted = is_accepted or getattr(ws, "unaccepted", True) is False
        # The compatibility router returns after setup without a session owner.
        if self.has_router_session and was_accepted:
            return
        try:
            interruption = await self._rollback_connection(session.conn_id)
        # Backends can fail removal for any ordinary reason; retain that error.
        except Exception as removal_error:  # ruff: ignore[blind-except] - retain both
            if isinstance(error, Exception):
                raise ExceptionGroup("rollback", [error, removal_error]) from None
            error.add_note(f"Connection rollback also failed: {removal_error!r}")
            return
        if interruption is not None and not isinstance(error, asyncio.CancelledError):
            raise interruption from error

    async def _rollback_connection(self, conn_id: str) -> asyncio.CancelledError | None:
        """Join pre-accept removal even when the session is cancelled again."""
        # No framework disconnect is promised yet; rollback must finish before
        # the owning task exits, rather than merely outliving it under a shield.
        removal = asyncio.create_task(self._conn_mgr.remove_connection(conn_id))
        interruption: asyncio.CancelledError | None = None
        while not removal.done():
            try:
                await asyncio.shield(removal)
            except asyncio.CancelledError as exc:
                if interruption is None:
                    interruption = exc
            except Exception as removal_error:
                if interruption is not None:
                    raise interruption from removal_error
                raise
        try:
            removal.result()
        except Exception as removal_error:
            if interruption is not None:
                raise interruption from removal_error
            raise
        self._conn_id = None
        return interruption

    async def on_disconnect(self, ws: WebSocketLike, close_code: int) -> None:
        """Remove the websocket from the connection manager on disconnect."""
        if self._conn_id is None:
            return
        conn_id = self._conn_id
        await self._conn_mgr.remove_connection(conn_id)
        self._conn_id = None
        await self._audit.record(
            "session.closed",
            connection=conn_id,
            close_code=close_code,
            workspace=self.state.get("workspace_id"),
            project=self.state.get("project_id"),
        )

    @handles_message("task.add")
    async def handle_add(self, ws: WebSocketLike, payload: AddTask) -> None:
        """Create a task and broadcast a note for the workspace."""
        workspace_id: str = self.state["workspace_id"]
        project_id: str = self.state["project_id"]
        author: str = self.state.get("user", "guest")
        await self._repo.add_task(
            workspace_id,
            project_id,
            TaskCreationParams(
                task_id=payload.task_id,
                title=payload.title,
                author=author,
                assignee=payload.assignee,
            ),
        )
        await ws.send_media({
            "type": "task.added",
            "payload": {
                "task_id": payload.task_id,
                "project_id": project_id,
            },
        })
        await self._feed.publish(
            workspace_id,
            {
                "type": "announcement",
                "payload": {
                    "kind": "task_added",
                    "task_id": payload.task_id,
                    "project_id": project_id,
                },
            },
        )

    @handles_message("task.complete")
    async def handle_complete(self, ws: WebSocketLike, payload: CompleteTask) -> None:
        """Mark a task as complete and acknowledge the caller."""
        await self._execute_task_operation(ws, payload.task_id, self._complete_config)

    @handles_message("task.assign")
    async def handle_assign(self, ws: WebSocketLike, payload: AssignTask) -> None:
        """Reassign a task and echo the new owner."""
        await self._execute_task_operation(
            ws,
            payload.task_id,
            self._assign_config,
            payload.assignee,
        )

    @handles_message("task.list", strict=False)
    async def handle_list(self, ws: WebSocketLike, payload: ListTasks) -> None:
        """Return the current task snapshot."""
        workspace_id: str = self.state["workspace_id"]
        project_id: str = self.state["project_id"]
        tasks = await self._repo.list_tasks(
            workspace_id,
            project_id,
            include_completed=payload.include_completed,
        )
        await ws.send_media({
            "type": "task.list",
            "payload": [dc.asdict(task) for task in tasks],
        })

    @handles_message("session.note")
    async def handle_note(self, ws: WebSocketLike, payload: BroadcastNote) -> None:
        """Publish a manual note to everyone in the workspace."""
        workspace_id: str = self.state["workspace_id"]
        await self._feed.publish(
            workspace_id,
            {
                "type": "announcement",
                "payload": {
                    "kind": "note",
                    "text": payload.text,
                },
            },
        )
        await ws.send_media({"type": "session.note", "payload": payload.text})

    @typ.override
    async def on_unhandled(self, ws: WebSocketLike, message: str | bytes) -> None:
        """Send a helpful error response for unexpected payloads."""
        await ws.send_media({
            "type": "error",
            "payload": "unsupported message",
        })

    async def _execute_task_operation(
        self,
        ws: WebSocketLike,
        task_id: str,
        config: TaskOperationConfig,
        *operation_args: object,
    ) -> None:
        """Execute a task operation and send the standardized response."""
        workspace_id: str = self.state["workspace_id"]
        project_id: str = self.state["project_id"]
        task = await config.repo_operation(
            workspace_id,
            project_id,
            task_id,
            *operation_args,
        )
        await ws.send_media({
            "type": config.response_type,
            "payload": config.payload_builder(task),
        })


async def _seed_workspace(context: HookContext) -> None:
    resource = context.resource
    if not isinstance(resource, WorkspaceResource) or not context.params:
        return
    workspace_id = context.params.get("workspace_id")
    if not isinstance(workspace_id, str):
        return
    workspace = await resource._repo.ensure_workspace(workspace_id)
    resource.state["workspace_id"] = workspace_id
    resource.state["workspace_name"] = workspace.name
    record = await resource._audit.record("workspace.loaded", workspace=workspace_id)
    _remember_connection_audit(resource, record)


async def _seed_project(context: HookContext) -> None:
    resource = context.resource
    if not isinstance(resource, ProjectResource):
        return
    params = context.params or {}
    workspace_id = params.get("workspace_id")
    project_id = params.get("project_id")
    if not isinstance(workspace_id, str) or not isinstance(project_id, str):
        return
    project = await resource._repo.ensure_project(workspace_id, project_id)
    resource.state["workspace_id"] = workspace_id
    resource.state["project_id"] = project_id
    resource.state["project_name"] = project.name
    record = await resource._audit.record(
        "project.loaded", workspace=workspace_id, project=project_id
    )
    _remember_connection_audit(resource, record)


async def _record_receive(context: HookContext) -> None:
    """Store received payload content in the trusted application audit trail.

    This explicit raw-payload storage is application policy, not a safe
    diagnostic. Valid UTF-8 bytes are decoded before storage, but decoding does
    not sanitize their contents.
    """
    resource = context.target
    if not isinstance(resource, TaskStreamResource) or context.raw is None:
        return
    payload = context.raw
    if isinstance(payload, bytes):
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError:
            payload = "<binary>"
    await resource._audit.record(
        "message.received", connection=resource._conn_id, payload=payload
    )


async def _record_receive_result(context: HookContext) -> None:
    resource = context.target
    if not isinstance(resource, TaskStreamResource):
        return
    await resource._audit.record(
        "message.processed",
        connection=resource._conn_id,
        result=context.result,
    )


_HOOKS_REGISTERED = False


def register_reference_hooks() -> None:
    """Attach lifecycle hooks used across the reference example exactly once."""
    global _HOOKS_REGISTERED
    if _HOOKS_REGISTERED:
        return
    WorkspaceResource.hooks.add(HookEvent.BEFORE_CONNECT, _seed_workspace)
    ProjectResource.hooks.add(HookEvent.BEFORE_CONNECT, _seed_project)
    TaskStreamResource.hooks.add(HookEvent.BEFORE_RECEIVE, _record_receive)
    TaskStreamResource.hooks.add(HookEvent.AFTER_RECEIVE, _record_receive_result)
    _HOOKS_REGISTERED = True
