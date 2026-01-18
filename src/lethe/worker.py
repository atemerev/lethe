"""Worker that processes tasks from the queue."""

import asyncio
import logging
from typing import Optional

from aiogram.utils.chat_action import ChatActionSender

from lethe.agent import AgentManager
from lethe.config import Settings, get_settings
from lethe.queue import TaskQueue
from lethe.telegram import TelegramBot

logger = logging.getLogger(__name__)


class Worker:
    """Processes tasks from the queue using the Letta agent."""

    def __init__(
        self,
        task_queue: TaskQueue,
        agent_manager: AgentManager,
        telegram_bot: TelegramBot,
        settings: Optional[Settings] = None,
    ):
        self.task_queue = task_queue
        self.agent_manager = agent_manager
        self.telegram_bot = telegram_bot
        self.settings = settings or get_settings()
        self._running = False

    async def start(self):
        """Start the worker loop."""
        self._running = True
        logger.info("Worker started")

        # Ensure agent is initialized
        await self.agent_manager.get_or_create_agent()

        while self._running:
            try:
                # Wait for a task (with timeout to allow graceful shutdown)
                task = await self.task_queue.dequeue(timeout=5.0)

                if task is None:
                    continue  # Timeout, check if still running

                logger.info(f"Processing task {task.id}: {task.message[:50]}...")

                try:
                    # Track messages sent for this task
                    messages_sent = []
                    
                    # Callback to send messages as they arrive
                    async def on_message(content: str):
                        messages_sent.append(content)
                        await self.telegram_bot.send_message(
                            chat_id=task.chat_id,
                            text=content,
                        )
                    
                    # Use aiogram's ChatActionSender to show typing indicator
                    async with ChatActionSender.typing(
                        bot=self.telegram_bot.bot,
                        chat_id=task.chat_id,
                        interval=4.0,  # Refresh every 4 seconds
                    ):
                        # Send to agent with streaming callback
                        response = await self.agent_manager.send_message(
                            message=task.message,
                            context=task.metadata,
                            on_message=on_message,
                        )

                    # Mark complete with full response
                    await self.task_queue.complete(task.id, response)

                    # Only send final response if no messages were streamed
                    # (this handles edge cases where agent returns without assistant messages)
                    if not messages_sent and response:
                        await self.telegram_bot.send_message(
                            chat_id=task.chat_id,
                            text=response,
                        )

                    logger.info(f"Completed task {task.id}")

                except Exception as e:
                    logger.exception(f"Task {task.id} failed: {e}")
                    await self.task_queue.fail(task.id, str(e))

                    # Notify user of failure
                    await self.telegram_bot.send_message(
                        chat_id=task.chat_id,
                        text=f"❌ Task failed: {e}",
                    )

            except asyncio.CancelledError:
                logger.info("Worker cancelled")
                break
            except Exception as e:
                logger.exception(f"Worker error: {e}")
                await asyncio.sleep(1)  # Prevent tight error loop

        logger.info("Worker stopped")

    async def stop(self):
        """Stop the worker loop."""
        self._running = False


class HeartbeatWorker:
    """Sends periodic heartbeat messages to the agent."""

    def __init__(
        self,
        agent_manager: AgentManager,
        telegram_bot: TelegramBot,
        chat_id: int,  # Chat to send heartbeat responses to
        interval_minutes: int = 60,
        enabled: bool = True,
    ):
        self.agent_manager = agent_manager
        self.telegram_bot = telegram_bot
        self.chat_id = chat_id
        self.interval_minutes = interval_minutes
        self.enabled = enabled
        self._running = False

    async def start(self):
        """Start the heartbeat loop."""
        if not self.enabled:
            logger.info("Heartbeat disabled")
            return

        self._running = True
        logger.info(f"Heartbeat started (interval: {self.interval_minutes} min)")

        while self._running:
            try:
                await asyncio.sleep(self.interval_minutes * 60)

                if not self._running:
                    break

                logger.info("Sending heartbeat...")

                messages_sent = []
                
                async def on_message(content: str):
                    # Only forward meaningful content, not just acknowledgments
                    if content and not content.lower().startswith("acknowledged"):
                        messages_sent.append(content)
                        await self.telegram_bot.send_message(
                            chat_id=self.chat_id,
                            text=f"🕐 {content}",
                        )

                response = await self.agent_manager.send_message(
                    message="""[HEARTBEAT]

This is a periodic check-in. Review your state:
1. Check your memory for any pending tasks or reminders
2. Consider if there's anything proactive you should do
3. Update your task list if needed

If you have something to report or a reminder for the principal, respond with it.
If not, you can simply acknowledge with a brief status.""",
                    on_message=on_message,
                )

                # Log if nothing meaningful was sent
                if not messages_sent:
                    logger.info("Heartbeat: agent acknowledged, nothing to report")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"Heartbeat error: {e}")

        logger.info("Heartbeat stopped")

    async def stop(self):
        """Stop the heartbeat loop."""
        self._running = False
