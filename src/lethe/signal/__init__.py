"""Signal bot interface via signal-cli daemon."""

import asyncio
import logging
from pathlib import Path
from typing import Any, Callable, Optional

from lethe.config import Settings, get_settings
from lethe.conversation import ConversationManager
from lethe.models import MODEL_CATALOG, get_available_providers, provider_for_model
from lethe.proxy_bot import MockMessage
from lethe.signal.client import SignalClient

logger = logging.getLogger(__name__)


class SignalBotAdapter:
    """Duck-type aiogram.Bot interface for transport_tools compatibility.

    The telegram_tools module calls bot.send_message(chat_id=..., text=..., parse_mode=...)
    via ContextVar. This adapter translates those calls to signal-cli JSON-RPC.
    """

    def __init__(self, client: SignalClient, sent_timestamps: Optional[set] = None):
        self._client = client
        self._message_counter = 0
        self._sent_timestamps = sent_timestamps  # shared with SignalBot

    def _track_ts(self, result):
        """Track sent timestamp for self-reply loop prevention."""
        if self._sent_timestamps is not None and isinstance(result, dict):
            ts = result.get("timestamp")
            if ts:
                self._sent_timestamps.add(ts)

    async def send_message(self, chat_id, text, parse_mode=None, **kwargs) -> MockMessage:
        result = await self._client.send(recipient=str(chat_id), message=text)
        self._track_ts(result)
        ts = result.get("timestamp", self._message_counter)
        self._message_counter += 1
        return MockMessage(message_id=ts)

    async def send_photo(self, chat_id, photo, caption=None, **kwargs) -> MockMessage:
        path = _resolve_path(photo)
        result = await self._client.send(
            recipient=str(chat_id), message=caption or "", attachments=[path]
        )
        self._track_ts(result)
        return MockMessage(message_id=result.get("timestamp", 0))

    async def send_document(self, chat_id, document, caption=None, **kwargs) -> MockMessage:
        path = _resolve_path(document)
        result = await self._client.send(
            recipient=str(chat_id), message=caption or "", attachments=[path]
        )
        return MockMessage(message_id=result.get("timestamp", 0))

    async def send_animation(self, chat_id, animation, caption=None, **kwargs) -> MockMessage:
        return await self.send_document(chat_id, animation, caption, **kwargs)

    async def send_video(self, chat_id, video, caption=None, **kwargs) -> MockMessage:
        return await self.send_document(chat_id, video, caption, **kwargs)

    async def send_audio(self, chat_id, audio, caption=None, **kwargs) -> MockMessage:
        return await self.send_document(chat_id, audio, caption, **kwargs)

    async def send_voice(self, chat_id, voice, caption=None, **kwargs) -> MockMessage:
        return await self.send_document(chat_id, voice, caption, **kwargs)

    async def send_chat_action(self, chat_id, action, **kwargs):
        try:
            await self._client.send_typing(recipient=str(chat_id))
        except Exception:
            pass  # Best-effort; signal-cli may not support typing

    async def set_message_reaction(self, chat_id, message_id, reaction, **kwargs):
        emoji = ""
        if reaction:
            emoji = getattr(reaction[0], "emoji", str(reaction[0]))
        if not emoji:
            return
        try:
            await self._client.send_reaction(
                recipient=str(chat_id),
                emoji=emoji,
                target_author=str(chat_id),
                target_timestamp=int(message_id),
            )
        except Exception as e:
            logger.debug(f"Signal reaction failed: {e}")


def _resolve_path(file_input: Any) -> str:
    """Extract filesystem path from various input types."""
    if hasattr(file_input, "path"):
        return str(file_input.path)
    if isinstance(file_input, Path):
        return str(file_input)
    return str(file_input)


class SignalBot:
    """Signal bot interface, mirroring TelegramBot's contract.

    Communicates with signal-cli daemon over HTTP (JSON-RPC + SSE).
    """

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
        self.actor_system = None  # Set after ActorSystem.setup()
        self.agent = None  # Set after agent init
        self.heartbeat_callback = heartbeat_callback

        self.client = SignalClient(
            base_url=self.settings.signal_cli_url,
            account=self.settings.signal_account,
        )
        # Adapter shares _sent_timestamps with the bot for loop prevention
        self._sent_timestamps: set[int] = set()
        self.adapter = SignalBotAdapter(self.client, sent_timestamps=self._sent_timestamps)

        self._running = False
        self._typing_tasks: dict[str, int] = {}  # recipient -> placeholder message timestamp
        self._last_message_timestamp: Optional[int] = None
        self._last_sender: Optional[str] = None

        # Pending model/aux selection: sender -> (kind, models_list)
        self._pending_selection: dict[str, tuple[str, list]] = {}

    def _is_authorized(self, sender: str) -> bool:
        """Check if sender is allowed.

        Default (no SIGNAL_ALLOWED_NUMBERS set): only Note to Self is allowed.
        With SIGNAL_ALLOWED_NUMBERS: listed numbers + own account are allowed.
        """
        # Always allow messages from own account (Note to Self)
        if sender == self.settings.signal_account:
            return True
        allowed = self.settings.signal_allowed_number_list
        # If no allowlist configured, only self is allowed (safe default)
        if not allowed:
            return False
        return sender in allowed

    # --- Message handling ---

    async def _handle_event(self, event: dict):
        """Route an incoming signal-cli SSE event."""
        envelope = event.get("envelope", event)
        source = envelope.get("source") or envelope.get("sourceNumber", "")
        if not source:
            return

        # Check both dataMessage (direct) and syncMessage.sentMessage (Note to Self)
        data_msg = envelope.get("dataMessage")
        if not data_msg:
            sync_msg = envelope.get("syncMessage", {}).get("sentMessage")
            if sync_msg:
                # Only process Note to Self — ignore synced messages to other people
                dest = sync_msg.get("destination") or sync_msg.get("destinationNumber", "")
                if dest != self.settings.signal_account:
                    return  # Message to someone else, not Note to Self
                # Ignore our own sent messages (self-reply loop prevention)
                ts = sync_msg.get("timestamp", 0)
                if ts in self._sent_timestamps:
                    self._sent_timestamps.discard(ts)
                    return
                data_msg = sync_msg
            else:
                return  # Not a data/sync message (receipt, typing, etc.)

        text = (data_msg.get("message") or "").strip()
        timestamp = data_msg.get("timestamp", 0)

        if not text and not data_msg.get("attachments"):
            return  # Empty message

        if not self._is_authorized(source):
            logger.info(f"Signal: unauthorized sender {source}")
            return

        # Track last message for reactions
        self._last_message_timestamp = timestamp
        self._last_sender = source

        # Check for pending model selection (user replied with a number)
        if source in self._pending_selection and text.isdigit():
            await self._handle_selection(source, int(text))
            return

        # Command handling (text prefix matching)
        if text.startswith("/"):
            cmd = text.split()[0].lower()
            handled = await self._handle_command(source, cmd, text)
            if handled:
                return

        # Regular message — pass to conversation manager
        if not self.conversation_manager or not self.process_callback:
            return

        # Handle attachments
        attachments = data_msg.get("attachments", [])
        content = text
        if attachments and not text:
            filenames = [a.get("filename", a.get("id", "file")) for a in attachments]
            content = f"[Received files: {', '.join(filenames)}]"

        await self.conversation_manager.add_message(
            chat_id=source,
            user_id=source,
            content=content,
            metadata={
                "transport": "signal",
                "message_id": timestamp,
                "source": source,
            },
            process_callback=self.process_callback,
        )

    async def _handle_command(self, sender: str, cmd: str, full_text: str) -> bool:
        """Handle slash commands. Returns True if command was handled."""
        handlers = {
            "/start": self._cmd_start,
            "/help": self._cmd_start,
            "/status": self._cmd_status,
            "/stop": self._cmd_stop,
            "/heartbeat": self._cmd_heartbeat,
            "/model": self._cmd_model,
            "/aux": self._cmd_aux,
        }
        handler = handlers.get(cmd)
        if handler:
            await handler(sender)
            return True
        return False

    async def _cmd_start(self, sender: str):
        await self.send_message(
            sender,
            "Hello! I'm Lethe, your autonomous assistant.\n\n"
            "Send me any message and I'll help you.\n\n"
            "Commands:\n"
            "/status - Check status\n"
            "/stop - Cancel current processing\n"
            "/heartbeat - Force a check-in\n"
            "/model - Switch main LLM model\n"
            "/aux - Switch auxiliary model",
        )

    async def _cmd_status(self, sender: str):
        lines = []
        if self.conversation_manager:
            is_processing = self.conversation_manager.is_processing(sender)
            is_debouncing = self.conversation_manager.is_debouncing(sender)
            pending = self.conversation_manager.get_pending_count(sender)
            status = "processing" if is_processing else "waiting for more input" if is_debouncing else "idle"
            lines.append(f"Status: {status}")
            lines.append(f"Pending messages: {pending}")

        if self.actor_system and hasattr(self.actor_system, "registry"):
            from lethe.actor import ActorState

            actors = self.actor_system.registry.all_actors
            system_names = {"cortex", "brainstem", "dmn", "amygdala"}
            active = [a for a in actors if a.state in (ActorState.RUNNING, ActorState.INITIALIZING, ActorState.WAITING)]
            subagents = [a for a in active if a.name not in system_names]

            lines.append(f"\nCortex: active")
            if subagents:
                lines.append(f"\nSubagents ({len(subagents)} active):")
                for a in subagents:
                    goals_short = a.goals[:60] + "..." if len(a.goals) > 60 else a.goals
                    lines.append(f"  {a.state.value} {a.name}: {goals_short}")

        await self.send_message(sender, "\n".join(lines) or "Status: idle")

    async def _cmd_stop(self, sender: str):
        if self.conversation_manager:
            cancelled = await self.conversation_manager.cancel(sender)
            await self.send_message(sender, "Processing cancelled." if cancelled else "Nothing to cancel.")

    async def _cmd_heartbeat(self, sender: str):
        if self.heartbeat_callback:
            await self.send_message(sender, "Triggering heartbeat...")
            await self.heartbeat_callback()
        else:
            await self.send_message(sender, "Heartbeat not configured.")

    async def _cmd_model(self, sender: str):
        await self._show_model_picker(sender, "main")

    async def _cmd_aux(self, sender: str):
        await self._show_model_picker(sender, "aux")

    async def _show_model_picker(self, sender: str, kind: str):
        """Show numbered text menu for model selection."""
        if not self.agent:
            await self.send_message(sender, "Agent not initialized yet.")
            return

        current = self.agent.llm.config.model if kind == "main" else self.agent.llm.config.model_aux
        label = "Main model" if kind == "main" else "Aux model"

        provider_infos = get_available_providers()
        if not provider_infos:
            await self.send_message(sender, "No models available.")
            return

        models = []  # flat list of (name, model_id, pricing, auth)
        lines = [f"{label}: {current}", "", "Reply with number to switch:"]
        idx = 1
        for info in provider_infos:
            provider = info["provider"]
            auth = info.get("auth", "API")
            catalog = MODEL_CATALOG.get(provider, {})
            provider_models = catalog.get(kind, [])
            if not provider_models:
                continue
            lines.append(f"\n-- {info['label']} --")
            for name, model_id, pricing in provider_models:
                is_active = model_id == current
                marker = "* " if is_active else "  "
                suffix = "" if auth == "sub" else f" ({pricing})"
                lines.append(f"{marker}{idx}. {name}{suffix}")
                models.append((name, model_id, pricing, auth))
                idx += 1

        if not models:
            await self.send_message(sender, "No models available.")
            return

        self._pending_selection[sender] = (kind, models)
        await self.send_message(sender, "\n".join(lines))

    async def _handle_selection(self, sender: str, choice: int):
        """Handle numbered model selection reply."""
        kind, models = self._pending_selection.pop(sender, (None, None))
        if not kind or not models:
            return

        if choice < 1 or choice > len(models):
            await self.send_message(sender, f"Invalid choice. Pick 1-{len(models)}.")
            self._pending_selection[sender] = (kind, models)
            return

        name, model_id, pricing, auth = models[choice - 1]

        if not self.agent:
            return

        old_model = self.agent.llm.config.model if kind == "main" else self.agent.llm.config.model_aux

        # Switch provider if needed
        new_provider = provider_for_model(model_id)
        if new_provider and new_provider != self.agent.llm.config.provider:
            self.agent.llm.config.provider = new_provider

        # OAuth handling
        if auth == "sub":
            self.agent.llm._force_oauth = True
        else:
            self.agent.llm._force_oauth = False

        if kind == "main":
            self.agent.llm.config.model = model_id
        else:
            self.agent.llm.config.model_aux = model_id

        label = "Main model" if kind == "main" else "Aux model"
        logger.info(f"Signal: {label} changed: {old_model} -> {model_id}")
        await self.send_message(sender, f"{label} switched to {name} ({model_id})")

    # --- Outbound messaging ---

    async def send_message(self, recipient: str, text: str, parse_mode: str = "Markdown"):
        """Send a message, splitting on --- for natural pauses."""
        if not text or not text.strip():
            return

        MAX_LENGTH = 4000
        segments = [s.strip() for s in text.split("---") if s.strip()]

        # If there's a typing placeholder, edit it with the first chunk
        placeholder_ts = self._typing_tasks.pop(recipient, None)

        for i, segment in enumerate(segments):
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
                    # Edit the placeholder with the first chunk, send new for the rest
                    edit_ts = None
                    if placeholder_ts:
                        edit_ts = placeholder_ts
                        placeholder_ts = None  # Only edit once
                    result = await self.client.send(
                        recipient=recipient, message=chunk, edit_timestamp=edit_ts,
                    )
                    # Track timestamp to ignore our own sync echo
                    ts = result.get("timestamp") if isinstance(result, dict) else None
                    if ts:
                        self._sent_timestamps.add(ts)
                        # Cap set size to prevent unbounded growth
                        if len(self._sent_timestamps) > 100:
                            self._sent_timestamps = set(sorted(self._sent_timestamps)[-50:])
                except Exception as e:
                    logger.error(f"Signal send failed: {e}")
                await asyncio.sleep(0.1)

            # Human-like pause between segments
            if i < len(segments) - 1:
                import random

                think = random.uniform(1.5, 3.0)
                typing = len(segment) * 0.03
                pause = min(think + typing, 10.0)
                pause *= random.uniform(0.8, 1.3)
                await asyncio.sleep(pause)

    async def send_photo(self, recipient: str, photo_path: str, caption: str = ""):
        """Send a photo as attachment."""
        try:
            await self.client.send(
                recipient=recipient,
                message=caption or "",
                attachments=[photo_path],
            )
        except Exception as e:
            logger.error(f"Signal send_photo failed: {e}")
            await self.send_message(recipient, f"[Image: {photo_path}]")

    async def react_to_message(self, recipient: str, timestamp: int, emoji: str = "👍"):
        """React to a message by its timestamp."""
        try:
            await self.client.send_reaction(
                recipient=recipient,
                emoji=emoji,
                target_author=recipient,
                target_timestamp=timestamp,
            )
        except Exception as e:
            logger.debug(f"Signal reaction failed: {e}")

    async def react_to_last_message(self, emoji: str = "👍"):
        """React to the last received message."""
        if self._last_sender and self._last_message_timestamp:
            await self.react_to_message(self._last_sender, self._last_message_timestamp, emoji)

    async def start_typing(self, recipient: str):
        """Send a '...' placeholder message as typing indicator.

        Signal's sendTyping doesn't work for Note to Self. Instead we send
        a placeholder that gets edited with the real response later.
        """
        if recipient in self._typing_tasks:
            return
        try:
            result = await self.client.send(recipient=recipient, message="...")
            ts = result.get("timestamp") if isinstance(result, dict) else None
            if ts:
                # Store placeholder timestamp so send_message can edit it
                self._typing_tasks[recipient] = ts
                self._sent_timestamps.add(ts)
        except Exception as e:
            logger.debug(f"Signal placeholder send failed: {e}")

    async def stop_typing(self, recipient: str):
        """Clean up typing placeholder if it wasn't edited."""
        self._typing_tasks.pop(recipient, None)

    async def start(self):
        """Start the SSE event loop."""
        await self.client.start()
        self._running = True
        logger.info(f"Signal bot started (account: {self.settings.signal_account})")

        try:
            async for event in self.client.events():
                if not self._running:
                    break
                try:
                    await self._handle_event(event)
                except Exception as e:
                    logger.exception(f"Signal event handler error: {e}")
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False

    async def stop(self):
        """Stop the bot."""
        self._running = False
        for task in self._typing_tasks.values():
            task.cancel()
        self._typing_tasks.clear()
        await self.client.close()
        logger.info("Signal bot stopped")
