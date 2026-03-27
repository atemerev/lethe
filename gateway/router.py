"""Message router — forwards messages to Lethe containers and relays SSE events to Telegram."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Optional

import httpx
from aiogram import Bot
from aiogram.enums import ChatAction
from aiogram.types import FSInputFile, ReactionTypeEmoji

from gateway.pool import ContainerInfo

logger = logging.getLogger(__name__)

MAX_TG_LENGTH = 4000


class Router:
    """Routes messages between Telegram and Lethe containers via SSE."""

    def __init__(self, bot: Bot):
        self.bot = bot
        self._typing_tasks: dict[int, asyncio.Task] = {}
        # Persistent /events SSE connections (container_id -> task)
        self._event_listeners: dict[str, asyncio.Task] = {}

    async def forward_message(
        self,
        container: ContainerInfo,
        chat_id: int,
        user_id: int,
        message: str,
        metadata: dict,
    ):
        """Send message to container's /chat endpoint and relay SSE events back to Telegram."""
        url = f"{container.api_url}/chat"
        payload = {
            "message": message,
            "user_id": user_id,
            "chat_id": chat_id,
            "metadata": metadata,
        }

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=10)) as client:
                async with client.stream("POST", url, json=payload) as response:
                    if response.status_code != 200:
                        body = await response.aread()
                        logger.error("Container %s returned %d: %s", container.container_name, response.status_code, body[:200])
                        await self.bot.send_message(chat_id, "Sorry, something went wrong. Please try again.")
                        return

                    await self._consume_sse(response, chat_id)
        except httpx.ConnectError:
            logger.error("Cannot connect to container %s at %s", container.container_name, container.api_url)
            await self.bot.send_message(chat_id, "Your assistant is starting up, please try again in a moment.")
        except Exception as e:
            logger.exception("Error forwarding to container %s: %s", container.container_name, e)
            await self.bot.send_message(chat_id, f"Error: {e}")

    async def _consume_sse(self, response: httpx.Response, chat_id: int):
        """Parse SSE stream and relay events to Telegram."""
        event_type = ""
        data_buf = ""

        async for line in response.aiter_lines():
            if line.startswith("event: "):
                event_type = line[7:].strip()
            elif line.startswith("data: "):
                data_buf = line[6:]
            elif line == "":
                # End of SSE frame
                if event_type and data_buf:
                    try:
                        data = json.loads(data_buf)
                    except json.JSONDecodeError:
                        data = {}
                    await self._handle_event(event_type, data, chat_id)
                    if event_type == "done":
                        break
                event_type = ""
                data_buf = ""

    async def _handle_event(self, event: str, data: dict, chat_id: int):
        """Handle a single SSE event by relaying to Telegram."""
        if event == "typing_start":
            await self._start_typing(chat_id)

        elif event == "typing_stop":
            await self._stop_typing(chat_id)

        elif event == "typing":
            # Single typing action from ProxyBot
            try:
                await self.bot.send_chat_action(chat_id, ChatAction.TYPING)
            except Exception:
                pass

        elif event == "text":
            content = data.get("content", "")
            if not content or not content.strip():
                return
            parse_mode = data.get("parse_mode")
            await self._send_text(chat_id, content, parse_mode)

        elif event == "file":
            await self._send_file(chat_id, data)

        elif event == "reaction":
            emoji = data.get("emoji", "👍")
            message_id = data.get("message_id", 0)
            if message_id:
                try:
                    await self.bot.set_message_reaction(
                        chat_id=chat_id,
                        message_id=message_id,
                        reaction=[ReactionTypeEmoji(emoji=emoji)],
                    )
                except Exception as e:
                    logger.warning("Failed to set reaction: %s", e)

        elif event == "done":
            await self._stop_typing(chat_id)

    async def _send_text(self, chat_id: int, text: str, parse_mode: Optional[str] = "Markdown"):
        """Send text to Telegram with --- splitting and chunking (mirrors TelegramBot.send_message)."""
        segments = [s.strip() for s in text.split("---") if s.strip()]

        for i, segment in enumerate(segments):
            if len(segment) <= MAX_TG_LENGTH:
                chunks = [segment]
            else:
                chunks = []
                current = ""
                for line_text in segment.split("\n"):
                    if len(current) + len(line_text) + 1 > MAX_TG_LENGTH:
                        if current:
                            chunks.append(current)
                        current = line_text
                    else:
                        current = f"{current}\n{line_text}" if current else line_text
                if current:
                    chunks.append(current)

            for chunk in chunks:
                try:
                    await self.bot.send_message(chat_id, chunk, parse_mode=parse_mode)
                except Exception:
                    try:
                        await self.bot.send_message(chat_id, chunk, parse_mode=None)
                    except Exception as e:
                        logger.error("Failed to send message chunk: %s", e)
                await asyncio.sleep(0.1)

            # Human-like pause between segments
            if i < len(segments) - 1:
                import random
                think = random.uniform(1.5, 3.0)
                typing = len(segment) * 0.03
                pause = min(think + typing, 10.0)
                pause *= random.uniform(0.8, 1.3)
                await asyncio.sleep(pause)

    async def _send_file(self, chat_id: int, data: dict):
        """Send a file to Telegram based on event data."""
        file_type = data.get("type", "document")
        path = data.get("path", "")
        caption = data.get("caption", "") or None

        if not path:
            return

        # Check if it's a URL or local path
        is_url = path.startswith(("http://", "https://"))

        if is_url:
            file_input = path
        else:
            p = Path(path)
            if not p.exists():
                logger.warning("File not found: %s", path)
                return
            file_input = FSInputFile(p)

        try:
            if file_type == "photo":
                await self.bot.send_photo(chat_id, file_input, caption=caption)
            elif file_type == "animation":
                await self.bot.send_animation(chat_id, file_input, caption=caption)
            elif file_type == "video":
                await self.bot.send_video(chat_id, file_input, caption=caption)
            elif file_type == "voice":
                await self.bot.send_voice(chat_id, file_input, caption=caption)
            elif file_type == "audio":
                await self.bot.send_audio(chat_id, file_input, caption=caption)
            else:
                await self.bot.send_document(chat_id, file_input, caption=caption)
        except Exception as e:
            logger.error("Failed to send %s: %s", file_type, e)

    async def _start_typing(self, chat_id: int):
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

    async def _stop_typing(self, chat_id: int):
        task = self._typing_tasks.pop(chat_id, None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    # --- Proactive event listener ---

    def start_event_listener(self, container: ContainerInfo, chat_id: int):
        """Start listening for proactive events from a container's /events endpoint."""
        if container.container_id in self._event_listeners:
            return
        task = asyncio.create_task(
            self._listen_events(container, chat_id),
            name=f"events-{container.container_name}",
        )
        self._event_listeners[container.container_id] = task

    def stop_event_listener(self, container_id: str):
        """Stop listening for events from a container."""
        task = self._event_listeners.pop(container_id, None)
        if task:
            task.cancel()

    async def _listen_events(self, container: ContainerInfo, chat_id: int):
        """Persistent SSE listener for proactive messages from a container."""
        url = f"{container.api_url}/events"
        while True:
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10)) as client:
                    async with client.stream("GET", url) as response:
                        event_type = ""
                        data_buf = ""
                        async for line in response.aiter_lines():
                            if line.startswith("event: "):
                                event_type = line[7:].strip()
                            elif line.startswith("data: "):
                                data_buf = line[6:]
                            elif line == "":
                                if event_type and data_buf:
                                    try:
                                        data = json.loads(data_buf)
                                    except json.JSONDecodeError:
                                        data = {}
                                    await self._handle_event(event_type, data, chat_id)
                                event_type = ""
                                data_buf = ""
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning("Event listener for %s disconnected: %s, reconnecting...", container.container_name, e)
                await asyncio.sleep(5)

    async def forward_cancel(self, container: ContainerInfo, chat_id: int):
        """Send cancel request to container."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    f"{container.api_url}/cancel",
                    json={"chat_id": chat_id},
                )
        except Exception as e:
            logger.warning("Failed to cancel on container %s: %s", container.container_name, e)
