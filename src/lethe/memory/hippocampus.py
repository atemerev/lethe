"""Hippocampus - Pattern completion memory retrieval.

Inspired by biological hippocampus CA3 region which performs autoassociative
pattern completion: given a partial cue, retrieve the complete memory.

Uses LLM to decide if recall would help and generate concise search queries.
This produces better results than raw message similarity search.
"""

import json
import logging
import os
import re
from collections import deque
from typing import Optional, Callable, Awaitable
from datetime import datetime, timezone

from lethe.prompts import load_prompt_template

logger = logging.getLogger(__name__)

# Max lines of recalled memories before summarization
MAX_RECALL_LINES = 500
# Hard cap for final recall payload size (approximate token budget)
MAX_RECALL_TOKENS = int(os.environ.get("HIPPOCAMPUS_MAX_RECALL_TOKENS", "2500"))
APPROX_CHARS_PER_TOKEN = 4
MAX_RECALL_CHARS = MAX_RECALL_TOKENS * APPROX_CHARS_PER_TOKEN
MAX_CONVERSATION_RECALL_ENTRY_CHARS = int(os.environ.get("HIPPOCAMPUS_MAX_CONVERSATION_ENTRY_CHARS", "12000"))

# Minimum score threshold for including memories
MIN_SCORE_THRESHOLD = 0.3

ANALYZE_PROMPT = load_prompt_template(
    "hippocampus_analyze",
    fallback='{"should_recall": false, "search_query": null, "reason": "template missing"}',
)

RELEVANCE_PROMPT = load_prompt_template(
    "hippocampus_relevance",
    fallback="USER MESSAGE: {message}\nMEMORIES:\n{candidates}\nReturn JSON array indices.",
)

SUMMARIZE_PROMPT = load_prompt_template(
    "hippocampus_summarize",
    fallback="Summarize memories:\n{memories}",
)


# Warning added to recall block
ACAUSAL_WARNING = """WARNING: This recall is acausal - these memories may be from the past and do not reflect current state. Do NOT use recalled memories to determine what is done or pending. Use conversation history, todo tools, and memory blocks for current state."""

class Hippocampus:
    """Pattern completion memory retrieval with LLM-guided search.
    
    Uses LLM to:
    1. Decide if memory recall would benefit the conversation
    2. Generate concise search queries (2-5 words) for better similarity matching
    3. Summarize retrieved memories to compress context
    """
    
    def __init__(
        self, 
        memory_store, 
        summarizer: Optional[Callable[[str], Awaitable[str]]] = None,
        analyzer: Optional[Callable[[str], Awaitable[str]]] = None,
        enabled: bool = True,
    ):
        """Initialize hippocampus.
        
        Args:
            memory_store: MemoryStore instance with archival and messages
            summarizer: Async function to summarize memories (uses aux model)
            analyzer: Async function to analyze if recall needed (uses aux model)
            enabled: Whether to enable memory recall
        """
        self.memory = memory_store
        self.summarizer = summarizer
        # Analyzer is optional. If absent, recall falls back to a simple query builder.
        self.analyzer = analyzer
        self.enabled = enabled
        self._stats = {
            "enabled": enabled,
            "calls": 0,
            "recalls": 0,
            "skips": 0,
            "misses": 0,
            "analysis_failures": 0,
            "last_reason": "",
            "last_query": "",
            "last_recall_chars": 0,
            "last_call_at": "",
            "last_message": "",
            "last_recall_preview": "",
        }
        self._trace: deque[dict] = deque(maxlen=50)
        logger.info(f"Hippocampus initialized (enabled={enabled}, summarizer={summarizer is not None})")
    
    async def recall(
        self,
        message: str,
        recent_messages: Optional[list[dict]] = None,
        max_lines: int = MAX_RECALL_LINES,
    ) -> Optional[str]:
        """Recall relevant memories for a user message.
        
        Uses LLM to decide if recall is needed and generate optimized search query.
        
        Args:
            message: The new user message
            recent_messages: Recent conversation context (optional)
            max_lines: Maximum lines of memories before summarization
            
        Returns:
            Formatted (and optionally summarized) memory recall string
        """
        if not self.enabled:
            call_started = datetime.now(timezone.utc)
            self._stats["calls"] += 1
            self._stats["skips"] += 1
            self._stats["last_reason"] = "disabled"
            self._stats["last_call_at"] = call_started.isoformat()
            self._stats["last_message"] = str(message)[:300]
            self._trace.append(
                {
                    "at": call_started.isoformat(),
                    "decision": "skip",
                    "reason": "disabled",
                    "query": "",
                    "result_chars": 0,
                    "latency_ms": 0,
                }
            )
            return None
        
        call_started = datetime.now(timezone.utc)
        self._stats["calls"] += 1
        self._stats["last_call_at"] = call_started.isoformat()
        self._stats["last_message"] = str(message)[:300]
        
        # Step 1: Ask LLM if we should recall and get optimized query
        analysis = await self._analyze_for_recall(message, recent_messages)
        
        if not analysis or not analysis.get("should_recall"):
            reason = analysis.get("reason") if analysis else "analysis failed"
            logger.info(f"Hippocampus: skipping recall - {reason}")
            self._stats["skips"] += 1
            self._stats["last_reason"] = reason
            if not analysis:
                self._stats["analysis_failures"] += 1
            self._trace.append(
                {
                    "at": call_started.isoformat(),
                    "decision": "skip",
                    "reason": reason,
                    "query": "",
                    "result_chars": 0,
                    "latency_ms": int((datetime.now(timezone.utc) - call_started).total_seconds() * 1000),
                }
            )
            return None
        
        search_query = analysis.get("search_query")
        if not search_query:
            logger.warning("Hippocampus: should_recall=True but no search_query")
            self._stats["skips"] += 1
            self._stats["last_reason"] = "empty search_query"
            self._trace.append(
                {
                    "at": call_started.isoformat(),
                    "decision": "skip",
                    "reason": "empty search_query",
                    "query": "",
                    "result_chars": 0,
                    "latency_ms": int((datetime.now(timezone.utc) - call_started).total_seconds() * 1000),
                }
            )
            return None
        self._stats["last_query"] = search_query
        
        logger.info(f"Hippocampus: searching with query '{search_query}' (reason: {analysis.get('reason')})")
        
        # Step 2: Search with LLM-generated query
        archival_results = self._search_archival(search_query)
        conversation_results = self._search_conversations(search_query, exclude_recent=5)
        
        # Step 2.5: Filter for relevance (batch LLM call)
        if self.analyzer and (archival_results or conversation_results):
            archival_results, conversation_results = await self._filter_relevant(
                message, archival_results, conversation_results
            )
        
        # Combine and format results
        memories = self._format_memories(archival_results, conversation_results, max_lines)
        
        if not memories:
            logger.info("Hippocampus: no memories found for query")
            self._stats["misses"] += 1
            self._stats["last_reason"] = "no memories found"
            self._trace.append(
                {
                    "at": call_started.isoformat(),
                    "decision": "miss",
                    "reason": "no memories found",
                    "query": search_query,
                    "result_chars": 0,
                    "latency_ms": int((datetime.now(timezone.utc) - call_started).total_seconds() * 1000),
                }
            )
            return None
        
        # Step 3: Summarize if we have a summarizer
        if self.summarizer:
            result = await self._summarize(memories)
            result = self._cap_recall_payload(result)
            self._stats["recalls"] += 1
            self._stats["last_recall_chars"] = len(result or "")
            self._stats["last_reason"] = analysis.get("reason", "")
            self._stats["last_recall_preview"] = (result or "")[:800]
            self._trace.append(
                {
                    "at": call_started.isoformat(),
                    "decision": "recall",
                    "reason": analysis.get("reason", ""),
                    "query": search_query,
                    "result_chars": len(result or ""),
                    "latency_ms": int((datetime.now(timezone.utc) - call_started).total_seconds() * 1000),
                }
            )
            return result
        else:
            result = (
                "<associative_memory_recall>\n"
                + ACAUSAL_WARNING + "\n\n"
                + memories
                + "\n</associative_memory_recall>"
            )
            result = self._cap_recall_payload(result)
            self._stats["recalls"] += 1
            self._stats["last_recall_chars"] = len(result)
            self._stats["last_reason"] = analysis.get("reason", "")
            self._stats["last_recall_preview"] = result[:800]
            self._trace.append(
                {
                    "at": call_started.isoformat(),
                    "decision": "recall",
                    "reason": analysis.get("reason", ""),
                    "query": search_query,
                    "result_chars": len(result),
                    "latency_ms": int((datetime.now(timezone.utc) - call_started).total_seconds() * 1000),
                }
            )
            return result
    
    async def _analyze_for_recall(
        self,
        message: str,
        recent_messages: Optional[list[dict]] = None,
    ) -> Optional[dict]:
        """Use LLM to decide if recall is needed and generate search query.
        
        Returns:
            Dict with keys: should_recall (bool), search_query (str|None), reason (str)
            Returns None if analysis fails
        """
        # Handle multimodal content (list of parts) - extract text
        if isinstance(message, list):
            text_parts = []
            for part in message:
                if isinstance(part, dict) and part.get("type") == "text":
                    text_parts.append(part.get("text", ""))
            message = " ".join(text_parts) if text_parts else "(image)"
        
        if not self.analyzer:
            # Fallback: always recall with raw query
            return {"should_recall": True, "search_query": message[:100], "reason": "no analyzer"}
        
        try:
            # Build context string
            context = self._format_context(recent_messages)
            
            # Ask LLM
            prompt = ANALYZE_PROMPT.format(context=context, message=message)
            response = await self.analyzer(prompt)
            
            if not response:
                return None
            
            # Parse JSON response
            try:
                # Try direct parse
                result = json.loads(response.strip())
            except json.JSONDecodeError:
                # Try to extract JSON from response
                json_match = re.search(r'\{[^{}]*\}', response)
                if json_match:
                    result = json.loads(json_match.group())
                else:
                    logger.warning(f"Hippocampus: invalid JSON response: {response[:200]}")
                    return None
            
            return result
            
        except Exception as e:
            logger.warning(f"Hippocampus analysis failed: {e}")
            return None
    
    def _format_context(
        self,
        recent_messages: Optional[list[dict]] = None,
    ) -> str:
        """Format recent messages as context for the analyzer."""
        if not recent_messages:
            return "(new conversation)"
        
        context_lines = []
        for msg in recent_messages[-5:]:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    part.get("text", "") for part in content 
                    if isinstance(part, dict) and part.get("type") == "text"
                )
            # Truncate long messages
            if len(content) > 200:
                content = content[:200] + "..."
            context_lines.append(f"{role}: {content}")
        
        return "\n".join(context_lines) if context_lines else "(new conversation)"

    def _build_query(
        self,
        message: str,
        recent_messages: Optional[list[dict]] = None,
    ) -> str:
        """Build a simple keyword query from message + recent user context.

        Kept for compatibility with older tests/workflows.
        """
        parts = [str(message).strip()]
        if recent_messages:
            for msg in recent_messages[-5:]:
                if msg.get("role") != "user":
                    continue
                content = msg.get("content", "")
                if isinstance(content, list):
                    content = " ".join(
                        part.get("text", "")
                        for part in content
                        if isinstance(part, dict) and part.get("type") == "text"
                    )
                content = str(content).strip()
                if content:
                    parts.append(content)

        # Keep query compact and deterministic.
        query = " ".join(parts).strip()
        return query[:200]
    
    def _search_archival(self, query: str, limit: int = 5) -> list[dict]:
        """Search archival memory."""
        try:
            results = self.memory.archival.search(
                query,
                limit=limit,
                search_type="hybrid"
            )
            # Filter by score threshold
            return [r for r in results if r.get("score", 0) >= MIN_SCORE_THRESHOLD]
        except Exception as e:
            logger.warning(f"Archival search failed: {e}")
            return []
    
    def _search_conversations(
        self,
        query: str,
        limit: int = 5,
        exclude_recent: int = 5,
    ) -> list[dict]:
        """Search conversation history, excluding very recent messages."""
        try:
            results = self.memory.messages.search(query, limit=limit + exclude_recent)
            # Skip the most recent messages (they're already in context)
            candidates = results[exclude_recent:] if len(results) > exclude_recent else []
            filtered = [m for m in candidates if self._conversation_entry_allowed(m)]
            dropped = len(candidates) - len(filtered)
            if dropped > 0:
                logger.info("Hippocampus: dropped %s tool/oversized conversation entries", dropped)
            return filtered
        except Exception as e:
            logger.warning(f"Conversation search failed: {e}")
            return []

    def _conversation_entry_allowed(self, msg: dict) -> bool:
        """Filter noisy conversation recall entries.

        We skip tool-role messages and assistant tool-call scaffolding to avoid
        recalling large tool transcripts into the main prompt.
        """
        role = str(msg.get("role", "")).strip().lower()
        metadata = msg.get("metadata", {}) or {}
        content = msg.get("content", "")
        if role == "tool":
            return False
        if metadata.get("tool_call_id") or metadata.get("name"):
            return False
        if role == "assistant" and metadata.get("tool_calls"):
            return False
        if not isinstance(content, str):
            content = str(content)
        if len(content) > MAX_CONVERSATION_RECALL_ENTRY_CHARS:
            return False
        return True
    
    async def _filter_relevant(
        self,
        message: str,
        archival: list[dict],
        conversations: list[dict],
    ) -> tuple[list[dict], list[dict]]:
        """Use LLM to filter out irrelevant memories in one batch call.
        
        Returns:
            Filtered (archival, conversations) tuple
        """
        # Build numbered candidate list for the LLM
        candidates = []
        sources = []  # Track which list each candidate came from
        
        for mem in archival:
            text = mem.get("text", "")
            created = self._format_created_at(mem.get("created_at", ""))
            # Show trimmed preview for LLM to judge
            preview = self._trim_entry(text, max_lines=10)
            candidates.append(f"[{len(candidates)}] [{created}] archival: {preview}")
            sources.append(("archival", mem))
        
        for msg in conversations:
            role = msg.get("role", "?")
            content = msg.get("content", "")
            created = self._format_created_at(msg.get("created_at", ""))
            preview = self._trim_entry(str(content), max_lines=10)
            candidates.append(f"[{len(candidates)}] [{created}] {role}: {preview}")
            sources.append(("conversation", msg))
        
        if not candidates:
            return archival, conversations
        
        candidates_text = "\n\n".join(candidates)
        prompt = RELEVANCE_PROMPT.format(message=message, candidates=candidates_text)
        
        try:
            response = await self.analyzer(prompt)
            if not response:
                return archival, conversations
            
            # Parse JSON array from response
            response = response.strip()
            json_match = re.search(r'\[[\d\s,]*\]', response)
            if json_match:
                relevant_indices = set(json.loads(json_match.group()))
            else:
                logger.warning(f"Hippocampus: invalid relevance response: {response[:200]}")
                return archival, conversations
            
            # Split back into archival and conversation lists
            filtered_archival = []
            filtered_conversations = []
            for idx in relevant_indices:
                if 0 <= idx < len(sources):
                    source_type, item = sources[idx]
                    if source_type == "archival":
                        filtered_archival.append(item)
                    else:
                        filtered_conversations.append(item)
            
            dropped = len(sources) - len(relevant_indices)
            if dropped > 0:
                logger.info(f"Hippocampus: filtered {dropped}/{len(sources)} irrelevant memories")
            
            return filtered_archival, filtered_conversations
            
        except Exception as e:
            logger.warning(f"Hippocampus relevance filter failed: {e}")
            return archival, conversations
    
    @staticmethod
    def _trim_entry(text: str, max_lines: int = 50) -> str:
        """Trim a single memory entry by lines. 
        
        If over max_lines, keep first max_lines. If still over 10K chars
        after line trimming, replace with a placeholder.
        """
        MAX_ENTRY_CHARS = 10000
        
        if not isinstance(text, str):
            text = str(text)
        
        lines = text.split("\n")
        if len(lines) > max_lines:
            text = "\n".join(lines[:max_lines])
        
        # If still huge after line trim (long lines), replace entirely
        if len(text) > MAX_ENTRY_CHARS:
            # Extract a meaningful summary from first line
            first_line = lines[0][:200] if lines else "unknown content"
            return f"[large entry: {len(lines)} lines, {len(text):,} chars — {first_line}]"
        
        return text

    @staticmethod
    def _parse_created_at(value: str) -> Optional[datetime]:
        """Parse a created_at value to datetime."""
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            return None

    @classmethod
    def _format_created_at(cls, value: str) -> str:
        """Format created_at with weekday and UTC marker."""
        dt = cls._parse_created_at(value)
        if not dt:
            return "unknown-time"
        return dt.astimezone(timezone.utc).strftime("%a %Y-%m-%d %H:%M:%S UTC")
    
    def _format_memories(
        self,
        archival: list[dict],
        conversations: list[dict],
        max_lines: int,
    ) -> Optional[str]:
        """Format retrieved memories into a context block."""
        if not archival and not conversations:
            return None
        
        sections = []
        total_lines = 0

        # Preserve timeline semantics inside each recall section.
        archival = sorted(
            archival,
            key=lambda m: self._parse_created_at(m.get("created_at", "")) or datetime.fromtimestamp(0, tz=timezone.utc),
        )
        conversations = sorted(
            conversations,
            key=lambda m: self._parse_created_at(m.get("created_at", "")) or datetime.fromtimestamp(0, tz=timezone.utc),
        )
        
        # Format archival memories
        if archival:
            archival_lines = []
            for mem in archival:
                if total_lines >= max_lines:
                    break
                    
                text = self._trim_entry(mem.get("text", ""))
                created = self._format_created_at(mem.get("created_at", ""))
                
                entry = f"- [{created}] {text}"
                entry_lines = entry.count("\n") + 1
                archival_lines.append(entry)
                total_lines += entry_lines
            
            if archival_lines:
                sections.append("**From long-term memory:**\n" + "\n".join(archival_lines))
        
        # Format conversation memories
        if conversations and total_lines < max_lines:
            conv_lines = []
            for msg in conversations:
                if total_lines >= max_lines:
                    break
                    
                role = msg.get("role", "?")
                content = self._trim_entry(msg.get("content", ""))
                created = self._format_created_at(msg.get("created_at", ""))
                
                entry = f"- [{created}] {role}: {content}"
                entry_lines = entry.count("\n") + 1
                conv_lines.append(entry)
                total_lines += entry_lines
            
            if conv_lines:
                sections.append("**From past conversations:**\n" + "\n".join(conv_lines))
        
        if not sections:
            return None
        
        return "\n\n".join(sections)
    
    async def _summarize(self, memories: str) -> str:
        """Summarize memories using the configured summarizer."""
        try:
            prompt = SUMMARIZE_PROMPT.format(memories=memories)
            summary = await self.summarizer(prompt)
            
            if summary:
                logger.info(f"Summarized {len(memories)} -> {len(summary)} chars")
                return (
                    "<associative_memory_recall summarized=\"true\">\n"
                    + ACAUSAL_WARNING + "\n\n"
                    + summary.strip()
                    + "\n</associative_memory_recall>"
                )
        except Exception as e:
            logger.warning(f"Summarization failed: {e}")
        
        # Fallback to unsummarized
        return (
            "<associative_memory_recall>\n"
            + ACAUSAL_WARNING + "\n\n"
            + memories
            + "\n</associative_memory_recall>"
        )

    def _cap_recall_payload(self, text: Optional[str]) -> str:
        """Cap recall payload to a fixed approximate token budget."""
        if not text:
            return ""
        if len(text) <= MAX_RECALL_CHARS:
            return text
        keep = max(1000, MAX_RECALL_CHARS - 160)
        head = text[:keep]
        omitted = len(text) - keep
        return f"{head}\n\n[... hippocampus recall truncated, omitted {omitted:,} chars ...]"

    async def augment_message(
        self,
        message,  # Can be str or list (multimodal)
        recent_messages: Optional[list[dict]] = None,
    ):
        """Augment a user message with recalled memories.
        
        Args:
            message: The user message (str or multimodal list)
            recent_messages: Recent conversation context
            
        Returns:
            Original message, possibly with appended memory context
        """
        recall = await self.recall(message, recent_messages)
        
        if recall:
            logger.info(f"Hippocampus recalled {len(recall)} chars of context")
            # Handle multimodal content
            if isinstance(message, list):
                # Append recall as text part
                return message + [{"type": "text", "text": f"\n\n{recall}"}]
            return f"{message}\n\n{recall}"
        
        return message

    def get_stats(self) -> dict:
        """Return lightweight runtime stats for monitoring UIs."""
        stats = dict(self._stats)
        calls = max(1, int(stats.get("calls", 0)))
        stats["hit_rate"] = float(stats.get("recalls", 0)) / calls
        stats["recent_trace"] = list(self._trace)
        return stats

    def get_context_view(self) -> str:
        """Build a human-readable context snapshot for dashboard debugging."""
        stats = self.get_stats()
        lines = [
            "# Hippocampus Context",
            "",
            f"- enabled: {stats.get('enabled')}",
            f"- calls: {stats.get('calls', 0)}",
            f"- recalls: {stats.get('recalls', 0)}",
            f"- skips: {stats.get('skips', 0)}",
            f"- misses: {stats.get('misses', 0)}",
            f"- hit_rate: {stats.get('hit_rate', 0.0):.2f}",
            f"- last_call_at: {stats.get('last_call_at') or '-'}",
            f"- last_reason: {stats.get('last_reason') or '-'}",
            f"- last_query: {stats.get('last_query') or '-'}",
            "",
            "## Last message cue",
            stats.get("last_message", "") or "(none)",
            "",
            "## Last recall preview",
            stats.get("last_recall_preview", "") or "(none)",
        ]
        return "\n".join(lines)
