"""HTTP client for signal-cli daemon (JSON-RPC 2.0 + SSE).

signal-cli exposes:
- POST /api/v1/rpc — JSON-RPC 2.0 for sending messages, typing, reactions
- GET /api/v1/events — Server-Sent Events stream for receiving messages

See: https://github.com/AsamK/signal-cli/wiki/JSON-RPC-service
"""

import asyncio
import json
import logging
import uuid
from typing import AsyncIterator, Optional

import httpx

logger = logging.getLogger(__name__)

# SSE reconnection parameters
SSE_INITIAL_BACKOFF = 1.0  # seconds
SSE_MAX_BACKOFF = 30.0
SSE_BACKOFF_FACTOR = 2.0
SSE_JITTER = 0.2


class SignalClientError(Exception):
    """Error from signal-cli RPC."""

    def __init__(self, code: int, message: str, data: Optional[dict] = None):
        self.code = code
        self.rpc_message = message
        self.data = data
        super().__init__(f"signal-cli RPC error {code}: {message}")


class SignalClient:
    """HTTP client for signal-cli daemon."""

    def __init__(self, base_url: str = "http://localhost:8080", account: str = ""):
        self.base_url = base_url.rstrip("/")
        self.account = account  # E.164 phone number, e.g. "+15551234567"
        self._http: Optional[httpx.AsyncClient] = None
        self._rpc_id = 0

    async def start(self):
        """Initialize the HTTP client."""
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
        logger.info(f"Signal client connected to {self.base_url} (account: {self.account})")

    async def close(self):
        """Close the HTTP client."""
        if self._http:
            await self._http.aclose()
            self._http = None

    def _next_id(self) -> str:
        self._rpc_id += 1
        return str(self._rpc_id)

    async def rpc(self, method: str, params: Optional[dict] = None) -> dict:
        """Make a JSON-RPC 2.0 call to signal-cli.

        Returns the 'result' field on success.
        Raises SignalClientError on RPC error.
        """
        if not self._http:
            raise RuntimeError("SignalClient not started. Call start() first.")

        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "id": self._next_id(),
        }
        if params:
            payload["params"] = params

        resp = await self._http.post("/api/v1/rpc", json=payload)
        resp.raise_for_status()

        data = resp.json()
        if "error" in data:
            err = data["error"]
            raise SignalClientError(
                code=err.get("code", -1),
                message=err.get("message", "Unknown error"),
                data=err.get("data"),
            )
        return data.get("result", {})

    async def send(
        self,
        recipient: str,
        message: str,
        attachments: Optional[list[str]] = None,
    ) -> dict:
        """Send a text message, optionally with attachments.

        Args:
            recipient: Phone number (E.164) or group ID
            message: Text content
            attachments: List of local file paths to attach

        Returns:
            dict with 'timestamp' of sent message
        """
        params: dict = {"message": message}
        # Use note-to-self when sending to own account (correct Note to Self routing)
        if self.account and recipient == self.account:
            params["note-to-self"] = True
        else:
            params["recipient"] = [recipient]
        if self.account:
            params["account"] = self.account
        if attachments:
            params["attachment"] = attachments
        return await self.rpc("send", params)

    async def send_typing(self, recipient: str, stop: bool = False) -> dict:
        """Send typing indicator."""
        params = {
            "recipient": [recipient],
        }
        if self.account:
            params["account"] = self.account
        if stop:
            params["stop"] = True
        return await self.rpc("sendTyping", params)

    async def send_reaction(
        self,
        recipient: str,
        emoji: str,
        target_author: str,
        target_timestamp: int,
        remove: bool = False,
    ) -> dict:
        """React to a message with an emoji."""
        params = {
            "recipient": [recipient],
            "emoji": emoji,
            "target-author": target_author,
            "target-timestamp": target_timestamp,
        }
        if self.account:
            params["account"] = self.account
        if remove:
            params["remove"] = True
        return await self.rpc("sendReaction", params)

    async def send_read_receipt(self, recipient: str, target_timestamp: int) -> dict:
        """Send a read receipt."""
        params = {
            "recipient": [recipient],
            "target-timestamp": [target_timestamp],
            "type": "read",
        }
        if self.account:
            params["account"] = self.account
        return await self.rpc("sendReceipt", params)

    async def events(self) -> AsyncIterator[dict]:
        """Stream SSE events from signal-cli with auto-reconnection.

        Yields parsed JSON event dicts. Reconnects with exponential backoff
        on connection loss.
        """
        import random

        backoff = SSE_INITIAL_BACKOFF

        while True:
            try:
                async for event in self._sse_stream():
                    yield event
                    backoff = SSE_INITIAL_BACKOFF  # Reset on successful event
            except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as e:
                jitter = random.uniform(1 - SSE_JITTER, 1 + SSE_JITTER)
                wait = min(backoff * jitter, SSE_MAX_BACKOFF)
                logger.warning(f"Signal SSE connection lost ({e}), reconnecting in {wait:.1f}s...")
                await asyncio.sleep(wait)
                backoff = min(backoff * SSE_BACKOFF_FACTOR, SSE_MAX_BACKOFF)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Signal SSE unexpected error: {e}")
                await asyncio.sleep(SSE_MAX_BACKOFF)

    async def _sse_stream(self) -> AsyncIterator[dict]:
        """Raw SSE stream parser. Yields parsed event data dicts."""
        if not self._http:
            raise RuntimeError("SignalClient not started.")

        url = "/api/v1/events"
        if self.account:
            url += f"?account={self.account}"

        async with self._http.stream("GET", url, headers={"Accept": "text/event-stream"}) as resp:
            resp.raise_for_status()
            logger.info("Signal SSE stream connected")

            event_type = ""
            data_lines: list[str] = []

            async for raw_line in resp.aiter_lines():
                line = raw_line.rstrip("\n\r")

                if not line:
                    # Blank line = end of event
                    if data_lines:
                        data_str = "\n".join(data_lines)
                        try:
                            event_data = json.loads(data_str)
                            if event_type:
                                event_data["_event_type"] = event_type
                            yield event_data
                        except json.JSONDecodeError:
                            logger.debug(f"Signal SSE non-JSON data: {data_str[:200]}")
                    event_type = ""
                    data_lines = []
                elif line.startswith("event:"):
                    event_type = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].strip())
                elif line.startswith("id:"):
                    pass  # Event ID, unused
                elif line.startswith(":"):
                    pass  # Comment/keepalive
