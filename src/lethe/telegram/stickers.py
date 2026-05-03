"""Telegram sticker ingestion, caching, and normalization helpers."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from aiogram.types import Message

from lethe.memory.llm import AsyncLLMClient, LLMConfig, Message as LLMMessage

logger = logging.getLogger(__name__)


@dataclass
class StickerContext:
    file_id: str
    file_unique_id: str | None
    emoji: str | None
    set_name: str | None
    is_animated: bool
    is_video: bool
    width: int | None
    height: int | None
    local_path: Path | None
    preview_path: Path | None
    description: str | None
    error: str | None
    file_size: int | None = None

    def kind(self) -> str:
        if self.is_video:
            return "video"
        if self.is_animated:
            return "animated"
        return "static"

    def short_description(self) -> str:
        parts = []
        if self.emoji:
            parts.append(f'emoji="{self.emoji}"')
        if self.set_name:
            parts.append(f'set="{self.set_name}"')
        parts.append(f"type={self.kind()}")
        if self.width and self.height:
            parts.append(f"size={self.width}x{self.height}")
        if self.description:
            parts.append(f'description="{self.description}"')
        else:
            parts.append("description unavailable")
            if self.error:
                parts.append(f'error="{self.error}"')
        return "[Sticker: " + ", ".join(parts) + "]"

    def to_message_content(self, include_preview: bool = False) -> str | list[dict[str, Any]]:
        text = self.short_description()
        if not include_preview or not self.preview_path or not self.preview_path.exists():
            return text

        mime_type, data = _encode_image_data_uri(self.preview_path)
        if not mime_type or not data:
            return text

        return [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{data}"}},
        ]

    def to_metadata(self) -> dict[str, Any]:
        data = asdict(self)
        data["local_path"] = str(self.local_path) if self.local_path else None
        data["preview_path"] = str(self.preview_path) if self.preview_path else None
        return data


class StickerProcessor:
    """Download, cache, normalize, and describe Telegram stickers."""

    def __init__(
        self,
        *,
        settings: Any,
        bot: Any,
        llm_client_provider: Optional[Callable[[], Optional[AsyncLLMClient]]] = None,
        max_concurrency: int = 2,
        process_timeout: float = 30.0,
        vision_timeout: float = 12.0,
    ):
        self.settings = settings
        self.bot = bot
        self._llm_client_provider = llm_client_provider
        self._cache_root = Path(settings.cache_dir) / "telegram" / "stickers"
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._process_timeout = process_timeout
        self._vision_timeout = vision_timeout

    async def process(self, message: Message) -> StickerContext:
        sticker = message.sticker
        if not sticker:
            return StickerContext(
                file_id="",
                file_unique_id=None,
                emoji=None,
                set_name=None,
                is_animated=False,
                is_video=False,
                width=None,
                height=None,
                local_path=None,
                preview_path=None,
                description=None,
                error="missing sticker payload",
            )

        context = StickerContext(
            file_id=sticker.file_id,
            file_unique_id=getattr(sticker, "file_unique_id", None),
            emoji=getattr(sticker, "emoji", None),
            set_name=getattr(sticker, "set_name", None),
            is_animated=bool(getattr(sticker, "is_animated", False)),
            is_video=bool(getattr(sticker, "is_video", False)),
            width=getattr(sticker, "width", None),
            height=getattr(sticker, "height", None),
            local_path=None,
            preview_path=None,
            description=None,
            error=None,
            file_size=getattr(sticker, "file_size", None),
        )

        cache_dir = self._cache_dir(context)
        metadata_path = cache_dir / "metadata.json"

        async with self._semaphore:
            if metadata_path.exists():
                cached = await asyncio.to_thread(self._load_metadata, metadata_path)
                if cached:
                    return cached

            try:
                context = await asyncio.wait_for(
                    self._download_and_process(context, cache_dir),
                    timeout=self._process_timeout,
                )
            except asyncio.TimeoutError:
                context.error = f"processing timed out after {self._process_timeout:.0f}s"
                context.description = context.description or self._fallback_description(context)
            except Exception as exc:
                logger.exception("Sticker processing failed")
                context.error = str(exc)
                context.description = context.description or self._fallback_description(context)

            await asyncio.to_thread(self._write_metadata, metadata_path, context)
            return context

    async def _download_and_process(self, context: StickerContext, cache_dir: Path) -> StickerContext:
        cache_dir.mkdir(parents=True, exist_ok=True)
        file = await self.bot.get_file(context.file_id)
        file_path = file.file_path or ""
        ext = _sticker_extension(file_path, context)
        original_path = cache_dir / f"original{ext}"

        await self._download_file(file.file_path, original_path)
        context.local_path = original_path

        if ext == ".webp":
            preview_path = cache_dir / "preview.png"
            await asyncio.to_thread(self._render_webp_preview, original_path, preview_path)
            context.preview_path = preview_path if preview_path.exists() else None
        elif ext == ".webm":
            preview_path = cache_dir / "preview.png"
            await self._render_webm_preview(original_path, preview_path)
            context.preview_path = preview_path if preview_path.exists() else None
        elif ext == ".tgs":
            context.error = "tgs rendering unsupported"
        else:
            context.error = f"unsupported sticker format: {ext or 'unknown'}"

        if context.preview_path and context.preview_path.exists():
            if self._can_use_preview_description():
                try:
                    context.description = await self._describe_preview(context)
                except Exception as exc:
                    logger.warning("Sticker vision description failed: %s", exc)
                    context.error = context.error or str(exc)
            else:
                context.error = context.error or "current model does not appear to support image input"

        if not context.description:
            context.description = self._fallback_description(context)

        return context

    async def _download_file(self, file_path: str | None, destination: Path):
        if not file_path:
            raise RuntimeError("Telegram did not return a file path for the sticker")
        await self.bot.download_file(file_path, destination)

    def _cache_dir(self, context: StickerContext) -> Path:
        key = context.file_unique_id or context.file_id
        safe_key = re.sub(r"[^A-Za-z0-9._-]+", "_", key)
        return self._cache_root / safe_key

    def _load_metadata(self, metadata_path: Path) -> Optional[StickerContext]:
        try:
            raw = json.loads(metadata_path.read_text())
            local_path = Path(raw["local_path"]) if raw.get("local_path") else None
            preview_path = Path(raw["preview_path"]) if raw.get("preview_path") else None
            return StickerContext(
                file_id=raw.get("file_id", ""),
                file_unique_id=raw.get("file_unique_id"),
                emoji=raw.get("emoji"),
                set_name=raw.get("set_name"),
                is_animated=bool(raw.get("is_animated", False)),
                is_video=bool(raw.get("is_video", False)),
                width=raw.get("width"),
                height=raw.get("height"),
                local_path=local_path if local_path and local_path.exists() else None,
                preview_path=preview_path if preview_path and preview_path.exists() else None,
                description=raw.get("description"),
                error=raw.get("error"),
                file_size=raw.get("file_size"),
            )
        except Exception:
            return None

    def _write_metadata(self, metadata_path: Path, context: StickerContext):
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        metadata_path.write_text(json.dumps(context.to_metadata(), indent=2, sort_keys=True))

    def _render_webp_preview(self, source: Path, destination: Path):
        from PIL import Image

        with Image.open(source) as img:
            img = img.copy()
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGBA")
            img.thumbnail((512, 512), Image.Resampling.LANCZOS)
            destination.parent.mkdir(parents=True, exist_ok=True)
            img.save(destination, format="PNG", optimize=True)

    async def _render_webm_preview(self, source: Path, destination: Path):
        destination.parent.mkdir(parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-i",
            str(source),
            "-vf",
            "fps=2,scale=256:-1:flags=lanczos,tile=3x2",
            "-frames:v",
            "1",
            str(destination),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError("ffmpeg failed to render webm preview")

    async def _describe_preview(self, context: StickerContext) -> str:
        client = self._build_vision_client()
        if not client or not context.preview_path:
            return self._fallback_description(context)

        client.context.add_message(
            LLMMessage(
                role="user",
                content=[
                    {"type": "text", "text": self._vision_prompt(context)},
                    {"type": "image_url", "image_url": {"url": self._preview_data_uri(context.preview_path)}},
                ],
            )
        )

        try:
            response = await asyncio.wait_for(client._call_api(), timeout=self._vision_timeout)
            content = response["choices"][0]["message"].get("content", "")
            return _compact_description(str(content)) or self._fallback_description(context)
        finally:
            try:
                await client.close()
            except Exception:
                pass

    def _build_vision_client(self) -> Optional[AsyncLLMClient]:
        if not self._llm_client_provider:
            return None
        current = self._llm_client_provider()
        if not current:
            return None

        config = current.config
        clone = AsyncLLMClient(
            config=LLMConfig(
                provider=config.provider,
                model=config.model,
                model_aux=config.model_aux,
                api_base=config.api_base,
                context_limit=config.context_limit,
                max_output_tokens=config.max_output_tokens,
                temperature=config.temperature,
            ),
            system_prompt="You describe stickers in one short sentence.",
            memory_context="",
            usage_scope="telegram_sticker",
        )
        clone._force_oauth = getattr(current, "_force_oauth", None)
        clone.refresh_auth_client()
        return clone

    def _can_use_preview_description(self) -> bool:
        if not self._llm_client_provider:
            return False
        client = self._llm_client_provider()
        if not client:
            return False
        model = (client.config.model or "").lower()
        provider = (client.config.provider or "").lower()
        if any(token in model for token in ("gpt-4o", "gpt-5", "claude", "gemini", "qwen", "kimi", "glm", "grok", "mimo")):
            return True
        return provider in {"anthropic", "openai"}

    def _vision_prompt(self, context: StickerContext) -> str:
        parts = [
            "Describe this Telegram sticker in one short sentence.",
            "Focus on the character, pose, mood, and likely meaning.",
            "Avoid listing obvious metadata or explaining your reasoning.",
        ]
        meta = []
        if context.emoji:
            meta.append(f"emoji={context.emoji}")
        if context.set_name:
            meta.append(f"set={context.set_name}")
        if meta:
            parts.append("Metadata: " + ", ".join(meta))
        return " ".join(parts)

    def _fallback_description(self, context: StickerContext) -> str:
        base = "sticker"
        if context.is_video:
            base = "video sticker"
        elif context.is_animated:
            base = "animated sticker"
        if context.emoji:
            return f"{base} {context.emoji}".strip()
        if context.set_name:
            return f"{base} from set {context.set_name}"
        return base

    def _preview_data_uri(self, preview_path: Path) -> str:
        mime_type, data = _encode_image_data_uri(preview_path)
        if not mime_type or not data:
            raise RuntimeError(f"failed to encode preview image: {preview_path}")
        return f"data:{mime_type};base64,{data}"


def _sticker_extension(file_path: str, context: StickerContext) -> str:
    ext = Path(file_path).suffix.lower()
    if ext in {".webp", ".webm", ".tgs"}:
        return ext
    if context.is_video:
        return ".webm"
    if context.is_animated:
        return ".tgs"
    return ".webp"


def _encode_image_data_uri(path: Path) -> tuple[str, str]:
    ext = path.suffix.lower().lstrip(".")
    mime_map = {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "webp": "image/webp",
    }
    mime_type = mime_map.get(ext)
    if not mime_type:
        return "", ""
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return mime_type, data


def _compact_description(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip().strip('"')
    if len(cleaned) > 220:
        cleaned = cleaned[:217].rstrip() + "..."
    return cleaned

