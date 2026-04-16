"""Conversation manager for interruptible async processing.

Handles per-chat conversation state with support for:
- Debouncing: wait for user to finish typing before processing
- Interrupting current processing when new messages arrive
- Accumulating messages during processing
- Resuming with combined context after interrupt
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Default debounce time in seconds
DEFAULT_DEBOUNCE_SECONDS = 5.0


@dataclass
class PendingMessage:
    """A message waiting to be processed."""
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class ConversationState:
    """State for a single chat conversation."""
    chat_id: str
    user_id: str
    pending_messages: list[PendingMessage] = field(default_factory=list)
    is_processing: bool = False
    is_debouncing: bool = False
    interrupt_event: asyncio.Event = field(default_factory=asyncio.Event)
    debounce_event: asyncio.Event = field(default_factory=asyncio.Event)  # Signals new message during debounce
    current_task: Optional[asyncio.Task] = None
    debounce_task: Optional[asyncio.Task] = None
    
    def add_message(self, content: str, metadata: Optional[dict] = None) -> tuple[bool, bool]:
        """Add a message to pending.
        
        Returns:
            Tuple of (interrupted_processing, interrupted_debounce)
        """
        self.pending_messages.append(PendingMessage(
            content=content,
            metadata=metadata or {},
        ))
        
        interrupted_processing = False
        interrupted_debounce = False
        
        if self.is_processing:
            self.interrupt_event.set()
            logger.info(f"Chat {self.chat_id}: Interrupt signaled (new message while processing)")
            interrupted_processing = True
        
        if self.is_debouncing:
            self.debounce_event.set()
            logger.info(f"Chat {self.chat_id}: Debounce reset (new message)")
            interrupted_debounce = True
        
        return interrupted_processing, interrupted_debounce
    
    def get_combined_message(self) -> tuple[str, dict]:
        """Get all pending messages combined into one, clearing the pending list.
        
        Returns:
            Tuple of (combined_content, merged_metadata)
        """
        if not self.pending_messages:
            return "", {}
        
        if len(self.pending_messages) == 1:
            msg = self.pending_messages.pop(0)
            return msg.content, msg.metadata
        
        # Multiple messages - combine them
        contents = []
        merged_metadata = {}
        
        for msg in self.pending_messages:
            contents.append(msg.content)
            # Merge metadata, later messages override earlier
            merged_metadata.update(msg.metadata)
        
        self.pending_messages.clear()
        
        # Format combined messages - simple newline separation for user messages
        combined = "\n\n".join(contents)
        return combined, merged_metadata
    
    def check_interrupt(self) -> bool:
        """Check if interrupt was requested. Clears the event."""
        if self.interrupt_event.is_set():
            self.interrupt_event.clear()
            return True
        return False


class ConversationManager:
    """Manages conversation state across multiple chats with debouncing."""
    
    def __init__(self, debounce_seconds: float = DEFAULT_DEBOUNCE_SECONDS):
        """Initialize conversation manager.
        
        Args:
            debounce_seconds: Time to wait for additional messages before processing.
                             Set to 0 to disable debouncing.
        """
        self._states: dict[int, ConversationState] = {}
        self._lock = asyncio.Lock()
        self.debounce_seconds = debounce_seconds
    
    def get_or_create_state(self, chat_id: str, user_id: str) -> ConversationState:
        """Get or create conversation state for a chat."""
        if chat_id not in self._states:
            self._states[chat_id] = ConversationState(chat_id=chat_id, user_id=user_id)
        return self._states[chat_id]
    
    async def add_message(
        self,
        chat_id: str,
        user_id: str,
        content: str,
        metadata: Optional[dict] = None,
        process_callback: Optional[Callable] = None,
    ) -> bool:
        """Add a message and start/restart debounce timer.

        Args:
            chat_id: Chat identifier (Telegram numeric ID or Signal phone number)
            user_id: User identifier
            content: Message content
            metadata: Optional metadata (username, attachments, etc.)
            process_callback: Async function to call for processing
                             Signature: async def callback(chat_id, user_id, message, metadata, interrupt_check)
        
        Returns:
            True if message was added
        """
        async with self._lock:
            state = self.get_or_create_state(chat_id, user_id)
            interrupted_processing, interrupted_debounce = state.add_message(content, metadata)
            
            # If currently processing, the interrupt was signaled - processing loop will handle it
            if interrupted_processing:
                logger.info(f"Chat {chat_id}: Message will interrupt processing")
                return True
            
            # If currently debouncing, the debounce was reset - debounce loop will handle it
            if interrupted_debounce:
                logger.info(f"Chat {chat_id}: Message added to batch (debounce reset)")
                return True
            
            # Not processing and not debouncing - process immediately (no debounce for first message)
            if process_callback:
                state.is_processing = True
                state.current_task = asyncio.create_task(
                    self._process_loop(state, process_callback)
                )
            
            return True
    
    async def _debounce_and_process(
        self,
        state: ConversationState,
        process_callback: Callable,
    ):
        """Wait for debounce period, accumulating messages, then process.
        
        If new messages arrive during debounce, the timer resets.
        """
        try:
            while True:
                state.debounce_event.clear()
                
                logger.info(f"Chat {state.chat_id}: Waiting {self.debounce_seconds}s for more messages...")
                
                try:
                    # Wait for either timeout or new message
                    await asyncio.wait_for(
                        state.debounce_event.wait(),
                        timeout=self.debounce_seconds
                    )
                    # New message arrived - loop continues, timer resets
                    logger.info(f"Chat {state.chat_id}: New message during debounce, resetting timer")
                    continue
                except asyncio.TimeoutError:
                    # Debounce period expired - time to process
                    break
            
            # Debounce complete - start processing
            state.is_debouncing = False
            state.debounce_task = None
            
            if state.pending_messages:
                logger.info(f"Chat {state.chat_id}: Debounce complete, processing {len(state.pending_messages)} message(s)")
                state.is_processing = True
                state.current_task = asyncio.create_task(
                    self._process_loop(state, process_callback)
                )
        except asyncio.CancelledError:
            logger.info(f"Chat {state.chat_id}: Debounce cancelled")
            state.is_debouncing = False
            state.debounce_task = None
            raise
    
    async def _process_loop(
        self,
        state: ConversationState,
        process_callback: Callable,
    ):
        """Main processing loop for a conversation.
        
        Continues until no more pending messages.
        Handles interrupts by restarting with combined messages.
        """
        try:
            while state.pending_messages:
                # Clear interrupt flag
                state.interrupt_event.clear()
                
                # Get combined message
                combined, metadata = state.get_combined_message()
                
                if not combined:
                    break
                
                logger.info(f"Chat {state.chat_id}: Processing message ({len(combined)} chars)")
                
                try:
                    # Process the message
                    await process_callback(
                        chat_id=state.chat_id,
                        user_id=state.user_id,
                        message=combined,
                        metadata=metadata,
                        interrupt_check=state.interrupt_event.is_set,
                    )
                except asyncio.CancelledError:
                    logger.info(f"Chat {state.chat_id}: Processing cancelled")
                    raise
                except Exception as e:
                    logger.exception(f"Chat {state.chat_id}: Processing error: {e}")
                    # Continue to process remaining messages
                
                # If interrupted, there will be new messages
                # Start debounce again to let user send more
                if state.interrupt_event.is_set():
                    logger.info(f"Chat {state.chat_id}: Interrupted, starting debounce for new messages")
                    state.interrupt_event.clear()
                    state.is_processing = False
                    
                    if self.debounce_seconds > 0 and state.pending_messages:
                        state.is_debouncing = True
                        state.debounce_task = asyncio.create_task(
                            self._debounce_and_process(state, process_callback)
                        )
                    return  # Exit this loop, debounce will start new one
        finally:
            state.is_processing = False
            state.current_task = None
            logger.info(f"Chat {state.chat_id}: Processing loop finished")
    
    def is_processing(self, chat_id: str) -> bool:
        """Check if a chat is currently being processed."""
        state = self._states.get(chat_id)
        return state.is_processing if state else False
    
    def is_debouncing(self, chat_id: str) -> bool:
        """Check if a chat is currently in debounce period."""
        state = self._states.get(chat_id)
        return state.is_debouncing if state else False
    
    def get_pending_count(self, chat_id: str) -> int:
        """Get number of pending messages for a chat."""
        state = self._states.get(chat_id)
        return len(state.pending_messages) if state else 0
    
    async def cancel(self, chat_id: str) -> bool:
        """Cancel processing and debouncing for a chat.
        
        Returns True if there was something to cancel.
        """
        state = self._states.get(chat_id)
        if not state:
            return False
        
        cancelled = False
        
        # Cancel debounce
        if state.debounce_task and not state.debounce_task.done():
            state.debounce_task.cancel()
            try:
                await state.debounce_task
            except asyncio.CancelledError:
                pass
            cancelled = True
        
        # Cancel processing
        if state.current_task and not state.current_task.done():
            state.current_task.cancel()
            try:
                await state.current_task
            except asyncio.CancelledError:
                pass
            cancelled = True
        
        state.pending_messages.clear()
        state.is_processing = False
        state.is_debouncing = False
        return cancelled
