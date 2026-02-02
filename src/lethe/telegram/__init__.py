"""Telegram bot interface."""

import asyncio
import logging
from typing import Callable, Optional

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatAction
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from lethe.config import Settings, get_settings
from lethe.conversation import ConversationManager

logger = logging.getLogger(__name__)


class TelegramBot:
    """Async Telegram bot with interruptible conversation processing."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        conversation_manager: Optional[ConversationManager] = None,
        process_callback: Optional[Callable] = None,
        heartbeat_callback: Optional[Callable] = None,
        oauth_callback: Optional[Callable] = None,
    ):
        self.settings = settings or get_settings()
        self.conversation_manager = conversation_manager
        self.process_callback = process_callback
        self.heartbeat_callback = heartbeat_callback
        self.oauth_callback = oauth_callback  # For Claude Max OAuth

        self.bot = Bot(
            token=self.settings.telegram_bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN),
        )
        self.dp = Dispatcher()
        self._running = False
        self._typing_tasks: dict[int, asyncio.Task] = {}

        self._setup_handlers()

    def _setup_handlers(self):
        """Set up message handlers."""

        @self.dp.message(CommandStart())
        async def handle_start(message: Message):
            if not self._is_authorized(message.from_user.id):
                await message.answer("Unauthorized.")
                return

            await message.answer(
                "Hello! I'm Lethe, your autonomous assistant.\n\n"
                "Send me any message and I'll help you.\n\n"
                "Commands:\n"
                "/status - Check status\n"
                "/stop - Cancel current processing\n"
                "/heartbeat - Force a check-in\n"
                "/oauth <url> - Complete Claude Max authentication"
            )

        @self.dp.message(Command("status"))
        async def handle_status(message: Message):
            if not self._is_authorized(message.from_user.id):
                return

            chat_id = message.chat.id
            is_processing = self.conversation_manager.is_processing(chat_id) if self.conversation_manager else False
            is_debouncing = self.conversation_manager.is_debouncing(chat_id) if self.conversation_manager else False
            pending = self.conversation_manager.get_pending_count(chat_id) if self.conversation_manager else 0

            status = "idle"
            if is_processing:
                status = "processing"
            elif is_debouncing:
                status = "waiting for more input"

            await message.answer(
                f"Status: {status}\n"
                f"Pending messages: {pending}"
            )

        @self.dp.message(Command("stop"))
        async def handle_stop(message: Message):
            if not self._is_authorized(message.from_user.id):
                return

            if self.conversation_manager:
                cancelled = await self.conversation_manager.cancel(message.chat.id)
                if cancelled:
                    await message.answer("Processing cancelled.")
                else:
                    await message.answer("Nothing to cancel.")

        @self.dp.message(Command("heartbeat"))
        async def handle_heartbeat(message: Message):
            if not self._is_authorized(message.from_user.id):
                return

            if self.heartbeat_callback:
                await message.answer("Triggering heartbeat...")
                await self.heartbeat_callback()
            else:
                await message.answer("Heartbeat not configured.")
        
        @self.dp.message(Command("oauth"))
        async def handle_oauth(message: Message):
            """Handle OAuth redirect URL for Claude Max authentication."""
            if not self._is_authorized(message.from_user.id):
                return
            
            # Extract URL from command
            text = message.text or ""
            parts = text.split(maxsplit=1)
            if len(parts) < 2:
                await message.answer(
                    "Usage: /oauth <redirect_url>\n\n"
                    "Paste the full URL from your browser after authenticating."
                )
                return
            
            redirect_url = parts[1].strip()
            
            if self.oauth_callback:
                try:
                    await message.answer("Processing OAuth...")
                    await self.oauth_callback(redirect_url)
                    await message.answer("✅ Claude Max authentication successful!")
                except Exception as e:
                    await message.answer(f"❌ OAuth failed: {e}")
            else:
                await message.answer("OAuth not configured or already authenticated.")

        @self.dp.message(F.text)
        async def handle_message(message: Message):
            if not self._is_authorized(message.from_user.id):
                await message.answer("Unauthorized.")
                return

            if not self.conversation_manager or not self.process_callback:
                await message.answer("Bot not fully initialized.")
                return

            # Add message to conversation manager
            await self.conversation_manager.add_message(
                chat_id=message.chat.id,
                user_id=message.from_user.id,
                content=message.text,
                metadata={
                    "username": message.from_user.username,
                    "first_name": message.from_user.first_name,
                },
                process_callback=self.process_callback,
            )

    def _is_authorized(self, user_id: int) -> bool:
        """Check if user is authorized."""
        allowed = self.settings.allowed_user_ids
        return not allowed or user_id in allowed

    async def send_message(self, chat_id: int, text: str, parse_mode: str = "Markdown"):
        """Send a message, splitting if too long."""
        MAX_LENGTH = 4000  # Telegram limit is 4096

        if len(text) <= MAX_LENGTH:
            try:
                await self.bot.send_message(chat_id, text, parse_mode=parse_mode)
            except Exception:
                # Fallback to no parsing if markdown fails
                await self.bot.send_message(chat_id, text, parse_mode=None)
            return

        # Split long messages
        chunks = []
        current = ""
        for line in text.split("\n"):
            if len(current) + len(line) + 1 > MAX_LENGTH:
                if current:
                    chunks.append(current)
                current = line
            else:
                current = f"{current}\n{line}" if current else line
        if current:
            chunks.append(current)

        for chunk in chunks:
            try:
                await self.bot.send_message(chat_id, chunk, parse_mode=parse_mode)
            except Exception:
                await self.bot.send_message(chat_id, chunk, parse_mode=None)
            await asyncio.sleep(0.1)

    async def send_photo(self, chat_id: int, photo_path: str, caption: str = ""):
        """Send a photo to chat."""
        from aiogram.types import FSInputFile
        try:
            photo = FSInputFile(photo_path)
            await self.bot.send_photo(chat_id, photo, caption=caption[:1024] if caption else None)
        except Exception as e:
            logger.error(f"Failed to send photo: {e}")
            await self.send_message(chat_id, f"[Image: {photo_path}]")

    async def start_typing(self, chat_id: int):
        """Start showing typing indicator."""
        if chat_id in self._typing_tasks:
            return

        async def typing_loop():
            while True:
                try:
                    await self.bot.send_chat_action(chat_id, ChatAction.TYPING)
                    await asyncio.sleep(4)
                except asyncio.CancelledError:
                    break
                except Exception:
                    break

        self._typing_tasks[chat_id] = asyncio.create_task(typing_loop())

    async def stop_typing(self, chat_id: int):
        """Stop showing typing indicator."""
        task = self._typing_tasks.pop(chat_id, None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def start(self):
        """Start the bot."""
        self._running = True
        logger.info("Starting Telegram bot...")
        # handle_signals=False lets us handle SIGTERM ourselves
        await self.dp.start_polling(self.bot, handle_signals=False)

    async def stop(self):
        """Stop the bot."""
        self._running = False
        # Cancel all typing tasks
        for task in self._typing_tasks.values():
            task.cancel()
        self._typing_tasks.clear()
        await self.dp.stop_polling()
        await self.bot.session.close()
        logger.info("Telegram bot stopped")
