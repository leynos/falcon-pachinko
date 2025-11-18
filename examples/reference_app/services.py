"""Support services used by the reference application example."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

__all__ = [
    "AnnouncementFeed",
    "AuditTrail",
    "AuthenticationError",
    "Project",
    "Task",
    "TaskCreationParams",
    "TokenAuthenticator",
    "Workspace",
    "WorkspaceRepository",
]


@dc.dataclass(slots=True)
class Task:
    """Represents a single task tracked inside a project."""

    task_id: str
    title: str
    created_by: str
    assigned_to: str | None = None
    completed: bool = False


@dc.dataclass(slots=True)
class TaskCreationParams:
    """Parameter object capturing task creation details."""

    task_id: str
    title: str
    author: str
    assignee: str | None = None


@dc.dataclass(slots=True)
class Project:
    """Aggregate of tasks grouped under a logical project."""

    project_id: str
    name: str
    tasks: dict[str, Task]


@dc.dataclass(slots=True)
class Workspace:
    """Collection of projects owned by a workspace."""

    workspace_id: str
    name: str
    projects: dict[str, Project]


class WorkspaceRepository:
    """In-memory repository coordinating workspaces, projects, and tasks."""

    def __init__(self) -> None:
        self._workspaces: dict[str, Workspace] = {}
        self._lock = asyncio.Lock()

    async def ensure_workspace(self, workspace_id: str) -> Workspace:
        """Return an existing workspace or create a new one."""
        async with self._lock:
            return self._get_or_create_workspace(workspace_id)

    async def ensure_project(self, workspace_id: str, project_id: str) -> Project:
        """Return a project, creating it and its parent workspace if needed."""
        async with self._lock:
            workspace = self._get_or_create_workspace(workspace_id)
            return self._get_or_create_project(workspace, project_id)

    async def add_task(
        self,
        workspace_id: str,
        project_id: str,
        params: TaskCreationParams,
    ) -> Task:
        """Store a new task and return it."""
        async with self._lock:
            project = self._ensure_project_locked(workspace_id, project_id)
            task = Task(
                task_id=params.task_id,
                title=params.title,
                created_by=params.author,
                assigned_to=params.assignee,
            )
            project.tasks[task.task_id] = task
            return task

    async def complete_task(
        self, workspace_id: str, project_id: str, task_id: str
    ) -> Task:
        """Mark ``task_id`` as completed and return it."""
        async with self._lock:
            project = self._ensure_project_locked(workspace_id, project_id)
            task = project.tasks[task_id]
            task.completed = True
            return task

    async def assign_task(
        self, workspace_id: str, project_id: str, task_id: str, assignee: str
    ) -> Task:
        """Assign ``task_id`` to ``assignee`` and return it."""
        async with self._lock:
            project = self._ensure_project_locked(workspace_id, project_id)
            task = project.tasks[task_id]
            task.assigned_to = assignee
            return task

    async def list_tasks(
        self,
        workspace_id: str,
        project_id: str,
        *,
        include_completed: bool = True,
    ) -> list[Task]:
        """Return a snapshot of tasks filtered by completion state."""
        async with self._lock:
            project = self._ensure_project_locked(workspace_id, project_id)
            tasks = list(project.tasks.values())

        if include_completed:
            return [dc.replace(task) for task in tasks]
        return [dc.replace(task) for task in tasks if not task.completed]

    async def snapshot(self) -> dict[str, Workspace]:
        """Return a deep-ish copy of the repository contents for inspection."""
        async with self._lock:
            return {
                wid: Workspace(
                    workspace_id=workspace.workspace_id,
                    name=workspace.name,
                    projects={
                        pid: Project(
                            project_id=project.project_id,
                            name=project.name,
                            tasks={
                                tid: dc.replace(task)
                                for tid, task in project.tasks.items()
                            },
                        )
                        for pid, project in workspace.projects.items()
                    },
                )
                for wid, workspace in self._workspaces.items()
            }

    def _get_or_create_workspace(self, workspace_id: str) -> Workspace:
        workspace = self._workspaces.get(workspace_id)
        if workspace is None:
            workspace = Workspace(
                workspace_id=workspace_id,
                name=workspace_id.replace("-", " ").title(),
                projects={},
            )
            self._workspaces[workspace_id] = workspace
        return workspace

    def _get_or_create_project(self, workspace: Workspace, project_id: str) -> Project:
        project = workspace.projects.get(project_id)
        if project is None:
            project = Project(
                project_id=project_id,
                name=project_id.replace("-", " ").title(),
                tasks={},
            )
            workspace.projects[project_id] = project
        return project

    def _ensure_project_locked(self, workspace_id: str, project_id: str) -> Project:
        workspace = self._get_or_create_workspace(workspace_id)
        return self._get_or_create_project(workspace, project_id)


class AuditTrail:
    """Simple async-friendly audit recorder used by the hooks."""

    def __init__(self) -> None:
        self._records: list[dict[str, object]] = []
        self._lock = asyncio.Lock()

    @property
    def records(self) -> list[dict[str, object]]:
        """Return a copy of the recorded events."""
        return list(self._records)

    async def record(self, event: str, **metadata: object) -> None:
        """Append an audit entry describing ``event``."""
        async with self._lock:
            self._records.append({"event": event, "metadata": metadata})


class AnnouncementFeed:
    """Async queue used by workers to broadcast state changes."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[tuple[str, dict[str, object]]] = asyncio.Queue()

    async def publish(self, workspace_id: str, payload: dict[str, object]) -> None:
        """Publish ``payload`` for ``workspace_id`` subscribers."""
        await self._queue.put((workspace_id, payload))

    async def next_event(self) -> tuple[str, dict[str, object]]:
        """Wait for the next announcement event."""
        return await self._queue.get()


class AuthenticationError(PermissionError):
    """Raised when a connection presents invalid credentials."""

    def __init__(self, workspace_id: str) -> None:
        self.workspace_id = workspace_id
        super().__init__(f"invalid token for workspace {workspace_id!r}")


class TokenAuthenticator:
    """Trivial header-based authenticator wired into global hooks."""

    def __init__(self, secrets: typ.Mapping[str, str]) -> None:
        self._secrets = dict(secrets)

    async def verify(self, workspace_id: str, token: str | None) -> None:
        """Ensure ``token`` matches the configured secret for the workspace."""
        expected = self._secrets.get(workspace_id)
        if expected is None:
            return
        if token != expected:
            raise AuthenticationError(workspace_id)
        return
