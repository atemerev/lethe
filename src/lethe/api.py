"""HTTP API server for Lethe (gateway mode).

Runs instead of Telegram polling when LETHE_MODE=api.
Provides SSE-based chat and proactive event interfaces for the gateway.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from uuid import uuid4

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Route

logger = logging.getLogger(__name__)

# Global references set by run_api() before the server starts
_agent = None
_conversation_manager = None
_actor_system = None
_heartbeat = None
_settings = None

# Queue for proactive/heartbeat events (drained by /events SSE)
_proactive_queue: asyncio.Queue = asyncio.Queue()


@dataclass
class ApiSession:
    """One active SSE chat stream."""

    session_id: str
    chat_id: int
    queue: asyncio.Queue
    closed: bool = False


_api_sessions: dict[str, ApiSession] = {}
_chat_sessions: dict[int, str] = {}
_api_session_lock = asyncio.Lock()


def _sse_encode(event: str, data: dict) -> str:
    """Encode a single SSE frame."""
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


def _expected_api_token() -> str:
    return os.environ.get("LETHE_API_TOKEN", "").strip()


def _presented_api_token(request: Request) -> str:
    bearer = request.headers.get("authorization", "").strip()
    if bearer.lower().startswith("bearer "):
        return bearer[7:].strip()
    return request.headers.get("x-lethe-token", "").strip()


def _auth_error() -> JSONResponse:
    expected = _expected_api_token()
    if not expected:
        logger.error("LETHE_API_TOKEN is missing in API mode")
        return JSONResponse({"error": "server misconfigured"}, status_code=503)
    return JSONResponse({"error": "unauthorized"}, status_code=401)


def _workspace_root() -> Path:
    if _settings and getattr(_settings, "workspace_dir", None):
        return Path(_settings.workspace_dir).resolve()
    return Path(os.environ.get("WORKSPACE_DIR", "/workspace")).resolve()


def _resolve_workspace_path(raw_path: str) -> Optional[Path]:
    if not raw_path:
        return None

    workspace_root = _workspace_root()
    requested = Path(raw_path)
    candidate = requested if requested.is_absolute() else workspace_root / requested
    resolved = candidate.resolve()

    try:
        resolved.relative_to(workspace_root)
    except ValueError:
        return None
    return resolved


async def _require_auth(request: Request) -> Optional[JSONResponse]:
    expected = _expected_api_token()
    presented = _presented_api_token(request)
    if not expected or not secrets.compare_digest(presented, expected):
        return _auth_error()
    return None


async def _close_queue(queue: asyncio.Queue):
    await queue.put({"event": "typing_stop", "data": {}})
    await queue.put({"event": "done", "data": {}})


async def _close_session(session_id: str, *, remove: bool) -> bool:
    """Close an SSE session and optionally remove it from the registry."""
    session: Optional[ApiSession] = None

    async with _api_session_lock:
        session = _api_sessions.get(session_id)
        if not session or session.closed:
            if remove:
                _api_sessions.pop(session_id, None)
            return False

        session.closed = True
        if remove:
            _api_sessions.pop(session_id, None)
        if _chat_sessions.get(session.chat_id) == session_id:
            _chat_sessions.pop(session.chat_id, None)

    await _close_queue(session.queue)
    return True


async def _register_session(chat_id: int, queue: asyncio.Queue) -> str:
    """Register a new SSE session for a chat and close any superseded stream."""
    session_id = uuid4().hex
    previous_session_id: Optional[str] = None

    async with _api_session_lock:
        previous_session_id = _chat_sessions.get(chat_id)
        _api_sessions[session_id] = ApiSession(
            session_id=session_id,
            chat_id=chat_id,
            queue=queue,
        )
        _chat_sessions[chat_id] = session_id

    if previous_session_id and previous_session_id != session_id:
        await _close_session(previous_session_id, remove=True)
    return session_id


async def _unregister_session(session_id: str):
    await _close_session(session_id, remove=True)


async def _session_accepts_events(chat_id: int, session_id: str) -> bool:
    async with _api_session_lock:
        session = _api_sessions.get(session_id)
        return bool(
            session
            and not session.closed
            and _chat_sessions.get(chat_id) == session_id
        )


async def _get_session_queue(session_id: str) -> Optional[asyncio.Queue]:
    async with _api_session_lock:
        session = _api_sessions.get(session_id)
        if not session or session.closed:
            return None
        return session.queue


async def _process_chat_message(
    *,
    chat_id: int,
    user_id: int,
    message: str,
    metadata: dict,
    interrupt_check,
):
    """ConversationManager callback for API chat processing."""
    session_id = str(metadata.get("_api_session_id", "")).strip()
    event_queue = await _get_session_queue(session_id) if session_id else None
    if event_queue is None:
        # Keep the task runnable even if the client disconnected.
        event_queue = asyncio.Queue()

    from lethe.proxy_bot import ProxyBot
    from lethe.tools import clear_telegram_context, set_last_message_id, set_telegram_context

    proxy = ProxyBot(event_queue)

    async def emit(event: str, data: Optional[dict] = None) -> bool:
        if session_id and not await _session_accepts_events(chat_id, session_id):
            return False
        await event_queue.put({"event": event, "data": data or {}})
        return True

    set_telegram_context(proxy, chat_id)
    if metadata.get("message_id"):
        set_last_message_id(metadata["message_id"])

    if _agent:
        removed = _agent.llm.clear_idle_markers()
        if removed:
            logger.info("Cleared %d idle marker(s) on incoming API message", removed)
        if _heartbeat:
            _heartbeat.reset_idle_timer("incoming API message")

    try:
        await emit("typing_start")

        async def on_intermediate(content: str):
            if not content or len(content) < 10 or interrupt_check():
                return
            await emit(
                "text",
                {
                    "content": content,
                    "parse_mode": None,
                    "message_id": 0,
                    "intermediate": True,
                },
            )

        async def on_image(image_path: str):
            if interrupt_check():
                return
            await emit(
                "file",
                {
                    "type": "photo",
                    "path": image_path,
                    "caption": "",
                    "message_id": 0,
                },
            )

        response = await _agent.chat(message, on_message=on_intermediate, on_image=on_image)

        if not interrupt_check() and response and response.strip():
            await emit(
                "text",
                {
                    "content": response,
                    "parse_mode": "Markdown",
                    "message_id": 0,
                },
            )

    except asyncio.CancelledError:
        logger.info("API chat processing cancelled for chat %s", chat_id)
        raise
    except Exception as e:
        logger.exception("Error in API chat processing: %s", e)
        if not interrupt_check():
            await emit(
                "text",
                {
                    "content": f"Error: {e}",
                    "parse_mode": None,
                    "message_id": 0,
                },
            )
    finally:
        try:
            await emit("typing_stop")
            await emit("done")
        finally:
            clear_telegram_context()
            if session_id:
                await _unregister_session(session_id)


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ready"})


async def chat(request: Request) -> StreamingResponse | JSONResponse:
    """Accept a user message and return an SSE stream of response events."""
    auth_error = await _require_auth(request)
    if auth_error:
        return auth_error
    if not _conversation_manager or not _agent:
        return JSONResponse({"error": "agent not initialized"}, status_code=503)

    body = await request.json()
    message = body.get("message", "")
    user_id = body.get("user_id", 0)
    chat_id = body.get("chat_id", user_id)
    metadata = dict(body.get("metadata", {}) or {})

    event_queue: asyncio.Queue = asyncio.Queue()
    session_id = await _register_session(chat_id, event_queue)
    metadata["_api_session_id"] = session_id

    await _conversation_manager.add_message(
        chat_id=chat_id,
        user_id=user_id,
        content=message,
        metadata=metadata,
        process_callback=_process_chat_message,
    )

    async def event_stream():
        try:
            while True:
                ev = await event_queue.get()
                yield _sse_encode(ev["event"], ev["data"])
                if ev["event"] == "done":
                    break
        except asyncio.CancelledError:
            if _chat_sessions.get(chat_id) == session_id and _conversation_manager:
                await _conversation_manager.cancel(chat_id)
            raise
        finally:
            await _unregister_session(session_id)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


async def cancel(request: Request) -> JSONResponse:
    """Cancel current processing for a chat."""
    auth_error = await _require_auth(request)
    if auth_error:
        return auth_error

    body = await request.json()
    chat_id = body.get("chat_id", 0)
    cancelled = False
    if _conversation_manager and chat_id:
        cancelled = await _conversation_manager.cancel(chat_id)
        session_id = _chat_sessions.get(chat_id)
        if session_id:
            await _unregister_session(session_id)
    return JSONResponse({"status": "cancelled", "cancelled": cancelled})


async def configure(request: Request) -> JSONResponse:
    """Write user metadata into the human memory block."""
    auth_error = await _require_auth(request)
    if auth_error:
        return auth_error

    body = await request.json()
    user_id = body.get("user_id", 0)
    username = body.get("username", "")
    first_name = body.get("first_name", "")

    if _agent:
        human_info = f"Name: {first_name}\n"
        if username:
            human_info += f"Telegram: @{username}\n"
        human_info += f"User ID: {user_id}\n"
        _agent.memory.blocks.update("human", human_info)
        _agent.refresh_memory_context()
        logger.info("Configured user metadata: %s (@%s, id=%d)", first_name, username, user_id)

    return JSONResponse({"status": "configured"})


async def model(request: Request) -> JSONResponse:
    """Get or set the main/aux model."""
    auth_error = await _require_auth(request)
    if auth_error:
        return auth_error

    from lethe.models import get_available_providers, provider_for_model

    if not _agent:
        return JSONResponse({"error": "agent not initialized"}, status_code=503)

    config = _agent.llm.config
    if request.method == "GET":
        force = getattr(_agent.llm, "_force_oauth", None)
        if force is True:
            current_auth = "sub"
        elif force is False:
            current_auth = "API"
        elif getattr(_agent.llm, "_oauth", None) and config.provider == getattr(_agent.llm, "_oauth_provider", ""):
            current_auth = "sub"
        else:
            current_auth = "API"

        return JSONResponse(
            {
                "model": config.model,
                "model_aux": config.model_aux,
                "provider": config.provider,
                "current_auth": current_auth,
                "available_providers": [p["provider"] for p in get_available_providers()],
                "provider_info": get_available_providers(),
            }
        )

    body = await request.json()
    force_oauth = None
    auth_type = body.get("auth", "API")
    if auth_type == "sub":
        force_oauth = True
    elif auth_type == "API":
        force_oauth = False

    new_model = body.get("model")
    new_aux = body.get("model_aux")

    target_provider = config.provider
    provider_source = new_model or new_aux
    if provider_source:
        mapped_provider = provider_for_model(provider_source)
        if mapped_provider:
            target_provider = mapped_provider

    changed = await _agent.reconfigure_models(
        provider=target_provider,
        model=new_model,
        model_aux=new_aux,
        force_oauth=force_oauth,
    )

    config = _agent.llm.config
    return JSONResponse(
        {
            "status": "updated",
            "model": config.model,
            "model_aux": config.model_aux,
            "provider": config.provider,
            "changed": changed,
        }
    )


async def events(request: Request) -> StreamingResponse | JSONResponse:
    """Persistent SSE stream for proactive messages (heartbeat, DMN)."""
    auth_error = await _require_auth(request)
    if auth_error:
        return auth_error

    async def event_stream():
        try:
            while True:
                ev = await _proactive_queue.get()
                yield _sse_encode(ev["event"], ev["data"])
        except asyncio.CancelledError:
            return

    return StreamingResponse(event_stream(), media_type="text/event-stream")


async def send_proactive(content: str):
    """Push a proactive message onto the /events stream."""
    await _proactive_queue.put(
        {
            "event": "text",
            "data": {
                "content": content,
                "parse_mode": "Markdown",
                "message_id": 0,
                "proactive": True,
            },
        }
    )


async def serve_file(request: Request) -> FileResponse | JSONResponse:
    """Serve a file from the container workspace only."""
    auth_error = await _require_auth(request)
    if auth_error:
        return auth_error

    raw_path = request.query_params.get("path", "")
    if not raw_path:
        return JSONResponse({"error": "path parameter required"}, status_code=400)

    resolved = _resolve_workspace_path(raw_path)
    if resolved is None:
        return JSONResponse({"error": "path outside workspace"}, status_code=403)
    if not resolved.exists():
        return JSONResponse({"error": f"not found: {raw_path}"}, status_code=404)
    if not resolved.is_file():
        return JSONResponse({"error": f"not a file: {raw_path}"}, status_code=400)

    return FileResponse(resolved)


app = Starlette(
    routes=[
        Route("/health", health, methods=["GET"]),
        Route("/chat", chat, methods=["POST"]),
        Route("/cancel", cancel, methods=["POST"]),
        Route("/configure", configure, methods=["POST"]),
        Route("/model", model, methods=["GET", "POST"]),
        Route("/events", events, methods=["GET"]),
        Route("/file", serve_file, methods=["GET"]),
    ],
)
