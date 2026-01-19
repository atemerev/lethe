"""Telegram tools for sending files/images.

Uses contextvars to access the bot and chat_id from the current task context.
"""

from __future__ import annotations

import json
import os
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Optional

# Context variables set by worker before tool execution
_current_bot: ContextVar[Any] = ContextVar('current_bot', default=None)
_current_chat_id: ContextVar[Optional[int]] = ContextVar('current_chat_id', default=None)


def set_telegram_context(bot: Any, chat_id: int):
    """Set the Telegram context for tool execution.
    
    Called by worker before processing a task.
    """
    _current_bot.set(bot)
    _current_chat_id.set(chat_id)


def clear_telegram_context():
    """Clear the Telegram context after task completion."""
    _current_bot.set(None)
    _current_chat_id.set(None)


async def telegram_send_file_async(
    file_path_or_url: str,
    caption: str = "",
    as_document: bool = False,
) -> str:
    """Send a file or image to the current Telegram chat.
    
    Args:
        file_path_or_url: Local file path or URL to send
        caption: Optional caption for the file
        as_document: If True, send as document even if it's an image
    
    Returns:
        JSON with success status
    """
    from aiogram.types import FSInputFile, URLInputFile, BufferedInputFile
    
    bot = _current_bot.get()
    chat_id = _current_chat_id.get()
    
    if not bot or not chat_id:
        raise RuntimeError("Telegram context not set. This tool can only be used during task processing.")
    
    # Determine source type
    is_url = file_path_or_url.startswith(('http://', 'https://'))
    
    if is_url:
        file_input = URLInputFile(file_path_or_url)
        filename = file_path_or_url.split('/')[-1].split('?')[0]  # Get filename from URL
    else:
        # Local file
        path = Path(file_path_or_url).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"File not found: {path}")
        file_input = FSInputFile(path)
        filename = path.name
    
    # Determine file type by extension
    ext = filename.lower().split('.')[-1] if '.' in filename else ''
    is_image = ext in ('jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp')
    is_video = ext in ('mp4', 'avi', 'mov', 'mkv', 'webm')
    is_audio = ext in ('mp3', 'ogg', 'wav', 'flac', 'm4a')
    is_voice = ext == 'ogg'  # Voice messages are typically ogg
    
    # Send based on type
    if is_image and not as_document:
        result = await bot.send_photo(
            chat_id=chat_id,
            photo=file_input,
            caption=caption or None,
        )
        send_type = "photo"
    elif is_video and not as_document:
        result = await bot.send_video(
            chat_id=chat_id,
            video=file_input,
            caption=caption or None,
        )
        send_type = "video"
    elif is_audio and not as_document:
        result = await bot.send_audio(
            chat_id=chat_id,
            audio=file_input,
            caption=caption or None,
        )
        send_type = "audio"
    else:
        result = await bot.send_document(
            chat_id=chat_id,
            document=file_input,
            caption=caption or None,
        )
        send_type = "document"
    
    return json.dumps({
        "success": True,
        "type": send_type,
        "filename": filename,
        "chat_id": chat_id,
        "message_id": result.message_id,
    })


# Sync version for tool registration (never actually called - async handler is used)
def _is_tool(func):
    """Decorator to mark a function as a tool."""
    func._is_tool = True
    return func


@_is_tool
def telegram_send_file(
    file_path_or_url: str,
    caption: str = "",
    as_document: bool = False,
) -> str:
    """Send a file or image to the current Telegram chat.
    
    Supports local files and URLs. Automatically detects file type:
    - Images (jpg, png, gif, webp): sent as photos
    - Videos (mp4, mov, etc): sent as videos  
    - Audio (mp3, ogg, etc): sent as audio
    - Other files: sent as documents
    
    Args:
        file_path_or_url: Local file path (e.g., "/tmp/chart.png") or URL (e.g., "https://example.com/image.jpg")
        caption: Optional caption to display with the file
        as_document: If True, send as document even if it's an image/video (preserves original quality)
    
    Returns:
        JSON with success status and message details
    """
    raise Exception("Client-side execution required")
