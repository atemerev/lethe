"""Telegram bot interface."""

import asyncio
import logging
from typing import Callable, Optional

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.enums import ChatAction
from aiogram.types import Message

from lethe.config import Settings, get_settings
from lethe.queue import TaskQueue

logger = logging.getLogger(__name__)


class TelegramBot:
    """Async Telegram bot that queues messages for agent processing."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        task_queue: Optional[TaskQueue] = None,
    ):
        self.settings = settings or get_settings()
        self.task_queue = task_queue

        self.bot = Bot(
            token=self.settings.telegram_bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN),
        )
        self.dp = Dispatcher()

        self._setup_handlers()

    def _setup_handlers(self):
        """Set up message handlers."""

        @self.dp.message(CommandStart())
        async def handle_start(message: Message):
            """Handle /start command."""
            if not self._is_authorized(message.from_user.id):
                await message.answer("Unauthorized.")
                return

            await message.answer(
                "Hello! I'm Lethe, your autonomous assistant.\n\n"
                "Send me any message and I'll process it asynchronously. "
                "I'll reply when I'm done.\n\n"
                "Commands:\n"
                "/status - Check queue status\n"
                "/cancel - Cancel pending tasks"
            )

        @self.dp.message(Command("status"))
        async def handle_status(message: Message):
            """Handle /status command."""
            if not self._is_authorized(message.from_user.id):
                return

            if self.task_queue:
                pending = await self.task_queue.get_pending_count()
                await message.answer(f"Pending tasks: {pending}")
            else:
                await message.answer("Queue not initialized.")

        @self.dp.message(F.text)
        async def handle_message(message: Message):
            """Handle regular text messages."""
            if not self._is_authorized(message.from_user.id):
                logger.warning(f"Unauthorized message from user {message.from_user.id}")
                return

            if not self.task_queue:
                await message.answer("System not ready. Please try again later.")
                return

            # Queue the task
            task = await self.task_queue.enqueue(
                chat_id=message.chat.id,
                user_id=message.from_user.id,
                message=message.text,
                metadata={
                    "username": message.from_user.username,
                    "first_name": message.from_user.first_name,
                    "message_id": message.message_id,
                },
            )

            # Only show queue position if there are multiple tasks
            pending = await self.task_queue.get_pending_count()
            if pending > 1:
                await message.answer(f"📋 Queued (position: {pending})")

            logger.info(f"Queued task {task.id} from user {message.from_user.id}")

    def _is_authorized(self, user_id: int) -> bool:
        """Check if a user is authorized to use the bot."""
        if not self.settings.allowed_user_ids:
            return True  # No restrictions
        return user_id in self.settings.allowed_user_ids

    async def send_typing(self, chat_id: int):
        """Send typing indicator to a chat."""
        try:
            await self.bot.send_chat_action(chat_id, ChatAction.TYPING)
        except Exception as e:
            logger.warning(f"Failed to send typing to {chat_id}: {e}")

    async def send_message(self, chat_id: int, text: str, parse_mode: Optional[str] = None, **kwargs):
        """Send a message to a chat (can be called from anywhere).
        
        Args:
            chat_id: Telegram chat ID
            text: Message text
            parse_mode: Optional parse mode. If None, sends as plain text (safest for agent responses)
        """
        # Split long messages
        max_len = 4096
        chunks = [text] if len(text) <= max_len else self._split_message(text, max_len)
        
        for chunk in chunks:
            try:
                await self.bot.send_message(chat_id, chunk, parse_mode=parse_mode, **kwargs)
            except Exception as e:
                # If markdown parsing fails, try without parse_mode
                if "parse entities" in str(e).lower():
                    await self.bot.send_message(chat_id, chunk, parse_mode=None, **kwargs)
                else:
                    raise
            if len(chunks) > 1:
                await asyncio.sleep(0.1)  # Rate limiting

    def _split_message(self, text: str, max_len: int) -> list[str]:
        """Split a long message into chunks."""
        chunks = []
        while text:
            if len(text) <= max_len:
                chunks.append(text)
                break

            # Try to split at a newline
            split_idx = text.rfind("\n", 0, max_len)
            if split_idx == -1 or split_idx < max_len // 2:
                # No good newline, split at space
                split_idx = text.rfind(" ", 0, max_len)
            if split_idx == -1 or split_idx < max_len // 2:
                # No good space, hard split
                split_idx = max_len

            chunks.append(text[:split_idx])
            text = text[split_idx:].lstrip()

        return chunks

    async def start(self):
        """Start the bot polling."""
        logger.info("Starting Telegram bot...")
        await self.dp.start_polling(self.bot, handle_signals=False)

    async def stop(self):
        """Stop the bot."""
        logger.info("Stopping Telegram bot...")
        await self.dp.stop_polling()
        await self.bot.session.close()


async def create_bot(
    settings: Optional[Settings] = None,
    task_queue: Optional[TaskQueue] = None,
) -> TelegramBot:
    """Create and return a TelegramBot instance."""
    return TelegramBot(settings=settings, task_queue=task_queue)
