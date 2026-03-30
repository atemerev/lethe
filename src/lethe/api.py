"""HTTP API server for Lethe (gateway mode).

Runs instead of Telegram polling when LETHE_MODE=api.
Provides SSE-based chat interface for the gateway to consume.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Optional

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
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


def _sse_encode(event: str, data: dict) -> str:
    """Encode a single SSE frame."""
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ready"})


async def chat(request: Request) -> StreamingResponse:
    """Accept a user message, return SSE stream of response events."""
    body = await request.json()
    message = body.get("message", "")
    user_id = body.get("user_id", 0)
    chat_id = body.get("chat_id", user_id)
    metadata = body.get("metadata", {})

    event_queue: asyncio.Queue = asyncio.Queue()

    from lethe.proxy_bot import ProxyBot
    proxy = ProxyBot(event_queue)

    async def process():
        from lethe.tools import set_telegram_context, set_last_message_id, clear_telegram_context

        set_telegram_context(proxy, chat_id)
        if metadata.get("message_id"):
            set_last_message_id(metadata["message_id"])

        # Mark user activity
        if _agent:
            removed = _agent.llm.clear_idle_markers()
            if removed:
                logger.info("Cleared %d idle marker(s) on incoming API message", removed)
            if _heartbeat:
                _heartbeat.reset_idle_timer("incoming API message")

        try:
            await event_queue.put({"event": "typing_start", "data": {}})

            async def on_intermediate(content: str):
                if not content or len(content) < 10:
                    return
                await event_queue.put({
                    "event": "text",
                    "data": {"content": content, "parse_mode": None, "message_id": 0, "intermediate": True},
                })

            async def on_image(image_path: str):
                await event_queue.put({
                    "event": "file",
                    "data": {"type": "photo", "path": image_path, "caption": "", "message_id": 0},
                })

            response = await _agent.chat(message, on_message=on_intermediate, on_image=on_image)

            if response and response.strip():
                await event_queue.put({
                    "event": "text",
                    "data": {"content": response, "parse_mode": "Markdown", "message_id": 0},
                })

        except Exception as e:
            logger.exception("Error in API chat processing: %s", e)
            await event_queue.put({
                "event": "text",
                "data": {"content": f"Error: {e}", "parse_mode": None, "message_id": 0},
            })
        finally:
            await event_queue.put({"event": "typing_stop", "data": {}})
            await event_queue.put({"event": "done", "data": {}})
            clear_telegram_context()

    task = asyncio.create_task(process())

    async def event_stream():
        try:
            while True:
                ev = await event_queue.get()
                yield _sse_encode(ev["event"], ev["data"])
                if ev["event"] == "done":
                    break
        except asyncio.CancelledError:
            task.cancel()
            raise

    return StreamingResponse(event_stream(), media_type="text/event-stream")


async def cancel(request: Request) -> JSONResponse:
    """Cancel current processing for a chat."""
    body = await request.json()
    chat_id = body.get("chat_id", 0)
    if _conversation_manager and chat_id:
        await _conversation_manager.cancel(chat_id)
    return JSONResponse({"status": "cancelled"})


async def configure(request: Request) -> JSONResponse:
    """Write user metadata into the human memory block."""
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
    """Get or set the main/aux model.

    GET  /model          → {"model", "model_aux", "provider", "available_providers"}
    POST /model          → {"model"} and/or {"model_aux"}, auto-switches provider
    """
    from lethe.models import get_available_providers, provider_for_model

    if not _agent:
        return JSONResponse({"error": "agent not initialized"}, status_code=503)

    config = _agent.llm.config
    if request.method == "GET":
        return JSONResponse({
            "model": config.model,
            "model_aux": config.model_aux,
            "provider": config.provider,
            "available_providers": [p["provider"] for p in get_available_providers()],
            "provider_info": get_available_providers(),
        })

    body = await request.json()
    old_model = config.model
    old_aux = config.model_aux
    changed = {}

    auth_type = body.get("auth", "API")  # "sub" for subscription/OAuth, "API" for key

    if "model" in body:
        new_model = body["model"]
        new_provider = provider_for_model(new_model)
        if new_provider and new_provider != config.provider:
            changed["provider"] = {"old": config.provider, "new": new_provider}
            config.provider = new_provider
            logger.info("Provider changed via API: %s → %s", changed["provider"]["old"], new_provider)
        config.model = new_model
        changed["model"] = {"old": old_model, "new": config.model}
        logger.info("Model changed via API: %s → %s", old_model, config.model)

    if "model_aux" in body:
        config.model_aux = body["model_aux"]
        changed["model_aux"] = {"old": old_aux, "new": config.model_aux}
        logger.info("Aux model changed via API: %s → %s", old_aux, config.model_aux)

    # Set OAuth preference
    if auth_type == "sub":
        _agent.llm._force_oauth = True
        logger.info("OAuth forced ON via API")
    elif auth_type == "API":
        _agent.llm._force_oauth = False
        logger.info("OAuth forced OFF via API, using API key")

    return JSONResponse({
        "status": "updated",
        "model": config.model,
        "model_aux": config.model_aux,
        "provider": config.provider,
        "changed": changed,
    })


async def events(request: Request) -> StreamingResponse:
    """Persistent SSE stream for proactive messages (heartbeat, DMN)."""
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
    await _proactive_queue.put({
        "event": "text",
        "data": {"content": content, "parse_mode": "Markdown", "message_id": 0, "proactive": True},
    })


async def serve_file(request: Request):
    """Serve a file from the container filesystem.
    
    Used by the gateway to fetch files that the agent created
    (e.g. generated images, exports) when the container path
    isn't directly accessible from the host.
    """
    from starlette.responses import FileResponse
    
    path = request.query_params.get("path", "")
    if not path:
        return JSONResponse({"error": "path parameter required"}, status_code=400)
    
    from pathlib import Path as P
    p = P(path)
    if not p.exists():
        return JSONResponse({"error": f"not found: {path}"}, status_code=404)
    if not p.is_file():
        return JSONResponse({"error": f"not a file: {path}"}, status_code=400)
    
    return FileResponse(p)


# Starlette app
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
