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
        oauth_manager: Optional["OAuthTokenManager"] = None,
    ):
        self.settings = settings or get_settings()
        self.conversation_manager = conversation_manager
        self.process_callback = process_callback
        self.actor_system = None  # Set after ActorSystem.setup()
        self.heartbeat_callback = heartbeat_callback
        self.oauth_manager = oauth_manager

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

            lines = [
                f"Status: {status}",
                f"Pending messages: {pending}",
            ]
            
            # Actor system info
            if self.actor_system and hasattr(self.actor_system, 'registry'):
                from lethe.actor import ActorState
                actors = self.actor_system.registry.all_actors
                
                # Separate system actors (cortex, brainstem, dmn, amygdala) from user-spawned
                system_names = {"cortex", "brainstem", "dmn", "amygdala"}
                active = [a for a in actors if a.state in (ActorState.RUNNING, ActorState.INITIALIZING, ActorState.WAITING)]
                terminated = [a for a in actors if a.state == ActorState.TERMINATED and a.name not in system_names]
                
                # DMN status: sleeping (between rounds) or running
                dmn_active = any(a.name == "dmn" and a.state == ActorState.RUNNING for a in actors)
                dmn_status = "🟢 running" if dmn_active else "💤 sleeping (wakes on heartbeat)"
                amygdala_enabled = bool(getattr(self.actor_system, "amygdala", None))
                amygdala_active = any(a.name == "amygdala" and a.state == ActorState.RUNNING for a in actors)
                if not amygdala_enabled:
                    amygdala_status = "⚪ disabled"
                else:
                    amygdala_status = "🟢 running" if amygdala_active else "💤 sleeping (wakes on heartbeat)"
                
                # Subagents (non-system)
                subagents = [a for a in active if a.name not in system_names]
                brainstem_active = any(a.name == "brainstem" and a.state == ActorState.RUNNING for a in actors)
                
                lines.append(f"\nCortex: 🟢 active")
                lines.append(f"Brainstem: {'🟢 online' if brainstem_active else '🟡 starting'}")
                lines.append(f"DMN: {dmn_status}")
                lines.append(f"Amygdala: {amygdala_status}")
                
                if subagents:
                    lines.append(f"\nSubagents ({len(subagents)} active):")
                    for a in subagents:
                        state_emoji = {"running": "🟢", "initializing": "🟡", "waiting": "🔵"}.get(a.state.value, "⚪")
                        goals_short = a.goals[:60] + "..." if len(a.goals) > 60 else a.goals
                        lines.append(f"  {state_emoji} {a.name}: {goals_short}")
                
                if terminated:
                    lines.append(f"\nRecent ({len(terminated)}):")
                    for a in terminated[:5]:
                        goals_short = a.goals[:50] + "..." if len(a.goals) > 50 else a.goals
                        lines.append(f"  ⚫ {a.name}: {goals_short}")
                    if len(terminated) > 5:
                        lines.append(f"  ... +{len(terminated) - 5} more")

            await message.answer("\n".join(lines))

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

            # Check if this is an OAuth auth code (long alphanumeric string, 40+ chars)
            # Auth codes look like: ant-auth-... or similar long tokens
            text = message.text.strip()
            if self.oauth_manager and len(text) >= 40 and text.isalnum():
                logger.info(f"Detected potential OAuth auth code: {text[:20]}...")
                try:
                    await self.oauth_manager.receive_auth_code(text)
                    await message.answer("✓ OAuth code received and processing...")
                    return
                except Exception as e:
                    logger.error(f"Error processing OAuth code: {e}")
                    await message.answer(f"Error processing auth code: {e}")
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
                mime_map = {'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png', 
                            'gif': 'image/gif', 'webp': 'image/webp'}
                mime_type = mime_map.get(ext, 'image/jpeg')
                
                # Build multimodal content (image-only if no caption provided).
                caption = (message.caption or "").strip()
                multimodal_content = []
                if caption:
                    multimodal_content.append({"type": "text", "text": caption})
                multimodal_content.append(
                    {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{image_data}"}}
                )

                caption_preview = caption[:50] if caption else "(no caption)"
                logger.info(
                    f"Received photo ({photo.width}x{photo.height}) with caption: {caption_preview}..."
                )
                
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

        @self.dp.message(F.document)
        async def handle_document(message: Message):
            """Handle document/file messages - save to workspace/Downloads."""
            if not self._is_authorized(message.from_user.id):
                await message.answer("Unauthorized.")
                return

            if not self.conversation_manager or not self.process_callback:
                await message.answer("Bot not fully initialized.")
                return

            document = message.document
            file_name = document.file_name or f"file_{document.file_id}"
            
            # Create Downloads directory in workspace
            downloads_dir = self.settings.workspace_dir / "Downloads"
            downloads_dir.mkdir(parents=True, exist_ok=True)
            
            # Download file
            try:
                file = await self.bot.get_file(document.file_id)
                file_path = downloads_dir / file_name
                
                await self.bot.download_file(file.file_path, file_path)
                
                logger.info(f"Received file: {file_name} ({document.file_size} bytes) -> {file_path}")
                
                # Build message with file info
                caption = message.caption or ""
                file_info = f"[Received file: {file_path}]"
                if caption:
                    content = f"{file_info}\n{caption}"
                else:
                    content = file_info
                
                # Add message to conversation manager
                await self.conversation_manager.add_message(
                    chat_id=message.chat.id,
                    user_id=message.from_user.id,
                    content=content,
                    metadata={
                        "username": message.from_user.username,
                        "first_name": message.from_user.first_name,
                        "is_document": True,
                        "file_name": file_name,
                        "file_path": str(file_path),
                        "file_size": document.file_size,
                        "mime_type": document.mime_type,
                    },
                    process_callback=self.process_callback,
                )
            except Exception as e:
                logger.error(f"Failed to process document: {e}")
                await message.answer(f"Failed to download file: {e}")

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
