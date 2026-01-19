"""Task tools for the agent to manage background tasks.

Uses contextvars to access the TaskManager from the current context.
"""

from __future__ import annotations

import json
from contextvars import ContextVar
from typing import Any, Optional

from lethe.tasks.manager import TaskManager, TaskMode, TaskPriority, TaskStatus

# Context variable set by worker before tool execution
_task_manager: ContextVar[Optional[TaskManager]] = ContextVar('task_manager', default=None)


def set_task_context(task_manager: TaskManager):
    """Set the task manager context for tool execution."""
    _task_manager.set(task_manager)


def clear_task_context():
    """Clear the task manager context."""
    _task_manager.set(None)


def _get_manager() -> TaskManager:
    """Get the current task manager or raise."""
    manager = _task_manager.get()
    if not manager:
        raise RuntimeError("Task manager context not set. Tasks not available.")
    return manager


# =============================================================================
# Async tool implementations (called by agent)
# =============================================================================

async def spawn_task_async(
    description: str,
    mode: str = "worker",
    priority: str = "normal",
) -> str:
    """Spawn a background task.
    
    Args:
        description: What the task should do (be specific!)
        mode: Execution mode - "worker" (simple), "subagent" (full agent), or "background"
        priority: Task priority - "low", "normal", "high", or "urgent"
    
    Returns:
        JSON with task_id and status
    """
    manager = _get_manager()
    
    # Validate mode
    try:
        task_mode = TaskMode(mode)
    except ValueError:
        return json.dumps({
            "success": False,
            "error": f"Invalid mode '{mode}'. Use: worker, subagent, or background",
        })
    
    # Validate priority
    try:
        task_priority = TaskPriority(priority)
    except ValueError:
        return json.dumps({
            "success": False,
            "error": f"Invalid priority '{priority}'. Use: low, normal, high, or urgent",
        })
    
    task = await manager.create_task(
        description=description,
        mode=task_mode,
        priority=task_priority,
        created_by="agent",
    )
    
    return json.dumps({
        "success": True,
        "task_id": task.id,
        "description": task.description,
        "mode": task.mode.value,
        "priority": task.priority.value,
        "status": task.status.value,
        "message": f"Task created and queued. Use get_task_status('{task.id}') to check progress.",
    })


async def get_tasks_async(
    status: str = "",
    limit: int = 10,
) -> str:
    """Get a list of tasks.
    
    Args:
        status: Filter by status - "pending", "running", "completed", "failed", "cancelled", or "" for all
        limit: Maximum number of tasks to return (default 10)
    
    Returns:
        JSON with list of tasks
    """
    manager = _get_manager()
    
    # Parse status filter
    status_filter = None
    if status:
        try:
            status_filter = TaskStatus(status)
        except ValueError:
            return json.dumps({
                "success": False,
                "error": f"Invalid status '{status}'. Use: pending, running, completed, failed, cancelled",
            })
    
    tasks = await manager.list_tasks(status=status_filter, limit=limit)
    
    # Format tasks for agent consumption
    task_list = []
    for task in tasks:
        task_info = {
            "id": task.id,
            "description": task.description[:100] + ("..." if len(task.description) > 100 else ""),
            "mode": task.mode.value,
            "priority": task.priority.value,
            "status": task.status.value,
            "created_at": task.created_at.isoformat(),
        }
        if task.progress is not None:
            task_info["progress"] = f"{task.progress * 100:.0f}%"
        if task.progress_message:
            task_info["progress_message"] = task.progress_message
        if task.error:
            task_info["error"] = task.error[:100]
        task_list.append(task_info)
    
    # Also get stats
    stats = await manager.get_stats()
    
    return json.dumps({
        "success": True,
        "tasks": task_list,
        "stats": stats,
        "count": len(task_list),
    }, indent=2)


async def get_task_status_async(task_id: str) -> str:
    """Get detailed status of a specific task.
    
    Args:
        task_id: The task ID to check
    
    Returns:
        JSON with detailed task information
    """
    manager = _get_manager()
    
    task = await manager.get_task(task_id)
    if not task:
        return json.dumps({
            "success": False,
            "error": f"Task not found: {task_id}",
        })
    
    # Get events
    events = await manager.get_task_events(task_id)
    
    return json.dumps({
        "success": True,
        "task": task.to_dict(),
        "events": [e.to_dict() for e in events[-10:]],  # Last 10 events
        "event_count": len(events),
    }, indent=2)


async def cancel_task_async(task_id: str) -> str:
    """Cancel a pending or running task.
    
    Args:
        task_id: The task ID to cancel
    
    Returns:
        JSON with cancellation result
    """
    manager = _get_manager()
    
    task = await manager.get_task(task_id)
    if not task:
        return json.dumps({
            "success": False,
            "error": f"Task not found: {task_id}",
        })
    
    if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
        return json.dumps({
            "success": False,
            "error": f"Task already finished with status: {task.status.value}",
        })
    
    cancelled = await manager.cancel_task(task_id)
    
    if cancelled:
        if task.status == TaskStatus.PENDING:
            message = "Task cancelled immediately (was pending)"
        else:
            message = "Cancellation requested (task is running, may take a moment)"
        return json.dumps({
            "success": True,
            "task_id": task_id,
            "message": message,
        })
    else:
        return json.dumps({
            "success": False,
            "error": "Failed to cancel task",
        })


# =============================================================================
# Sync tool stubs (for Letta registration)
# =============================================================================

def _is_tool(func):
    """Decorator to mark a function as a tool."""
    func._is_tool = True
    return func


@_is_tool
def spawn_task(
    description: str,
    mode: str = "worker",
    priority: str = "normal",
) -> str:
    """Spawn a background task to work on something while you continue chatting.
    
    Use this when asked to do something that takes time (research, analysis, etc.).
    The task will run in the background while you remain responsive to the user.
    
    Execution modes:
    - "worker": Simple local execution with tools (fast, lightweight)
    - "subagent": Spawn a full Letta subagent (has memory, more capable)
    - "background": Run on your own context in background mode
    
    Args:
        description: Detailed description of what the task should accomplish
        mode: Execution mode - "worker", "subagent", or "background"
        priority: Task priority - "low", "normal", "high", or "urgent"
    
    Returns:
        JSON with task_id to track progress
    """
    raise Exception("Client-side execution required")


@_is_tool
def get_tasks(status: str = "", limit: int = 10) -> str:
    """Get a list of background tasks.
    
    Use this to check what tasks are pending, running, or completed.
    
    Args:
        status: Filter by status - "pending", "running", "completed", "failed", "cancelled", or "" for all
        limit: Maximum number of tasks to return (default 10)
    
    Returns:
        JSON with list of tasks and statistics
    """
    raise Exception("Client-side execution required")


@_is_tool
def get_task_status(task_id: str) -> str:
    """Get detailed status of a specific task.
    
    Args:
        task_id: The task ID to check
    
    Returns:
        JSON with detailed task info including progress and events
    """
    raise Exception("Client-side execution required")


@_is_tool
def cancel_task(task_id: str) -> str:
    """Cancel a pending or running task.
    
    Pending tasks are cancelled immediately.
    Running tasks will stop at the next checkpoint.
    
    Args:
        task_id: The task ID to cancel
    
    Returns:
        JSON with cancellation result
    """
    raise Exception("Client-side execution required")
