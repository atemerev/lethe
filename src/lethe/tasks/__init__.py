"""Task management system for background task execution.

Supports three execution modes:
1. subagent - Spawns a Letta subagent (stateful, has memory, can use tools)
2. worker - Simple local worker (just executes tools, lightweight)
3. background - Uses Letta's background mode on current agent
"""

from lethe.tasks.manager import TaskManager, Task, TaskStatus, TaskMode, TaskPriority, TaskEvent
from lethe.tasks.tools import (
    spawn_task,
    get_tasks,
    get_task_status,
    cancel_task,
    set_task_context,
    clear_task_context,
)

__all__ = [
    "TaskManager",
    "Task",
    "TaskStatus",
    "TaskMode",
    "TaskPriority",
    "TaskEvent",
    "spawn_task",
    "get_tasks",
    "get_task_status", 
    "cancel_task",
    "set_task_context",
    "clear_task_context",
]
