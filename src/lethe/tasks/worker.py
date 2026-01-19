"""Task worker that processes background tasks.

Supports three execution modes:
1. worker - Simple local execution using tool handlers
2. subagent - Spawns a Letta subagent for complex tasks
3. background - Uses Letta background mode on the main agent
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

from letta_client import AsyncLetta

from lethe.tasks.manager import TaskManager, Task, TaskMode, TaskStatus

logger = logging.getLogger(__name__)


class TaskWorker:
    """Processes background tasks from the TaskManager."""
    
    def __init__(
        self,
        task_manager: TaskManager,
        letta_client: AsyncLetta,
        main_agent_id: str,
        tool_handlers: dict[str, Callable],
        on_task_complete: Optional[Callable[[Task], Any]] = None,
    ):
        """Initialize the task worker.
        
        Args:
            task_manager: The TaskManager instance
            letta_client: AsyncLetta client for subagent/background modes
            main_agent_id: The main agent's ID (for background mode)
            tool_handlers: Map of tool name to handler function (for worker mode)
            on_task_complete: Optional callback when a task completes
        """
        self.task_manager = task_manager
        self.client = letta_client
        self.main_agent_id = main_agent_id
        self.tool_handlers = tool_handlers
        self.on_task_complete = on_task_complete
        self._running = False
        self._current_task: Optional[Task] = None
    
    async def start(self):
        """Start the task worker loop."""
        self._running = True
        logger.info("TaskWorker started")
        
        while self._running:
            try:
                # Wait for a task
                task = await self.task_manager.get_pending_task(timeout=5.0)
                
                if task is None:
                    continue  # Timeout, check if still running
                
                # Mark as started
                if not await self.task_manager.start_task(task.id):
                    logger.warning(f"Task {task.id} was already taken")
                    continue
                
                self._current_task = task
                logger.info(f"Processing task {task.id} ({task.mode.value}): {task.description[:50]}...")
                
                try:
                    # Execute based on mode
                    if task.mode == TaskMode.WORKER:
                        result = await self._execute_worker_mode(task)
                    elif task.mode == TaskMode.SUBAGENT:
                        result = await self._execute_subagent_mode(task)
                    elif task.mode == TaskMode.BACKGROUND:
                        result = await self._execute_background_mode(task)
                    else:
                        raise ValueError(f"Unknown task mode: {task.mode}")
                    
                    # Mark complete
                    await self.task_manager.complete_task(task.id, result)
                    
                    # Callback
                    if self.on_task_complete:
                        task.result = result
                        task.status = TaskStatus.COMPLETED
                        await self.on_task_complete(task)
                    
                except asyncio.CancelledError:
                    await self.task_manager.cancel_task(task.id)
                    raise
                except Exception as e:
                    logger.exception(f"Task {task.id} failed: {e}")
                    await self.task_manager.fail_task(task.id, str(e))
                finally:
                    self._current_task = None
                
            except asyncio.CancelledError:
                logger.info("TaskWorker cancelled")
                break
            except Exception as e:
                logger.exception(f"TaskWorker error: {e}")
                await asyncio.sleep(1)  # Prevent tight error loop
        
        logger.info("TaskWorker stopped")
    
    async def stop(self):
        """Stop the task worker."""
        self._running = False
    
    async def _check_cancellation(self, task: Task) -> bool:
        """Check if the task should be cancelled."""
        if await self.task_manager.is_cancellation_requested(task.id):
            logger.info(f"Task {task.id} cancellation detected")
            now = __import__("datetime").datetime.utcnow()
            await self.task_manager._db.execute(
                """
                UPDATE tasks SET status = ?, completed_at = ?
                WHERE id = ?
                """,
                (TaskStatus.CANCELLED.value, now.isoformat(), task.id),
            )
            await self.task_manager._add_event(task.id, "cancelled", {"reason": "user_requested"})
            await self.task_manager._db.commit()
            return True
        return False
    
    async def _execute_worker_mode(self, task: Task) -> str:
        """Execute task using local tool handlers.
        
        This is the simplest mode - just run tools directly.
        Good for: file operations, CLI commands, simple automation.
        """
        await self.task_manager.update_progress(task.id, 0.1, "Starting worker execution...")
        
        # For worker mode, we interpret the description as a task prompt
        # and try to execute relevant tools
        # This is a simple implementation - could be enhanced with LLM routing
        
        description = task.description.lower()
        
        # Check for cancellation periodically
        if await self._check_cancellation(task):
            return "Task cancelled"
        
        # Simple keyword-based routing (could be replaced with LLM)
        result_parts = []
        
        if "bash" in description or "run" in description or "command" in description:
            # Try to extract and run a command
            # This is very basic - a real implementation would use an LLM
            handler = self.tool_handlers.get("bash")
            if handler:
                await self.task_manager.update_progress(task.id, 0.5, "Executing command...")
                # Note: This is a placeholder - real implementation would parse the command
                result_parts.append("Worker mode doesn't yet support complex command parsing")
        
        await self.task_manager.update_progress(task.id, 0.9, "Finishing up...")
        
        if not result_parts:
            result_parts.append(
                f"Worker mode executed for: {task.description}\n"
                "Note: Worker mode is best for simple, predefined tasks. "
                "For complex tasks, use 'subagent' mode."
            )
        
        return "\n".join(result_parts)
    
    async def _execute_subagent_mode(self, task: Task) -> str:
        """Execute task by spawning a Letta subagent.
        
        This creates a new agent specifically for this task.
        Good for: research, complex multi-step tasks, anything needing memory.
        """
        await self.task_manager.update_progress(task.id, 0.1, "Creating subagent...")
        
        # Create a worker agent for this task
        try:
            # Create a minimal agent for the task
            worker_agent = await self.client.agents.create(
                name=f"task-worker-{task.id[:8]}",
                description=f"Worker agent for task: {task.description[:100]}",
                include_base_tools=True,
                memory_blocks=[
                    {
                        "label": "task",
                        "value": f"Your job: {task.description}\n\nComplete this task and report your findings.",
                    }
                ],
                # Inherit tools from main agent would be ideal, but for now use base tools
            )
            
            await self.task_manager.update_progress(task.id, 0.2, "Subagent created, starting work...")
            
            # Check for cancellation
            if await self._check_cancellation(task):
                # Cleanup: delete the worker agent
                await self.client.agents.delete(worker_agent.id)
                return "Task cancelled"
            
            # Send the task to the subagent
            response = await self.client.agents.messages.create(
                agent_id=worker_agent.id,
                messages=[{
                    "role": "user",
                    "content": f"""Please complete this task:

{task.description}

Work through it step by step and provide a comprehensive result."""
                }],
            )
            
            await self.task_manager.update_progress(task.id, 0.8, "Subagent completed, collecting results...")
            
            # Extract assistant messages from response
            result_parts = []
            for msg in response.messages:
                if getattr(msg, "message_type", None) == "assistant_message":
                    content = getattr(msg, "content", None)
                    if content:
                        result_parts.append(content)
            
            # Cleanup: delete the worker agent
            await self.client.agents.delete(worker_agent.id)
            
            await self.task_manager.update_progress(task.id, 1.0, "Complete")
            
            return "\n\n".join(result_parts) if result_parts else "Subagent completed but returned no response"
            
        except Exception as e:
            logger.exception(f"Subagent execution failed: {e}")
            raise
    
    async def _execute_background_mode(self, task: Task) -> str:
        """Execute task using Letta's background mode on the main agent.
        
        This runs on the main agent's context but in the background.
        Good for: tasks that need the main agent's memory/context.
        """
        await self.task_manager.update_progress(task.id, 0.1, "Starting background execution...")
        
        try:
            # Use Letta's async mode
            run = await self.client.agents.messages.create_async(
                agent_id=self.main_agent_id,
                messages=[{
                    "role": "user",
                    "content": f"""[BACKGROUND TASK]

Please complete this task in the background:

{task.description}

When done, summarize your results."""
                }],
            )
            
            await self.task_manager.update_progress(task.id, 0.3, f"Background run started: {run.id}")
            
            # Poll for completion
            max_polls = 60  # 5 minutes with 5 second intervals
            for i in range(max_polls):
                # Check for cancellation
                if await self._check_cancellation(task):
                    return "Task cancelled (background run may still be processing)"
                
                run = await self.client.runs.retrieve(run.id)
                
                if run.status == "completed":
                    break
                elif run.status == "failed":
                    raise RuntimeError(f"Background run failed: {run.error}")
                
                # Update progress
                progress = 0.3 + (i / max_polls) * 0.6
                await self.task_manager.update_progress(
                    task.id, progress, f"Background run status: {run.status}"
                )
                
                await asyncio.sleep(5)
            else:
                raise TimeoutError("Background run timed out after 5 minutes")
            
            await self.task_manager.update_progress(task.id, 0.95, "Collecting results...")
            
            # Get the messages from the run
            messages = await self.client.runs.messages.list(run.id)
            
            # Extract assistant messages
            result_parts = []
            for msg in messages:
                if getattr(msg, "message_type", None) == "assistant_message":
                    content = getattr(msg, "content", None)
                    if content:
                        result_parts.append(content)
            
            return "\n\n".join(result_parts) if result_parts else "Background task completed"
            
        except Exception as e:
            logger.exception(f"Background execution failed: {e}")
            raise
