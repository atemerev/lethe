"""Tests for speech-to-text provider helpers."""

import httpx
import pytest

from lethe.config import Settings
from lethe.transcription import (
    DEFAULT_LOCAL_MODEL,
    DEFAULT_OPENAI_MODEL,
    DEFAULT_OPENROUTER_MODEL,
    OPENAI_TRANSCRIPTIONS_URL,
    OPENROUTER_TRANSCRIPTIONS_URL,
    TranscriptionError,
    choose_transcription_provider,
    infer_audio_format,
    transcribe_audio,
)


def _response(status_code: int, payload: dict) -> httpx.Response:
    return httpx.Response(
        status_code,
        json=payload,
        request=httpx.Request("POST", "https://example.test/transcriptions"),
    )


def test_choose_transcription_provider_prefers_openrouter(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("OPENAI_API_KEY", "oa-key")

    provider = choose_transcription_provider(Settings())

    assert provider == "openrouter"


def test_choose_transcription_provider_honors_explicit_openai(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("OPENAI_API_KEY", "oa-key")

    provider = choose_transcription_provider(Settings(transcription_provider="openai"))

    assert provider == "openai"


def test_choose_transcription_provider_requires_supported_provider():
    with pytest.raises(TranscriptionError, match="Unsupported TRANSCRIPTION_PROVIDER"):
        choose_transcription_provider(Settings(transcription_provider="anthropic"))


def test_choose_transcription_provider_ignores_placeholder_local_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "local")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr("lethe.transcription.shutil.which", lambda command: None)

    with pytest.raises(TranscriptionError, match="Speech-to-text is not configured"):
        choose_transcription_provider(Settings())


def test_choose_transcription_provider_falls_back_to_local_whisper(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr("lethe.transcription.shutil.which", lambda command: "/usr/bin/whisper")

    provider = choose_transcription_provider(Settings())

    assert provider == "local"


def test_choose_transcription_provider_honors_explicit_local(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")

    provider = choose_transcription_provider(Settings(transcription_provider="local"))

    assert provider == "local"


def test_infer_audio_format_from_mime_type_and_extension():
    assert infer_audio_format("voice.oga", "audio/ogg") == "ogg"
    assert infer_audio_format("song.mpga", None) == "mp3"
    assert infer_audio_format("clip.webm", None) == "webm"
    assert infer_audio_format("", None) == "ogg"


@pytest.mark.asyncio
async def test_transcribe_audio_openrouter_request(monkeypatch):
    calls = []

    class DummyClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers=None, json=None, **kwargs):
            calls.append({"url": url, "headers": headers, "json": json, "kwargs": kwargs})
            return _response(200, {"text": "hello from voice"})

    monkeypatch.setattr(httpx, "AsyncClient", DummyClient)
    settings = Settings(openrouter_api_key="or-key")

    text = await transcribe_audio(
        b"audio-bytes",
        filename="voice.ogg",
        mime_type="audio/ogg",
        settings=settings,
    )

    assert text == "hello from voice"
    assert calls[0]["url"] == OPENROUTER_TRANSCRIPTIONS_URL
    assert calls[0]["headers"]["Authorization"] == "Bearer or-key"
    assert calls[0]["json"]["model"] == DEFAULT_OPENROUTER_MODEL
    assert calls[0]["json"]["input_audio"]["format"] == "ogg"
    assert calls[0]["json"]["input_audio"]["data"] == "YXVkaW8tYnl0ZXM="


@pytest.mark.asyncio
async def test_transcribe_audio_openai_request(monkeypatch):
    calls = []

    class DummyClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers=None, data=None, files=None, **kwargs):
            calls.append({
                "url": url,
                "headers": headers,
                "data": data,
                "files": files,
                "kwargs": kwargs,
            })
            return _response(200, {"text": "openai transcript"})

    monkeypatch.setattr(httpx, "AsyncClient", DummyClient)
    settings = Settings(openai_api_key="oa-key", transcription_provider="openai")

    text = await transcribe_audio(
        b"audio-bytes",
        filename="voice.oga",
        mime_type="audio/ogg",
        settings=settings,
    )

    assert text == "openai transcript"
    assert calls[0]["url"] == OPENAI_TRANSCRIPTIONS_URL
    assert calls[0]["headers"]["Authorization"] == "Bearer oa-key"
    assert calls[0]["data"]["model"] == DEFAULT_OPENAI_MODEL
    assert calls[0]["files"]["file"] == ("voice.ogg", b"audio-bytes", "audio/ogg")


@pytest.mark.asyncio
async def test_transcribe_audio_supports_custom_model_and_language(monkeypatch):
    calls = []

    class DummyClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers=None, json=None, **kwargs):
            calls.append(json)
            return _response(200, {"text": "ciao"})

    monkeypatch.setattr(httpx, "AsyncClient", DummyClient)
    settings = Settings(
        openrouter_api_key="or-key",
        transcription_model="openai/whisper-large-v3",
        transcription_language="it",
    )

    await transcribe_audio(b"bytes", filename="audio.wav", mime_type="audio/wav", settings=settings)

    assert calls[0]["model"] == "openai/whisper-large-v3"
    assert calls[0]["language"] == "it"


@pytest.mark.asyncio
async def test_transcribe_audio_local_whisper_request(monkeypatch):
    calls = []

    class DummyProcess:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_create_subprocess_exec(*cmd, stdout=None, stderr=None):
        calls.append({"cmd": cmd, "stdout": stdout, "stderr": stderr})
        return DummyProcess()

    written = {}

    def fake_write_bytes(self, data):
        written[str(self)] = data
        return len(data)

    def fake_exists(self):
        return self.suffix == ".txt"

    def fake_read_text(self, encoding=None):
        return "local transcript"

    monkeypatch.setattr(
        "lethe.transcription.tempfile.TemporaryDirectory",
        lambda prefix: _DummyTempDir("/tmp/lethe-stt-test"),
    )
    monkeypatch.setattr("lethe.transcription.Path.write_bytes", fake_write_bytes)
    monkeypatch.setattr("lethe.transcription.Path.exists", fake_exists)
    monkeypatch.setattr("lethe.transcription.Path.read_text", fake_read_text)
    monkeypatch.setattr(
        "lethe.transcription.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    settings = Settings(transcription_provider="local", transcription_language="en")

    text = await transcribe_audio(
        b"audio-bytes",
        filename="voice.oga",
        mime_type="audio/ogg",
        settings=settings,
    )

    assert text == "local transcript"
    assert written["/tmp/lethe-stt-test/voice.ogg"] == b"audio-bytes"
    assert calls[0]["cmd"] == (
        "whisper",
        "/tmp/lethe-stt-test/voice.ogg",
        "--model",
        DEFAULT_LOCAL_MODEL,
        "--output_format",
        "txt",
        "--output_dir",
        "/tmp/lethe-stt-test",
        "--language",
        "en",
    )


class _DummyTempDir:
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        return self.path

    def __exit__(self, exc_type, exc, tb):
        return False
