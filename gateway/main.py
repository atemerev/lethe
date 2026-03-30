"""Gateway entry point — single Telegram bot that routes to per-user Lethe containers."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

import httpx

from gateway.config import GatewayConfig
from gateway.models import MODEL_CATALOG, PROVIDER_LABELS
from gateway.pool import PoolManager
from gateway.router import Router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


class Gateway:
    """Multi-tenant Telegram gateway for Lethe."""

    def __init__(self, config: GatewayConfig):
        self.config = config
        self.bot = Bot(
            token=config.telegram_bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN),
        )
        self.dp = Dispatcher()
        self.pool = PoolManager(config)
        self.router = Router(self.bot)
        self._register_handlers()

    def _register_handlers(self):
        dp = self.dp

        @dp.message(CommandStart())
        async def cmd_start(message: Message):
            user = message.from_user
            if not user:
                return
            metadata = self._extract_metadata(message)
            container = await self.pool.get_or_assign(user.id, metadata)
            if container:
                # Start proactive event listener
                self.router.start_event_listener(container, message.chat.id)
                await message.answer(
                    f"Welcome, {user.first_name}! Your personal assistant is ready.\n"
                    "Just send me a message to get started."
                )
            else:
                await message.answer(
                    "Sorry, all assistant slots are currently in use. Please try again in a moment."
                )

        @dp.message(Command("stop"))
        async def cmd_stop(message: Message):
            user = message.from_user
            if not user:
                return
            container = self.pool.get_container(user.id)
            if container:
                await self.router.forward_cancel(container, message.chat.id)
                await message.answer("Processing cancelled.")
            else:
                await message.answer("No active session.")

        @dp.message(Command("status"))
        async def cmd_status(message: Message):
            user = message.from_user
            if not user:
                return
            container = self.pool.get_container(user.id)
            pool_status = self.pool.status()
            lines = [
                f"*Gateway Status*",
                f"Your container: {'assigned' if container else 'none'}",
                f"Total containers: {pool_status['total']}",
                f"Active users: {pool_status['users']}",
            ]
            for state, count in pool_status.get("by_state", {}).items():
                lines.append(f"  {state}: {count}")
            await message.answer("\n".join(lines))

        @dp.message(Command("model"))
        async def cmd_model(message: Message):
            user = message.from_user
            if not user:
                return
            container = self.pool.get_container(user.id)
            if not container:
                await message.answer("No active session. Send /start first.")
                return
            await self._show_model_picker(message, container, "main")

        @dp.message(Command("aux"))
        async def cmd_aux(message: Message):
            user = message.from_user
            if not user:
                return
            container = self.pool.get_container(user.id)
            if not container:
                await message.answer("No active session. Send /start first.")
                return
            await self._show_model_picker(message, container, "aux")

        @dp.callback_query(F.data.startswith("main:") | F.data.startswith("aux:"))
        async def handle_model_callback(callback: CallbackQuery):
            user = callback.from_user
            if not user:
                await callback.answer("Unauthorized")
                return
            container = self.pool.get_container(user.id)
            if not container:
                await callback.answer("No active session.")
                return
            await self._handle_model_selection(callback, container)

        @dp.message(F.text)
        async def handle_text(message: Message):
            user = message.from_user
            if not user:
                return
            metadata = self._extract_metadata(message)
            container = await self.pool.get_or_assign(user.id, metadata)
            if not container:
                await message.answer("Sorry, no assistant available right now. Please try again shortly.")
                return

            # Start event listener if not already running
            self.router.start_event_listener(container, message.chat.id)

            await self.router.forward_message(
                container=container,
                chat_id=message.chat.id,
                user_id=user.id,
                message=message.text or "",
                metadata=metadata,
            )

        @dp.message(F.photo)
        async def handle_photo(message: Message):
            user = message.from_user
            if not user:
                return
            metadata = self._extract_metadata(message)
            container = await self.pool.get_or_assign(user.id, metadata)
            if not container:
                await message.answer("Sorry, no assistant available right now.")
                return

            self.router.start_event_listener(container, message.chat.id)

            # Download the photo and save to container workspace
            photo = message.photo[-1]  # Highest resolution
            file = await self.bot.get_file(photo.file_id)
            if file.file_path:
                # Download to container's workspace
                download_dir = Path(container.workspace_path) / "Downloads"
                download_dir.mkdir(parents=True, exist_ok=True)
                local_path = download_dir / f"photo_{message.message_id}.jpg"
                await self.bot.download_file(file.file_path, str(local_path))

                text = message.caption or "Sent a photo."
                metadata["is_photo"] = True
                metadata["photo_path"] = f"/workspace/Downloads/{local_path.name}"

                await self.router.forward_message(
                    container=container,
                    chat_id=message.chat.id,
                    user_id=user.id,
                    message=text,
                    metadata=metadata,
                )

        @dp.message(F.document)
        async def handle_document(message: Message):
            user = message.from_user
            if not user:
                return
            metadata = self._extract_metadata(message)
            container = await self.pool.get_or_assign(user.id, metadata)
            if not container:
                await message.answer("Sorry, no assistant available right now.")
                return

            self.router.start_event_listener(container, message.chat.id)

            doc = message.document
            if doc and doc.file_id:
                file = await self.bot.get_file(doc.file_id)
                if file.file_path:
                    download_dir = Path(container.workspace_path) / "Downloads"
                    download_dir.mkdir(parents=True, exist_ok=True)
                    filename = doc.file_name or f"file_{message.message_id}"
                    local_path = download_dir / filename
                    await self.bot.download_file(file.file_path, str(local_path))

                    text = message.caption or f"Sent a file: {filename}"
                    metadata["is_document"] = True
                    metadata["file_name"] = filename
                    metadata["file_path"] = f"/workspace/Downloads/{filename}"

                    await self.router.forward_message(
                        container=container,
                        chat_id=message.chat.id,
                        user_id=user.id,
                        message=text,
                        metadata=metadata,
                    )

    @staticmethod
    def _build_model_buttons(provider_info: list[dict], kind: str, current: str, current_auth: str = "API") -> list[list[InlineKeyboardButton]]:
        """Build inline keyboard buttons for all available providers."""
        buttons = []
        for info in provider_info:
            provider = info.get("provider", "")
            auth = info.get("auth", "API")
            label = info.get("label", PROVIDER_LABELS.get(provider, provider))
            catalog = MODEL_CATALOG.get(provider, {})
            models = catalog.get(kind, [])
            if not models:
                continue
            buttons.append([InlineKeyboardButton(text=f"── {label} ──", callback_data="noop")])
            for name, model_id, pricing in models:
                is_active = model_id == current and auth == current_auth
                marker = "✅ " if is_active else ""
                suffix = "" if auth == "sub" else f" ({pricing})"
                btn_text = f"{marker}{name}{suffix}"
                cb_data = f"{kind}:{auth}:{model_id}"
                if len(cb_data) > 64:
                    cb_data = cb_data[:64]
                buttons.append([InlineKeyboardButton(text=btn_text, callback_data=cb_data)])
        return buttons

    async def _show_model_picker(self, message: Message, container, kind: str):
        """Show inline keyboard with model options, fetching current model from worker."""
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{container.api_url}/model")
                data = resp.json()
        except Exception as e:
            await message.answer(f"Failed to get model info: {e}")
            return

        provider_info = data.get("provider_info") or [{"provider": data.get("provider", "openrouter"), "label": data.get("provider", "openrouter")}]
        current = data.get("model") if kind == "main" else data.get("model_aux")
        current_auth = data.get("current_auth", "API")
        label = "Main model" if kind == "main" else "Aux model"

        buttons = self._build_model_buttons(provider_info, kind, current, current_auth)
        if not buttons:
            await message.answer("No models available.")
            return

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        await message.answer(
            f"{label}: `{current}`\n\nSelect new model:",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )

    async def _handle_model_selection(self, callback: CallbackQuery, container):
        """Handle model selection callback by updating the worker via API."""
        data = callback.data or ""
        if data == "noop":
            await callback.answer()
            return

        # Parse: kind:auth:model_id
        parts = data.split(":", 2)
        if len(parts) < 3:
            if len(parts) == 2:
                kind, model_id = parts
                auth = "API"
            else:
                await callback.answer("Unknown selection.")
                return
        else:
            kind, auth, model_id = parts

        if kind not in ("main", "aux"):
            await callback.answer("Unknown selection.")
            return

        if kind == "main":
            payload = {"model": model_id, "auth": auth}
        else:
            payload = {"model_aux": model_id, "auth": auth}

        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.post(f"{container.api_url}/model", json=payload)
                result = resp.json()
        except Exception as e:
            await callback.answer(f"Failed: {e}")
            return

        label = "Main model" if kind == "main" else "Aux model"
        changed = result.get("changed", {}).get(kind, {})
        old_model = changed.get("old", "?")

        await callback.answer(f"Switched to {model_id}")

        # Update keyboard to reflect new selection
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                info = (await client.get(f"{container.api_url}/model")).json()
            pi = info.get("provider_info") or [{"provider": info.get("provider", "openrouter"), "label": info.get("provider", "openrouter")}]
            ca = info.get("current_auth", auth)
            buttons = self._build_model_buttons(pi, kind, model_id, ca)
            keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
            await callback.message.edit_text(
                f"{label}: `{model_id}`\n\n✅ Switched from `{old_model}`",
                reply_markup=keyboard,
                parse_mode="Markdown",
            )
        except Exception:
            pass

    def _extract_metadata(self, message: Message) -> dict:
        """Extract user metadata from a Telegram message."""
        user = message.from_user
        return {
            "username": user.username if user else "",
            "first_name": user.first_name if user else "",
            "message_id": message.message_id,
        }

    async def run(self):
        """Start the gateway."""
        logger.info("Starting Lethe Gateway")
        logger.info("Pool size: %d", self.config.pool_size)
        logger.info("Image: %s", self.config.lethe_image)
        logger.info("Workspace base: %s", self.config.workspace_base)

        # Initialize pool
        await self.pool.start()

        # Background tasks
        pool_task = asyncio.create_task(self._pool_maintenance())
        reap_task = asyncio.create_task(self._reap_loop())

        # Set up shutdown
        shutdown_event = asyncio.Event()

        def signal_handler():
            logger.info("Received shutdown signal")
            shutdown_event.set()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, signal_handler)

        # Start polling
        bot_task = asyncio.create_task(
            self.dp.start_polling(self.bot, handle_signals=False)
        )

        logger.info("Gateway is running")

        try:
            await shutdown_event.wait()
        except asyncio.CancelledError:
            pass
        finally:
            logger.info("Shutting down gateway...")
            pool_task.cancel()
            reap_task.cancel()
            for task in (pool_task, reap_task, bot_task):
                try:
                    task.cancel()
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            await self.dp.stop_polling()
            logger.info("Gateway shut down")

    async def _pool_maintenance(self):
        """Periodically ensure pool has enough idle containers."""
        while True:
            try:
                await self.pool.ensure_pool()
            except Exception as e:
                logger.error("Pool maintenance error: %s", e)
            await asyncio.sleep(30)

    async def _reap_loop(self):
        """Periodically reap idle containers."""
        while True:
            await asyncio.sleep(3600)  # Every hour
            try:
                await self.pool.reap_idle()
            except Exception as e:
                logger.error("Reap error: %s", e)


def main():
    config = GatewayConfig()

    if not config.telegram_bot_token:
        print("ERROR: TELEGRAM_BOT_TOKEN environment variable is required")
        sys.exit(1)

    gateway = Gateway(config)

    try:
        asyncio.run(gateway.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
