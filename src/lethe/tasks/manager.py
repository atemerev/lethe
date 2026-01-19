"""Task manager with SQLite backend."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import aiosqlite

logger = logging.getLogger(__name__)


class TaskStatus(str, Enum):
    """Task lifecycle states."""
    PENDING = "pending"      # Queued, waiting to be picked up
    RUNNING = "running"      # Currently executing
    COMPLETED = "completed"  # Finished successfully
    FAILED = "failed"        # Finished with error
    CANCELLED = "cancelled"  # Cancelled by user/agent


class TaskMode(str, Enum):
    """Execution modes for tasks."""
    SUBAGENT = "subagent"    # Spawn a Letta subagent
    WORKER = "worker"        # Simple local worker (tools only)
    BACKGROUND = "background"  # Letta background mode on current agent


class TaskPriority(str, Enum):
    """Task priorities."""
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


@dataclass
class TaskEvent:
    """An event in a task's lifecycle."""
    id: str
    task_id: str
    event_type: str  # created, started, progress, completed, failed, cancelled
    timestamp: datetime
    data: dict = field(default_factory=dict)
    
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "event_type": self.event_type,
            "timestamp": self.timestamp.isoformat(),
            "data": self.data,
        }
    
    @classmethod
    def from_row(cls, row: dict) -> "TaskEvent":
        return cls(
            id=row["id"],
            task_id=row["task_id"],
            event_type=row["event_type"],
            timestamp=datetime.fromisoformat(row["timestamp"]),
            data=json.loads(row["data"]) if row["data"] else {},
        )


@dataclass
class Task:
    """A background task."""
    id: str
    description: str
    mode: TaskMode
    priority: TaskPriority
    status: TaskStatus
    created_at: datetime
    created_by: str  # "agent" or "user" or specific identifier
    
    # Optional fields
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    result: Optional[str] = None
    error: Optional[str] = None
    progress: Optional[float] = None  # 0.0 to 1.0
    progress_message: Optional[str] = None
    metadata: dict = field(default_factory=dict)
    
    # Runtime fields (not persisted)
    cancel_requested: bool = False
    
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "description": self.description,
            "mode": self.mode.value,
            "priority": self.priority.value,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "created_by": self.created_by,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "result": self.result,
            "error": self.error,
            "progress": self.progress,
            "progress_message": self.progress_message,
            "metadata": self.metadata,
        }
    
    @classmethod
    def from_row(cls, row: dict) -> "Task":
        return cls(
            id=row["id"],
            description=row["description"],
            mode=TaskMode(row["mode"]),
            priority=TaskPriority(row["priority"]),
            status=TaskStatus(row["status"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            created_by=row["created_by"],
            started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
            completed_at=datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None,
            result=row["result"],
            error=row["error"],
            progress=row["progress"],
            progress_message=row["progress_message"],
            metadata=json.loads(row["metadata"]) if row["metadata"] else {},
        )


class TaskManager:
    """Manages background tasks with SQLite persistence."""
    
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None
        self._task_added_event = asyncio.Event()
    
    async def initialize(self):
        """Initialize the database connection and schema."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        
        # Create tables
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                description TEXT NOT NULL,
                mode TEXT NOT NULL,
                priority TEXT NOT NULL DEFAULT 'normal',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                created_by TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                result TEXT,
                error TEXT,
                progress REAL,
                progress_message TEXT,
                metadata TEXT
            );
            
            CREATE TABLE IF NOT EXISTS task_events (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                data TEXT,
                FOREIGN KEY (task_id) REFERENCES tasks(id)
            );
            
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
            CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks(priority);
            CREATE INDEX IF NOT EXISTS idx_task_events_task_id ON task_events(task_id);
        """)
        await self._db.commit()
        logger.info(f"TaskManager initialized with database: {self.db_path}")
    
    async def close(self):
        """Close the database connection."""
        if self._db:
            await self._db.close()
            self._db = None
    
    async def create_task(
        self,
        description: str,
        mode: TaskMode = TaskMode.WORKER,
        priority: TaskPriority = TaskPriority.NORMAL,
        created_by: str = "agent",
        metadata: Optional[dict] = None,
    ) -> Task:
        """Create a new task and add it to the queue."""
        task = Task(
            id=str(uuid.uuid4()),
            description=description,
            mode=mode,
            priority=priority,
            status=TaskStatus.PENDING,
            created_at=datetime.utcnow(),
            created_by=created_by,
            metadata=metadata or {},
        )
        
        await self._db.execute(
            """
            INSERT INTO tasks (id, description, mode, priority, status, created_at, created_by, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task.id,
                task.description,
                task.mode.value,
                task.priority.value,
                task.status.value,
                task.created_at.isoformat(),
                task.created_by,
                json.dumps(task.metadata),
            ),
        )
        
        # Record creation event
        await self._add_event(task.id, "created", {"description": description, "mode": mode.value})
        
        await self._db.commit()
        
        # Signal that a task was added
        self._task_added_event.set()
        
        logger.info(f"Created task {task.id}: {description[:50]}...")
        return task
    
    async def get_task(self, task_id: str) -> Optional[Task]:
        """Get a task by ID."""
        async with self._db.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return Task.from_row(dict(row))
        return None
    
    async def list_tasks(
        self,
        status: Optional[TaskStatus] = None,
        limit: int = 50,
    ) -> list[Task]:
        """List tasks, optionally filtered by status."""
        if status:
            query = "SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC LIMIT ?"
            params = (status.value, limit)
        else:
            query = "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?"
            params = (limit,)
        
        async with self._db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [Task.from_row(dict(row)) for row in rows]
    
    async def get_pending_task(self, timeout: float = 5.0) -> Optional[Task]:
        """Get the next pending task, waiting if none available.
        
        Tasks are prioritized by: priority (urgent > high > normal > low), then created_at.
        """
        priority_order = "CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 WHEN 'low' THEN 3 END"
        
        while True:
            async with self._db.execute(
                f"""
                SELECT * FROM tasks 
                WHERE status = 'pending' 
                ORDER BY {priority_order}, created_at ASC 
                LIMIT 1
                """,
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return Task.from_row(dict(row))
            
            # No pending tasks, wait for one to be added
            self._task_added_event.clear()
            try:
                await asyncio.wait_for(self._task_added_event.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                return None
    
    async def start_task(self, task_id: str) -> bool:
        """Mark a task as started."""
        now = datetime.utcnow()
        result = await self._db.execute(
            """
            UPDATE tasks SET status = ?, started_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (TaskStatus.RUNNING.value, now.isoformat(), task_id),
        )
        await self._add_event(task_id, "started", {})
        await self._db.commit()
        return result.rowcount > 0
    
    async def update_progress(
        self,
        task_id: str,
        progress: float,
        message: Optional[str] = None,
    ):
        """Update task progress (0.0 to 1.0)."""
        await self._db.execute(
            """
            UPDATE tasks SET progress = ?, progress_message = ?
            WHERE id = ?
            """,
            (progress, message, task_id),
        )
        await self._add_event(task_id, "progress", {"progress": progress, "message": message})
        await self._db.commit()
    
    async def complete_task(self, task_id: str, result: Optional[str] = None):
        """Mark a task as completed."""
        now = datetime.utcnow()
        await self._db.execute(
            """
            UPDATE tasks SET status = ?, completed_at = ?, result = ?, progress = 1.0
            WHERE id = ?
            """,
            (TaskStatus.COMPLETED.value, now.isoformat(), result, task_id),
        )
        await self._add_event(task_id, "completed", {"result": result[:200] if result else None})
        await self._db.commit()
        logger.info(f"Task {task_id} completed")
    
    async def fail_task(self, task_id: str, error: str):
        """Mark a task as failed."""
        now = datetime.utcnow()
        await self._db.execute(
            """
            UPDATE tasks SET status = ?, completed_at = ?, error = ?
            WHERE id = ?
            """,
            (TaskStatus.FAILED.value, now.isoformat(), error, task_id),
        )
        await self._add_event(task_id, "failed", {"error": error[:500]})
        await self._db.commit()
        logger.warning(f"Task {task_id} failed: {error[:100]}")
    
    async def cancel_task(self, task_id: str) -> bool:
        """Request cancellation of a task.
        
        For pending tasks, immediately marks as cancelled.
        For running tasks, sets cancel_requested flag (executor should check).
        """
        task = await self.get_task(task_id)
        if not task:
            return False
        
        if task.status == TaskStatus.PENDING:
            # Immediately cancel pending tasks
            now = datetime.utcnow()
            await self._db.execute(
                """
                UPDATE tasks SET status = ?, completed_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (TaskStatus.CANCELLED.value, now.isoformat(), task_id),
            )
            await self._add_event(task_id, "cancelled", {"reason": "user_requested"})
            await self._db.commit()
            logger.info(f"Task {task_id} cancelled (was pending)")
            return True
        
        elif task.status == TaskStatus.RUNNING:
            # For running tasks, we set a flag in metadata
            # The executor should periodically check this
            metadata = task.metadata
            metadata["cancel_requested"] = True
            await self._db.execute(
                "UPDATE tasks SET metadata = ? WHERE id = ?",
                (json.dumps(metadata), task_id),
            )
            await self._add_event(task_id, "cancel_requested", {})
            await self._db.commit()
            logger.info(f"Task {task_id} cancellation requested (is running)")
            return True
        
        return False  # Can't cancel completed/failed/cancelled tasks
    
    async def is_cancellation_requested(self, task_id: str) -> bool:
        """Check if cancellation was requested for a running task."""
        task = await self.get_task(task_id)
        if task:
            return task.metadata.get("cancel_requested", False)
        return False
    
    async def get_task_events(self, task_id: str) -> list[TaskEvent]:
        """Get all events for a task."""
        async with self._db.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY timestamp ASC",
            (task_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [TaskEvent.from_row(dict(row)) for row in rows]
    
    async def _add_event(self, task_id: str, event_type: str, data: dict):
        """Add an event to the task's history."""
        event_id = str(uuid.uuid4())
        await self._db.execute(
            """
            INSERT INTO task_events (id, task_id, event_type, timestamp, data)
            VALUES (?, ?, ?, ?, ?)
            """,
            (event_id, task_id, event_type, datetime.utcnow().isoformat(), json.dumps(data)),
        )
    
    async def get_stats(self) -> dict:
        """Get task statistics."""
        stats = {}
        for status in TaskStatus:
            async with self._db.execute(
                "SELECT COUNT(*) FROM tasks WHERE status = ?", (status.value,)
            ) as cursor:
                row = await cursor.fetchone()
                stats[status.value] = row[0]
        return stats
