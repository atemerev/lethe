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
    ):
        self.settings = settings or get_settings()
        self.conversation_manager = conversation_manager
        self.process_callback = process_callback
        self.heartbeat_callback = heartbeat_callback

        self.bot = Bot(
            token=self.settings.telegram_bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN),
        )
        self.dp = Dispatcher()
        self._running = False
        self._typing_tasks: dict[int, asyncio.Task] = {}
        self._last_message_id: Optional[int] = None
        self._last_chat_id: Optional[int] = None

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
                "/heartbeat - Force a check-in"
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
        


        @self.dp.message(F.text)
        async def handle_message(message: Message):
            if not self._is_authorized(message.from_user.id):
                await message.answer("Unauthorized.")
                return

            if not self.conversation_manager or not self.process_callback:
                await message.answer("Bot not fully initialized.")
                return

            # Store last message ID for reactions
            self._last_message_id = message.message_id
            self._last_chat_id = message.chat.id
            
            # Add message to conversation manager
            await self.conversation_manager.add_message(
                chat_id=message.chat.id,
                user_id=message.from_user.id,
                content=message.text,
                metadata={
                    "username": message.from_user.username,
                    "first_name": message.from_user.first_name,
                    "message_id": message.message_id,
                },
                process_callback=self.process_callback,
            )

        @self.dp.message(F.photo)
        async def handle_photo(message: Message):
            """Handle photo messages with optional caption."""
            if not self._is_authorized(message.from_user.id):
                await message.answer("Unauthorized.")
                return

            if not self.conversation_manager or not self.process_callback:
                await message.answer("Bot not fully initialized.")
                return

            # Get the largest photo (last in the list)
            photo = message.photo[-1]
            
            # Download photo to memory and convert to base64
            import base64
            from io import BytesIO
            
            try:
                file = await self.bot.get_file(photo.file_id)
                bio = BytesIO()
                await self.bot.download_file(file.file_path, bio)
                bio.seek(0)
                image_data = base64.b64encode(bio.read()).decode('utf-8')
                
                # Determine mime type from file extension
                ext = file.file_path.split('.')[-1].lower() if file.file_path else 'jpg'
                mime_type = f"image/{ext}" if ext in ('jpg', 'jpeg', 'png', 'gif', 'webp') else "image/jpeg"
                
                # Build multimodal content
                caption = message.caption or "What is this?"
                multimodal_content = [
                    {"type": "text", "text": caption},
                    {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{image_data}"}}
                ]
                
                logger.info(f"Received photo ({photo.width}x{photo.height}) with caption: {caption[:50]}...")
                
                # Add message to conversation manager with multimodal content
                await self.conversation_manager.add_message(
                    chat_id=message.chat.id,
                    user_id=message.from_user.id,
                    content=multimodal_content,  # Pass as list for multimodal
                    metadata={
                        "username": message.from_user.username,
                        "first_name": message.from_user.first_name,
                        "is_photo": True,
                        "photo_size": f"{photo.width}x{photo.height}",
                    },
                    process_callback=self.process_callback,
                )
            except Exception as e:
                logger.error(f"Failed to process photo: {e}")
                await message.answer(f"Failed to process photo: {e}")

    def _is_authorized(self, user_id: int) -> bool:
        """Check if user is authorized."""
        allowed = self.settings.allowed_user_ids
        return not allowed or user_id in allowed

    async def send_message(self, chat_id: int, text: str, parse_mode: str = "Markdown"):
        """Send a message, splitting on --- for natural pauses."""
        # Skip empty messages (some models return empty responses)
        if not text or not text.strip():
            logger.warning("Skipping empty message to Telegram")
            return
            
        MAX_LENGTH = 4000  # Telegram limit is 4096
        
        # Split on --- for natural message breaks (human-like texting)
        # Each segment becomes a separate message with a pause
        segments = [s.strip() for s in text.split("---") if s.strip()]
        
        for i, segment in enumerate(segments):
            # Further split if segment is too long
            if len(segment) <= MAX_LENGTH:
                chunks = [segment]
            else:
                chunks = []
                current = ""
                for line in segment.split("\n"):
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
                    # Fallback to no parsing if markdown fails
                    await self.bot.send_message(chat_id, chunk, parse_mode=None)
                await asyncio.sleep(0.1)
            
            # Pause between segments (natural typing feel)
            if i < len(segments) - 1:
                await asyncio.sleep(1.0 + len(segment) / 500)  # Longer pause for longer messages

    async def send_photo(self, chat_id: int, photo_path: str, caption: str = ""):
        """Send a photo to chat."""
        from aiogram.types import FSInputFile
        try:
            photo = FSInputFile(photo_path)
            await self.bot.send_photo(chat_id, photo, caption=caption[:1024] if caption else None)
        except Exception as e:
            logger.error(f"Failed to send photo: {e}")
            await self.send_message(chat_id, f"[Image: {photo_path}]")
    
    async def react_to_message(self, chat_id: int, message_id: int, emoji: str = "👍"):
        """React to a message with an emoji."""
        from aiogram.types import ReactionTypeEmoji
        try:
            await self.bot.set_message_reaction(
                chat_id=chat_id,
                message_id=message_id,
                reaction=[ReactionTypeEmoji(emoji=emoji)]
            )
            logger.info(f"Reacted to message {message_id} with {emoji}")
        except Exception as e:
            logger.warning(f"Failed to react to message: {e}")
    
    async def react_to_last_message(self, emoji: str = "👍"):
        """React to the last received message."""
        if self._last_chat_id and self._last_message_id:
            await self.react_to_message(self._last_chat_id, self._last_message_id, emoji)
        else:
            logger.warning("No last message to react to")

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
