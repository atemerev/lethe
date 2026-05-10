"""Tests for Telegram sticker processing helpers."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image

from lethe.telegram.stickers import StickerContext, StickerProcessor


class DummyBot:
    def __init__(self, *, file_path: str):
        self.file_path = file_path
        self.get_file = AsyncMock(return_value=SimpleNamespace(file_path=file_path))
        self.download_file = AsyncMock(side_effect=self._download_file)

    async def _download_file(self, file_path, destination):
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if self.file_path.endswith(".webp"):
            img = Image.new("RGBA", (64, 64), (255, 0, 0, 255))
            img.save(destination, format="WEBP")
        elif self.file_path.endswith(".webm"):
            destination.write_bytes(b"webm-bytes")
        else:
            destination.write_bytes(b"sticker-bytes")


def _sticker_message(file_path: str, *, animated: bool = False, video: bool = False):
    sticker = SimpleNamespace(
        file_id="file-id",
        file_unique_id="unique-id",
        emoji="😩",
        set_name="cat_reactions",
        is_animated=animated,
        is_video=video,
        width=512,
        height=512,
        file_size=1234,
    )
    return SimpleNamespace(sticker=sticker, from_user=SimpleNamespace(username="alice", first_name="Alice"), message_id=42, chat=SimpleNamespace(id=99), answer=AsyncMock())


@pytest.mark.asyncio
async def test_static_webp_is_cached_and_previewed(tmp_path, monkeypatch):
    settings = SimpleNamespace(cache_dir=tmp_path)
    bot = DummyBot(file_path="https://example.test/sticker.webp")
    processor = StickerProcessor(settings=settings, bot=bot, llm_client_provider=None)
    message = _sticker_message("sticker.webp")

    context = await processor.process(message)

    assert context.local_path and context.local_path.exists()
    assert context.preview_path and context.preview_path.exists()
    assert context.description == "animated sticker 😩" or context.description == "sticker 😩"
    assert context.error is None or "image input" in context.error or context.error == "tgs rendering unsupported"


@pytest.mark.asyncio
async def test_webm_generates_preview_via_ffmpeg(tmp_path, monkeypatch):
    settings = SimpleNamespace(cache_dir=tmp_path)
    bot = DummyBot(file_path="https://example.test/sticker.webm")
    processor = StickerProcessor(settings=settings, bot=bot, llm_client_provider=None)
    message = _sticker_message("sticker.webm", video=True)

    async def fake_ffmpeg(source, destination):
        destination = Path(destination)
        img = Image.new("RGBA", (64, 64), (0, 255, 0, 255))
        img.save(destination, format="PNG")

    monkeypatch.setattr(processor, "_render_webm_preview", fake_ffmpeg)

    context = await processor.process(message)

    assert context.local_path and context.local_path.suffix == ".webm"
    assert context.preview_path and context.preview_path.exists()
    assert context.kind() == "video"


@pytest.mark.asyncio
async def test_cache_hit_skips_redownload(tmp_path, monkeypatch):
    settings = SimpleNamespace(cache_dir=tmp_path)
    bot = DummyBot(file_path="https://example.test/sticker.webp")
    processor = StickerProcessor(settings=settings, bot=bot, llm_client_provider=None)
    message = _sticker_message("sticker.webp")

    first = await processor.process(message)
    assert first.preview_path and first.preview_path.exists()

    bot.get_file.reset_mock()
    bot.download_file.reset_mock()

    second = await processor.process(message)

    bot.get_file.assert_not_called()
    bot.download_file.assert_not_called()
    assert second.file_unique_id == "unique-id"
    assert second.preview_path and second.preview_path.exists()


@pytest.mark.asyncio
async def test_non_visual_model_falls_back_to_metadata_only(tmp_path, monkeypatch):
    settings = SimpleNamespace(cache_dir=tmp_path)
    bot = DummyBot(file_path="https://example.test/sticker.webp")
    processor = StickerProcessor(settings=settings, bot=bot, llm_client_provider=None)
    message = _sticker_message("sticker.webp")

    context = await processor.process(message)

    content = context.to_message_content(include_preview=False)
    assert isinstance(content, str)
    assert "Sticker" in content

