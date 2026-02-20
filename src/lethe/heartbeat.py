"""Heartbeat - Periodic check-ins for proactive agent behavior.

Sends periodic internal messages to the agent, allowing it to:
- Review pending tasks and reminders
- Surface important information proactively
- Maintain continuity even when user is away
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Callable, Awaitable, Optional

from lethe.prompts import load_prompt_template
from lethe.utils import strip_model_tags

logger = logging.getLogger(__name__)

# Default interval in seconds (15 minutes)
DEFAULT_HEARTBEAT_INTERVAL = 15 * 60
# Full context heartbeat interval (2 hours)
FULL_CONTEXT_INTERVAL = 2 * 60 * 60

HEARTBEAT_MESSAGE = load_prompt_template(
    "heartbeat_message",
    fallback="[System Heartbeat - {timestamp}]\n\n{reminders}\nRespond with ok unless urgent.",
)

HEARTBEAT_MESSAGE_FULL = load_prompt_template(
    "heartbeat_message_full",
    fallback="[System Heartbeat - {timestamp}]\n\n{reminders}\nFull review. Respond with ok unless urgent.",
)

SUMMARIZE_HEARTBEAT_PROMPT = load_prompt_template(
    "heartbeat_summarize",
    fallback="MESSAGE:\n{response}\n\nReply with ok unless urgent.",
)


class Heartbeat:
    """Manages periodic heartbeat check-ins."""
    
    def __init__(
        self,
        process_callback: Callable[[str], Awaitable[Optional[str]]],
        send_callback: Callable[[str], Awaitable[None]],
        summarize_callback: Optional[Callable[[str], Awaitable[str]]] = None,
        full_context_callback: Optional[Callable[[str], Awaitable[Optional[str]]]] = None,
        get_reminders_callback: Optional[Callable[[], Awaitable[str]]] = None,
        idle_callback: Optional[Callable[[int], Awaitable[None]]] = None,
        interval: int = DEFAULT_HEARTBEAT_INTERVAL,
        full_context_interval: int = FULL_CONTEXT_INTERVAL,
        enabled: bool = True,
    ):
        """Initialize heartbeat.
        
        Args:
            process_callback: Async function to process heartbeat (minimal context)
            send_callback: Async function to send response to user (e.g., Telegram)
            summarize_callback: Async function to summarize/evaluate response before sending
            full_context_callback: Async function to process with full agent context
            get_reminders_callback: Async function to get active reminders as string
            idle_callback: Async function invoked with cumulative quiet minutes when nothing happened
            interval: Seconds between heartbeats
            full_context_interval: Seconds between full context heartbeats (default 2h)
            enabled: Whether heartbeats are enabled
        """
        self.process_callback = process_callback
        self.send_callback = send_callback
        self.summarize_callback = summarize_callback
        self.full_context_callback = full_context_callback
        self.get_reminders_callback = get_reminders_callback
        self.idle_callback = idle_callback
        self.interval = interval
        self.full_context_interval = full_context_interval
        self.enabled = enabled
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._last_full_context: Optional[datetime] = None
        self._heartbeat_count = 0
        self._idle_minutes_accum = 0
        
        logger.info(f"Heartbeat initialized (interval={interval}s, full_context={full_context_interval}s, enabled={enabled})")
    
    async def start(self):
        """Start the heartbeat loop."""
        if not self.enabled:
            logger.info("Heartbeat disabled, not starting")
            return
        
        self._running = True
        self._task = asyncio.create_task(self._heartbeat_loop())
        logger.info("Heartbeat started")
    
    async def stop(self):
        """Stop the heartbeat loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Heartbeat stopped")
    
    async def _heartbeat_loop(self):
        """Main heartbeat loop."""
        # Initial heartbeat on startup
        await self._send_heartbeat()
        
        while self._running:
            try:
                # Wait for interval
                await asyncio.sleep(self.interval)
                
                if not self._running:
                    break
                
                # Send heartbeat
                await self._send_heartbeat()
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"Heartbeat error: {e}")
                # Continue running despite errors
                await asyncio.sleep(60)  # Brief pause before retry
    
    async def _send_heartbeat(self):
        """Send a heartbeat message to the agent."""
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        now = datetime.now(timezone.utc)
        first_tick = self._heartbeat_count == 0
        
        # Get active reminders
        reminders_text = ""
        if self.get_reminders_callback:
            try:
                reminders = await self.get_reminders_callback()
                if reminders:
                    reminders_text = f"Active reminders:\n{reminders}\n\n"
            except Exception as e:
                logger.warning(f"Failed to get reminders: {e}")
        
        # Check if we should do full context heartbeat
        use_full_context = False
        if self.full_context_callback:
            if self._last_full_context is None:
                use_full_context = True  # First heartbeat is always full
            elif (now - self._last_full_context).total_seconds() >= self.full_context_interval:
                use_full_context = True
        
        if use_full_context:
            message = HEARTBEAT_MESSAGE_FULL.format(timestamp=timestamp, reminders=reminders_text)
            self._last_full_context = now
            logger.info(f"Sending FULL CONTEXT heartbeat at {timestamp}")
        else:
            message = HEARTBEAT_MESSAGE.format(timestamp=timestamp, reminders=reminders_text)
            logger.info(f"Sending heartbeat at {timestamp}")
        
        try:
            # Use full context callback for full heartbeats, otherwise minimal
            if use_full_context and self.full_context_callback:
                response = await self.full_context_callback(message)
            else:
                response = await self.process_callback(message)
            
            if not response or not response.strip():
                logger.debug("No heartbeat response")
                return
            
            # Strip model reasoning tags from response
            response = strip_model_tags(response)
            
            # Summarize/evaluate response before deciding to send
            if self.summarize_callback:
                prompt = SUMMARIZE_HEARTBEAT_PROMPT.format(response=response)
                evaluated = await self.summarize_callback(prompt)
                
                # Strip model reasoning tags
                final_response = strip_model_tags(evaluated) if evaluated else "ok"
            else:
                final_response = response.strip()
            
            # "ok" means nothing urgent
            if final_response.lower() == "ok":
                logger.info("Heartbeat: nothing urgent (work saved to history)")
                if not first_tick and self.idle_callback:
                    interval_minutes = max(1, int(round(self.interval / 60)))
                    self._idle_minutes_accum += interval_minutes
                    try:
                        await self.idle_callback(self._idle_minutes_accum)
                    except Exception as e:
                        logger.warning(f"Idle callback failed: {e}")
            else:
                self._idle_minutes_accum = 0
                logger.info(f"Heartbeat sending: {final_response[:100]}...")
                await self.send_callback(final_response)
                
        except Exception as e:
            logger.exception(f"Heartbeat processing failed: {e}")
        finally:
            self._heartbeat_count += 1
    
    async def trigger(self):
        """Manually trigger a heartbeat (for testing)."""
        await self._send_heartbeat()
