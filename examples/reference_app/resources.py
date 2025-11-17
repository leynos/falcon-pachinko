"""WebSocket resources powering the reference application example."""

from __future__ import annotations

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


class WorkspaceResource(WebSocketResource):
    """Entry point for workspace-scoped routes."""

    def __init__(
        self,
        *,
        workspace_repo: WorkspaceRepository,
        audit_trail: AuditTrail,
    ) -> None:
        self._repo = workspace_repo
        self._audit = audit_trail
        self.add_subroute("projects/{project_id}", ProjectResource)


class ProjectResource(WebSocketResource):
    """Intermediate resource responsible for project validation."""

    def __init__(
        self,
        *,
        workspace_repo: WorkspaceRepository,
        audit_trail: AuditTrail,
    ) -> None:
        self._repo = workspace_repo
        self._audit = audit_trail
        self.add_subroute("tasks", TaskStreamResource)


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

    async def on_connect(
        self,
        req: "falcon.Request",  # noqa: UP037
        ws: WebSocketLike,
        *,
        workspace_id: str,
        project_id: str,
    ) -> bool:
        """Attach the websocket to the workspace-wide room."""
        conn_id = secrets.token_hex(12)
        await self._conn_mgr.add_connection(conn_id, ws)
        await self._conn_mgr.join_room(conn_id, _workspace_room(workspace_id))
        self._conn_id = conn_id
        self.state.setdefault("workspace_id", workspace_id)
        self.state["project_id"] = project_id
        self.state["user"] = req.get_header("x-user", default="guest")
        await self._audit.record(
            "session.open",
            connection=conn_id,
            workspace=workspace_id,
            project=project_id,
            user=self.state["user"],
        )
        await ws.send_media(
            {
                "type": "session.ready",
                "payload": {
                    "workspace": workspace_id,
                    "project": project_id,
                },
            }
        )
        return True

    async def on_disconnect(self, ws: WebSocketLike, close_code: int) -> None:
        """Remove the websocket from the connection manager on disconnect."""
        if self._conn_id is None:
            return
        await self._conn_mgr.remove_connection(self._conn_id)
        await self._audit.record(
            "session.closed",
            connection=self._conn_id,
            close_code=close_code,
            workspace=self.state.get("workspace_id"),
            project=self.state.get("project_id"),
        )

    @handles_message("task.add")
    async def handle_add(self, ws: WebSocketLike, payload: AddTask) -> None:
        """Create a task and broadcast a note for the workspace."""
        workspace_id = typ.cast("str", self.state["workspace_id"])
        project_id = typ.cast("str", self.state["project_id"])
        await self._repo.add_task(
            workspace_id,
            project_id,
            TaskCreationParams(
                task_id=payload.task_id,
                title=payload.title,
                author=typ.cast("str", self.state.get("user", "guest")),
                assignee=payload.assignee,
            ),
        )
        await ws.send_media(
            {
                "type": "task.added",
                "payload": {
                    "task_id": payload.task_id,
                    "project_id": project_id,
                },
            }
        )
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
        await self._execute_task_operation(
            ws,
            payload.task_id,
            self._repo.complete_task,
            "task.completed",
            lambda task: {"task_id": task.task_id, "completed": task.completed},
        )

    @handles_message("task.assign")
    async def handle_assign(self, ws: WebSocketLike, payload: AssignTask) -> None:
        """Reassign a task and echo the new owner."""
        await self._execute_task_operation(
            ws,
            payload.task_id,
            self._repo.assign_task,
            "task.assigned",
            lambda task: {"task_id": task.task_id, "assignee": task.assigned_to},
            payload.assignee,
        )

    @handles_message("task.list", strict=False)
    async def handle_list(self, ws: WebSocketLike, payload: ListTasks) -> None:
        """Return the current task snapshot."""
        workspace_id = typ.cast("str", self.state["workspace_id"])
        project_id = typ.cast("str", self.state["project_id"])
        tasks = await self._repo.list_tasks(
            workspace_id,
            project_id,
            include_completed=payload.include_completed,
        )
        await ws.send_media(
            {
                "type": "task.list",
                "payload": [dc.asdict(task) for task in tasks],
            }
        )

    @handles_message("session.note")
    async def handle_note(self, ws: WebSocketLike, payload: BroadcastNote) -> None:
        """Publish a manual note to everyone in the workspace."""
        workspace_id = typ.cast("str", self.state["workspace_id"])
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

    async def on_unhandled(self, ws: WebSocketLike, message: str | bytes) -> None:
        """Send a helpful error response for unexpected payloads."""
        await ws.send_media(
            {
                "type": "error",
                "payload": "unsupported message",
            }
        )

    async def _execute_task_operation(
        self,
        ws: WebSocketLike,
        task_id: str,
        repo_operation: typ.Callable[..., Task],
        response_type: str,
        payload_builder: typ.Callable[[Task], dict[str, object]],
        *operation_args: object,
    ) -> None:
        workspace_id = typ.cast("str", self.state["workspace_id"])
        project_id = typ.cast("str", self.state["project_id"])
        task = await repo_operation(
            workspace_id,
            project_id,
            task_id,
            *operation_args,
        )
        await ws.send_media(
            {
                "type": response_type,
                "payload": payload_builder(task),
            }
        )


async def _seed_workspace(context: HookContext) -> None:
    resource = typ.cast("WorkspaceResource", context.resource)
    if not context.params:
        return
    workspace_id = typ.cast("str", context.params.get("workspace_id"))
    workspace = await resource._repo.ensure_workspace(workspace_id)
    resource.state["workspace_id"] = workspace_id
    resource.state["workspace_name"] = workspace.name
    await resource._audit.record("workspace.loaded", workspace=workspace_id)


async def _seed_project(context: HookContext) -> None:
    resource = typ.cast("ProjectResource", context.resource)
    params = context.params or {}
    workspace_id = typ.cast("str", params.get("workspace_id"))
    project_id = typ.cast("str", params.get("project_id"))
    project = await resource._repo.ensure_project(workspace_id, project_id)
    resource.state["workspace_id"] = workspace_id
    resource.state["project_id"] = project_id
    resource.state["project_name"] = project.name
    await resource._audit.record(
        "project.loaded", workspace=workspace_id, project=project_id
    )


async def _record_receive(context: HookContext) -> None:
    resource = typ.cast("TaskStreamResource", context.target)
    if context.raw is None:
        return
    payload = context.raw
    if isinstance(payload, bytes):
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError:
            payload = "<binary>"
    await resource._audit.record("message.received", payload=payload)


async def _record_receive_result(context: HookContext) -> None:
    resource = typ.cast("TaskStreamResource", context.target)
    await resource._audit.record("message.processed", result=context.result)


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


register_reference_hooks()
