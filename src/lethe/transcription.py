"""Speech-to-text helpers for Telegram audio messages."""

import asyncio
import base64
import logging
import os
import shlex
import shutil
import tempfile
from pathlib import Path

import httpx

from lethe.config import Settings

logger = logging.getLogger(__name__)

OPENAI_TRANSCRIPTIONS_URL = "https://api.openai.com/v1/audio/transcriptions"
OPENROUTER_TRANSCRIPTIONS_URL = "https://openrouter.ai/api/v1/audio/transcriptions"

DEFAULT_OPENAI_MODEL = "whisper-1"
DEFAULT_OPENROUTER_MODEL = "openai/whisper-1"
DEFAULT_LOCAL_MODEL = "base"

_MIME_TO_FORMAT = {
    "audio/aac": "aac",
    "audio/flac": "flac",
    "audio/m4a": "m4a",
    "audio/mp4": "m4a",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/ogg": "ogg",
    "audio/opus": "ogg",
    "audio/wav": "wav",
    "audio/wave": "wav",
    "audio/webm": "webm",
    "video/mp4": "mp4",
    "video/webm": "webm",
}

_EXTENSION_ALIASES = {
    "oga": "ogg",
    "opus": "ogg",
    "mpeg": "mp3",
    "mpga": "mp3",
}

_PLACEHOLDER_API_KEYS = {"", "local"}


class TranscriptionError(RuntimeError):
    """Raised when speech-to-text configuration or provider calls fail."""


def _normalize_provider(provider: str) -> str:
    normalized = provider.strip().lower()
    if normalized in ("", "auto"):
        return ""
    if normalized not in {"local", "openai", "openrouter"}:
        raise TranscriptionError(
            f"Unsupported TRANSCRIPTION_PROVIDER '{provider}'. Use local, openai, or openrouter."
        )
    return normalized


def choose_transcription_provider(settings: Settings) -> str:
    """Choose the transcription provider from settings and available API keys."""
    configured = _normalize_provider(settings.transcription_provider)
    if configured:
        return configured

    if _configured_api_key(settings.openrouter_api_key or os.environ.get("OPENROUTER_API_KEY", "")):
        return "openrouter"
    if _configured_api_key(settings.openai_api_key or os.environ.get("OPENAI_API_KEY", "")):
        return "openai"
    if _local_whisper_available(settings):
        return "local"

    raise TranscriptionError(
        "Speech-to-text is not configured. Set OPENROUTER_API_KEY or OPENAI_API_KEY, "
        "or set TRANSCRIPTION_PROVIDER=local with a local Whisper CLI installed."
    )


def default_model_for_provider(provider: str) -> str:
    if provider == "openrouter":
        return DEFAULT_OPENROUTER_MODEL
    if provider == "openai":
        return DEFAULT_OPENAI_MODEL
    if provider == "local":
        return DEFAULT_LOCAL_MODEL
    raise TranscriptionError(f"Unsupported transcription provider: {provider}")


def infer_audio_format(filename: str = "", mime_type: str | None = None) -> str:
    """Infer provider audio format from Telegram metadata."""
    if mime_type:
        mime_format = _MIME_TO_FORMAT.get(mime_type.split(";", 1)[0].strip().lower())
        if mime_format:
            return mime_format

    suffix = Path(filename or "").suffix.lower().lstrip(".")
    if suffix:
        return _EXTENSION_ALIASES.get(suffix, suffix)

    return "ogg"


def _filename_for_upload(filename: str, audio_format: str) -> str:
    """Return a filename whose extension matches the normalized audio format."""
    base = Path(filename or "telegram_audio").stem or "telegram_audio"
    suffix = Path(filename or "").suffix.lower().lstrip(".")
    if suffix == audio_format:
        return filename
    return f"{base}.{audio_format}"


def _api_key_for_provider(provider: str, settings: Settings) -> str:
    if provider == "local":
        return ""
    if provider == "openrouter":
        api_key = settings.openrouter_api_key or os.environ.get("OPENROUTER_API_KEY", "")
        key_name = "OPENROUTER_API_KEY"
    elif provider == "openai":
        api_key = settings.openai_api_key or os.environ.get("OPENAI_API_KEY", "")
        key_name = "OPENAI_API_KEY"
    else:
        raise TranscriptionError(f"Unsupported transcription provider: {provider}")

    if not _configured_api_key(api_key):
        raise TranscriptionError(f"{key_name} is required for {provider} transcription.")
    return api_key


def _configured_api_key(api_key: str) -> bool:
    return api_key.strip().lower() not in _PLACEHOLDER_API_KEYS


def _local_whisper_available(settings: Settings) -> bool:
    command_parts = shlex.split(settings.transcription_local_command or "whisper")
    if not command_parts:
        return False
    return shutil.which(command_parts[0]) is not None


def _provider_error(provider: str, exc: httpx.HTTPStatusError) -> TranscriptionError:
    response_text = exc.response.text.strip()
    if len(response_text) > 500:
        response_text = response_text[:500] + "..."
    return TranscriptionError(
        f"{provider} transcription failed with HTTP {exc.response.status_code}: {response_text}"
    )


async def transcribe_audio(
    audio_bytes: bytes,
    *,
    filename: str,
    mime_type: str | None,
    settings: Settings,
) -> str:
    """Transcribe audio bytes using OpenAI or OpenRouter Whisper-compatible STT."""
    if not audio_bytes:
        raise TranscriptionError("Cannot transcribe an empty audio file.")

    provider = choose_transcription_provider(settings)
    model = settings.transcription_model.strip() or default_model_for_provider(provider)
    language = (settings.transcription_language or "").strip() or None
    audio_format = infer_audio_format(filename, mime_type)

    logger.info(
        "Transcribing Telegram audio via %s model=%s format=%s bytes=%d",
        provider,
        model,
        audio_format,
        len(audio_bytes),
    )

    if provider == "local":
        return await _transcribe_local_whisper(
            audio_bytes,
            filename,
            audio_format,
            model,
            language,
            settings.transcription_local_command,
        )

    api_key = _api_key_for_provider(provider, settings)
    if provider == "openrouter":
        return await _transcribe_openrouter(audio_bytes, audio_format, model, language, api_key)
    return await _transcribe_openai(
        audio_bytes,
        filename,
        audio_format,
        mime_type,
        model,
        language,
        api_key,
    )


async def _transcribe_local_whisper(
    audio_bytes: bytes,
    filename: str,
    audio_format: str,
    model: str,
    language: str | None,
    command: str,
) -> str:
    command_parts = shlex.split(command or "whisper")
    if not command_parts:
        raise TranscriptionError("TRANSCRIPTION_LOCAL_COMMAND cannot be empty.")

    with tempfile.TemporaryDirectory(prefix="lethe-stt-") as tmpdir:
        upload_name = _filename_for_upload(filename, audio_format)
        audio_path = Path(tmpdir) / upload_name
        audio_path.write_bytes(audio_bytes)

        cmd = [
            *command_parts,
            str(audio_path),
            "--model",
            model,
            "--output_format",
            "txt",
            "--output_dir",
            tmpdir,
        ]
        if language:
            cmd.extend(["--language", language])

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            executable = command_parts[0]
            raise TranscriptionError(
                f"Local Whisper command '{executable}' was not found. Install openai-whisper "
                "or set TRANSCRIPTION_LOCAL_COMMAND."
            ) from exc

        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            error_text = stderr.decode("utf-8", errors="replace").strip()
            if len(error_text) > 500:
                error_text = error_text[:500] + "..."
            raise TranscriptionError(
                f"Local Whisper failed with exit code {proc.returncode}: {error_text}"
            )

        transcript_path = audio_path.with_suffix(".txt")
        if transcript_path.exists():
            text = transcript_path.read_text(encoding="utf-8").strip()
        else:
            text = _extract_local_whisper_stdout(stdout.decode("utf-8", errors="replace"))

        if not text:
            raise TranscriptionError("Local Whisper returned an empty transcription.")
        return text


def _extract_local_whisper_stdout(stdout: str) -> str:
    lines = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("[") and "]" in stripped:
            stripped = stripped.split("]", 1)[1].strip()
        lines.append(stripped)
    return "\n".join(lines).strip()


async def _transcribe_openrouter(
    audio_bytes: bytes,
    audio_format: str,
    model: str,
    language: str | None,
    api_key: str,
) -> str:
    payload = {
        "model": model,
        "input_audio": {
            "data": base64.b64encode(audio_bytes).decode("ascii"),
            "format": audio_format,
        },
    }
    if language:
        payload["language"] = language

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            response = await client.post(
                OPENROUTER_TRANSCRIPTIONS_URL,
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise _provider_error("OpenRouter", exc) from exc

    text = response.json().get("text", "").strip()
    if not text:
        raise TranscriptionError("OpenRouter returned an empty transcription.")
    return text


async def _transcribe_openai(
    audio_bytes: bytes,
    filename: str,
    audio_format: str,
    mime_type: str | None,
    model: str,
    language: str | None,
    api_key: str,
) -> str:
    upload_name = _filename_for_upload(filename, audio_format)
    data = {
        "model": model,
        "response_format": "json",
    }
    if language:
        data["language"] = language

    headers = {"Authorization": f"Bearer {api_key}"}
    files = {
        "file": (
            upload_name,
            audio_bytes,
            mime_type or _mime_type_for_format(audio_format),
        )
    }

    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            response = await client.post(
                OPENAI_TRANSCRIPTIONS_URL,
                headers=headers,
                data=data,
                files=files,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise _provider_error("OpenAI", exc) from exc

    text = response.json().get("text", "").strip()
    if not text:
        raise TranscriptionError("OpenAI returned an empty transcription.")
    return text


def _mime_type_for_format(audio_format: str) -> str:
    return {
        "aac": "audio/aac",
        "flac": "audio/flac",
        "m4a": "audio/mp4",
        "mp3": "audio/mpeg",
        "mp4": "audio/mp4",
        "ogg": "audio/ogg",
        "wav": "audio/wav",
        "webm": "audio/webm",
    }.get(audio_format, "application/octet-stream")
