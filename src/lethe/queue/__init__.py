"""Task queue for async job processing."""

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import aiosqlite


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class Task:
    """A task to be processed by the agent."""

    id: str
    chat_id: int
    user_id: int
    message: str
    status: TaskStatus = TaskStatus.PENDING
    result: Optional[str] = None
    error: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "chat_id": self.chat_id,
            "user_id": self.user_id,
            "message": self.message,
            "status": self.status.value,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "metadata": json.dumps(self.metadata),
        }

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "Task":
        return cls(
            id=row["id"],
            chat_id=row["chat_id"],
            user_id=row["user_id"],
            message=row["message"],
            status=TaskStatus(row["status"]),
            result=row["result"],
            error=row["error"],
            created_at=datetime.fromisoformat(row["created_at"]),
            started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
            completed_at=(
                datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None
            ),
            metadata=json.loads(row["metadata"]) if row["metadata"] else {},
        )


class TaskQueue:
    """SQLite-backed async task queue."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None
        self._new_task_event = asyncio.Event()

    async def initialize(self):
        """Initialize the database connection and schema."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row

        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                result TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                metadata TEXT
            )
        """)

        await self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)
        """)

        await self._db.execute("""
            CREATE INDEX IF NOT EXISTS idx_tasks_chat_id ON tasks(chat_id)
        """)

        await self._db.commit()

    async def close(self):
        """Close the database connection."""
        if self._db:
            await self._db.close()
            self._db = None

    async def enqueue(
        self,
        chat_id: int,
        user_id: int,
        message: str,
        metadata: Optional[dict] = None,
    ) -> Task:
        """Add a new task to the queue."""
        task = Task(
            id=str(uuid.uuid4()),
            chat_id=chat_id,
            user_id=user_id,
            message=message,
            metadata=metadata or {},
        )

        await self._db.execute(
            """
            INSERT INTO tasks (id, chat_id, user_id, message, status, created_at, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task.id,
                task.chat_id,
                task.user_id,
                task.message,
                task.status.value,
                task.created_at.isoformat(),
                json.dumps(task.metadata),
            ),
        )
        await self._db.commit()

        # Signal that a new task is available
        self._new_task_event.set()

        return task

    async def dequeue(self, timeout: Optional[float] = None) -> Optional[Task]:
        """Get the next pending task, waiting if necessary."""
        while True:
            # Try to get a pending task
            async with self._db.execute(
                """
                SELECT * FROM tasks
                WHERE status = ?
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (TaskStatus.PENDING.value,),
            ) as cursor:
                row = await cursor.fetchone()

            if row:
                task = Task.from_row(row)
                # Mark as running
                task.status = TaskStatus.RUNNING
                task.started_at = datetime.now(timezone.utc)

                await self._db.execute(
                    """
                    UPDATE tasks SET status = ?, started_at = ?
                    WHERE id = ?
                    """,
                    (task.status.value, task.started_at.isoformat(), task.id),
                )
                await self._db.commit()
                return task

            # No task available, wait for signal or timeout
            self._new_task_event.clear()
            try:
                await asyncio.wait_for(self._new_task_event.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                return None

    async def complete(self, task_id: str, result: str):
        """Mark a task as completed."""
        await self._db.execute(
            """
            UPDATE tasks SET status = ?, result = ?, completed_at = ?
            WHERE id = ?
            """,
            (
                TaskStatus.COMPLETED.value,
                result,
                datetime.now(timezone.utc).isoformat(),
                task_id,
            ),
        )
        await self._db.commit()

    async def fail(self, task_id: str, error: str):
        """Mark a task as failed."""
        await self._db.execute(
            """
            UPDATE tasks SET status = ?, error = ?, completed_at = ?
            WHERE id = ?
            """,
            (
                TaskStatus.FAILED.value,
                error,
                datetime.now(timezone.utc).isoformat(),
                task_id,
            ),
        )
        await self._db.commit()

    async def get_task(self, task_id: str) -> Optional[Task]:
        """Get a task by ID."""
        async with self._db.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return Task.from_row(row) if row else None

    async def get_pending_count(self) -> int:
        """Get the number of pending tasks."""
        async with self._db.execute(
            "SELECT COUNT(*) FROM tasks WHERE status = ?",
            (TaskStatus.PENDING.value,),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0]
