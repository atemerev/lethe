"""Hippocampus - Pattern completion memory retrieval.

Inspired by biological hippocampus CA3 region which performs autoassociative
pattern completion: given a partial cue, retrieve the complete memory.

Key insight: Don't ask "should I recall?" - just DO similarity search on every
message. Let the similarity threshold filter out irrelevant results. This is
how biological pattern completion works - automatic activation based on
similarity, not explicit decision.

Uses conversation context (not just current message) for search, so even
"do it" or "yes" works when combined with recent dialogue context.
"""

import json
import logging
from typing import Optional

from letta_client import AsyncLetta

from lethe.config import Settings, get_settings

logger = logging.getLogger(__name__)

# Default persona for the hippocampus agent
HIPPOCAMPUS_PERSONA = """You are a memory retrieval assistant. Your job is to decide if looking up memories would benefit the current conversation.

When given a user message, think: would remembering something from past conversations or archival memory help here?

Look for:
- References to people, places, projects, or things mentioned before
- Questions that might have been answered previously
- Credentials, API keys, configurations discussed before
- Patterns, preferences, or decisions made in the past
- Anything where prior context would improve the response

Respond ONLY with valid JSON:
{"should_recall": true/false, "search_query": "query string or null", "reason": "brief reason or null"}

Rules:
- should_recall: true if memory lookup would genuinely help
- search_query: concise query (2-5 words) to search memories
- reason: brief explanation of what you're looking for

Examples:
- "Deploy the app to the server" -> {"should_recall": true, "search_query": "server deployment credentials", "reason": "may need server details from before"}
- "What did we decide about the API design?" -> {"should_recall": true, "search_query": "API design decisions", "reason": "explicit reference to past decision"}
- "Hello!" -> {"should_recall": false, "search_query": null, "reason": null}
- "Fix the bug in auth.py" -> {"should_recall": true, "search_query": "auth.py bugs issues", "reason": "may have discussed this file before"}
- "What's 2+2?" -> {"should_recall": false, "search_query": null, "reason": null}

Be pragmatic - recall when it would actually help, skip for simple or self-contained requests."""


class HippocampusManager:
    """Manages the hippocampus subagent for memory retrieval."""

    def __init__(
        self,
        client: AsyncLetta,
        settings: Optional[Settings] = None,
    ):
        self.client = client
        self.settings = settings or get_settings()
        self._agent_id: Optional[str] = None
        
        # Configuration
        self.agent_name = getattr(self.settings, 'hippocampus_agent_name', 'lethe-hippocampus')
        self.model = getattr(self.settings, 'hippocampus_model', 'anthropic/claude-3-haiku-20240307')
        self.enabled = getattr(self.settings, 'hippocampus_enabled', True)

    async def get_or_create_agent(self) -> str:
        """Get existing hippocampus agent or create a new one."""
        if self._agent_id:
            return self._agent_id

        # Check for existing agent
        agents = self.client.agents.list()
        if hasattr(agents, '__aiter__'):
            async for agent in agents:
                if agent.name == self.agent_name:
                    self._agent_id = agent.id
                    logger.info(f"Found existing hippocampus agent: {self._agent_id}")
                    return self._agent_id
        else:
            for agent in agents:
                if agent.name == self.agent_name:
                    self._agent_id = agent.id
                    logger.info(f"Found existing hippocampus agent: {self._agent_id}")
                    return self._agent_id

        # Create new agent
        logger.info(f"Creating hippocampus agent: {self.agent_name} with model {self.model}")
        agent = await self.client.agents.create(
            name=self.agent_name,
            model=self.model,
            memory_blocks=[
                {"label": "persona", "value": HIPPOCAMPUS_PERSONA, "limit": 2000},
            ],
            tools=[],  # No tools - pure reasoning
            include_base_tools=False,  # No memory tools needed
        )
        self._agent_id = agent.id
        logger.info(f"Created hippocampus agent: {self._agent_id}")
        return self._agent_id

    async def analyze_for_recall(
        self,
        new_message: str,
        recent_messages: list[dict],
    ) -> Optional[dict]:
        """Analyze a new message to determine if memory recall would be beneficial.
        
        Args:
            new_message: The new user message
            recent_messages: List of recent messages [{"role": "user"|"assistant", "content": "..."}]
            
        Returns:
            Dict with keys: should_recall (bool), search_query (str|None), reason (str|None)
            Returns None if hippocampus is disabled or fails
        """
        if not self.enabled:
            return None

        try:
            agent_id = await self.get_or_create_agent()
            
            # Format recent context (brief, just for awareness)
            context_lines = []
            for msg in recent_messages[-5:]:  # Last 5 messages for context
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
            
            context = "\n".join(context_lines) if context_lines else "(new conversation)"
            
            prompt = f"""RECENT CONTEXT:
{context}

NEW USER MESSAGE:
{new_message}

Would looking up memories (past conversations, archival notes, credentials, previous decisions) benefit the response to this message?

Think about:
- Does this reference something from before?
- Would past context improve the answer?
- Are there credentials/configs/patterns we discussed?

JSON only:"""

            # Send to hippocampus agent
            response = await self.client.agents.messages.create(
                agent_id=agent_id,
                messages=[{"role": "user", "content": prompt}],
            )

            # Extract the response text
            result_text = None
            for msg in response.messages:
                if hasattr(msg, 'message_type') and msg.message_type == 'assistant_message':
                    content = msg.content
                    if isinstance(content, str):
                        result_text = content
                    elif isinstance(content, list):
                        for part in content:
                            if hasattr(part, 'text'):
                                result_text = part.text
                                break
                    break

            if not result_text:
                logger.warning("Hippocampus returned no response")
                return None

            # Parse JSON response
            try:
                result = json.loads(result_text)
            except json.JSONDecodeError:
                import re
                json_match = re.search(r'\{[^{}]*\}', result_text)
                if json_match:
                    result = json.loads(json_match.group())
                else:
                    logger.warning(f"Hippocampus returned invalid JSON: {result_text[:200]}")
                    return None

            logger.info(f"Hippocampus analysis: should_recall={result.get('should_recall')}, query={result.get('search_query')}, reason={result.get('reason')}")
            return result

        except Exception as e:
            logger.exception(f"Hippocampus analysis failed: {e}")
            return None

    async def search_memories(
        self,
        main_agent_id: str,
        query: str,
        max_results: int = 3,
    ) -> str:
        """Search the main agent's archival and conversation memory.
        
        Uses semantic similarity search - the query should be conversation
        context, not just keywords.
        
        Args:
            main_agent_id: The main agent's ID to search
            query: Search context (recent messages + new message)
            max_results: Maximum results to return per source
            
        Returns:
            Formatted string with search results, or empty string if none found
        """
        results = []
        
        # Truncate query to avoid BM25 limit (max 1024 Unicode code points)
        if len(query) > 1000:
            query = query[:1000]
        
        try:
            # Search archival memory (passages) - semantic search
            archival_results = await self.client.agents.passages.search(
                agent_id=main_agent_id,
                query=query,
                top_k=max_results,
            )
            
            # Handle async iterator or list
            passages = []
            if hasattr(archival_results, '__aiter__'):
                async for p in archival_results:
                    passages.append(p)
            elif archival_results:
                passages = list(archival_results)
            
            for passage in passages[:max_results]:
                # Check similarity score if available
                score = getattr(passage, 'score', None)
                if score is not None and score < 0.5:
                    continue  # Skip low-relevance results
                
                if hasattr(passage, 'content'):
                    content = passage.content
                elif isinstance(passage, tuple) and len(passage) > 0:
                    content = str(passage[0])
                else:
                    content = str(passage)
                
                # Skip very short or empty results
                if len(content.strip()) < 20:
                    continue
                    
                results.append(f"[Archival] {content}")
                    
        except Exception as e:
            logger.warning(f"Archival search failed: {e}")

        try:
            # Search conversation history - hybrid search (text + semantic)
            conv_results = await self.client.messages.search(
                query=query,
                agent_id=main_agent_id,
                limit=max_results,
            )
            
            # Handle async iterator or list  
            messages = []
            if hasattr(conv_results, '__aiter__'):
                async for m in conv_results:
                    messages.append(m)
            elif conv_results:
                messages = list(conv_results)
            
            for msg in messages[:max_results]:
                if hasattr(msg, 'content'):
                    content = msg.content if isinstance(msg.content, str) else str(msg.content)
                elif hasattr(msg, 'reasoning'):
                    content = msg.reasoning if isinstance(msg.reasoning, str) else str(msg.reasoning)
                elif hasattr(msg, 'text'):
                    content = msg.text
                else:
                    content = str(msg)
                
                # Skip very short results
                if len(content.strip()) < 20:
                    continue
                
                # Get timestamp if available
                timestamp = ""
                if hasattr(msg, 'created_at') and msg.created_at:
                    try:
                        from datetime import datetime
                        if isinstance(msg.created_at, datetime):
                            timestamp = msg.created_at.strftime("%Y-%m-%d %H:%M")
                        else:
                            timestamp = str(msg.created_at)[:16]  # "2024-01-30T14:30"
                    except:
                        pass
                    
                role = getattr(msg, 'message_type', 'message').replace('_message', '')
                if timestamp:
                    results.append(f"[{timestamp}] [{role}] {content}")
                else:
                    results.append(f"[{role}] {content}")
                    
        except Exception as e:
            logger.warning(f"Conversation search failed: {e}")

        if not results:
            return ""
        
        combined = "\n\n".join(results)
        
        # If results are too long, compress via hippocampus
        if len(combined) > 3000:
            combined = await self._compress_memories(combined, query[:200])
            
        return combined

    async def _compress_memories(self, memories: str, query: str) -> str:
        """Compress long memory results using hippocampus agent.
        
        Args:
            memories: The full memory text to compress
            query: The original search query for context
            
        Returns:
            Compressed summary of the memories
        """
        try:
            agent_id = await self.get_or_create_agent()
            
            prompt = f"""The following memories were retrieved for the query "{query}".
They are too long to include in full. Summarize the key relevant information concisely.
Preserve important facts, names, dates, and context. Do not add information that isn't present.

MEMORIES:
{memories}

SUMMARY (be concise but preserve key details):"""

            response = await self.client.agents.messages.create(
                agent_id=agent_id,
                messages=[{"role": "user", "content": prompt}],
            )

            # Extract response text
            for msg in response.messages:
                if hasattr(msg, 'message_type') and msg.message_type == 'assistant_message':
                    content = msg.content
                    if isinstance(content, str):
                        return f"[Compressed summary] {content}"
                    elif isinstance(content, list):
                        for part in content:
                            if hasattr(part, 'text'):
                                return f"[Compressed summary] {part.text}"
            
            # Fallback - return original if compression failed
            logger.warning("Memory compression returned no response, using original")
            return memories
            
        except Exception as e:
            logger.warning(f"Memory compression failed: {e}")
            return memories

    async def augment_message(
        self,
        main_agent_id: str,
        new_message: str,
        recent_messages: list[dict],
    ) -> str:
        """Pattern completion: automatically recall relevant memories.
        
        Like biological hippocampus CA3 - given context, retrieve related memories
        automatically via similarity search. No explicit "should I recall?" decision.
        
        Uses recent conversation context + new message as search query, so even
        short messages like "do it" work when combined with preceding context.
        
        Args:
            main_agent_id: The main agent's ID (for memory search)
            new_message: The new user message
            recent_messages: Recent conversation messages for context
            
        Returns:
            The message, potentially augmented with recalled memories
        """
        if not self.enabled:
            return new_message

        # Build search context from recent messages + new message
        # This enables pattern completion even for short messages like "yes" or "do it"
        context_parts = []
        for msg in recent_messages[-5:]:  # Last 5 messages for context
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    part.get("text", "") for part in content 
                    if isinstance(part, dict) and part.get("type") == "text"
                )
            if content:
                # Truncate very long messages to keep search focused
                if len(content) > 300:
                    content = content[:300]
                context_parts.append(content)
        context_parts.append(new_message)
        
        # Use full context as search query (pattern completion input)
        search_context = "\n".join(context_parts)
        
        # Search memories using semantic similarity
        memories = await self.search_memories(main_agent_id, search_context)
        
        if not memories:
            return new_message

        # Augment the message with recalled memories
        augmented = f"""{new_message}

---
[Memory recall]
{memories}
[End of recall]"""
        
        logger.info(f"Pattern completion: augmented with {len(memories)} chars of recalled memories")
        return augmented

    async def judge_response(
        self,
        original_request: str,
        agent_response: str,
        iteration: int,
        is_continuation: bool,
    ) -> dict:
        """Judge an agent's response - whether to send it and whether to continue.
        
        Args:
            original_request: The user's original request
            agent_response: The agent's latest response (empty if no response)
            iteration: Current iteration number (0-based)
            is_continuation: Whether this is a continuation prompt response
            
        Returns:
            Dict with keys:
            - send_to_user: bool - whether this response should be sent to user
            - continue_task: bool - whether to prompt agent to continue
            - reason: str - brief explanation
        """
        default_result = {"send_to_user": True, "continue_task": False, "reason": "default"}
        
        if not self.enabled:
            return default_result

        try:
            agent_id = await self.get_or_create_agent()
            
            # If no response and early iteration, continue but don't send
            if not agent_response and iteration <= 2:
                return {"send_to_user": False, "continue_task": True, "reason": "no response early iteration"}
            
            # If no response and later iteration, stop
            if not agent_response:
                return {"send_to_user": False, "continue_task": False, "reason": "no response late iteration"}
            
            from datetime import datetime, timezone
            current_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            
            prompt = f"""CURRENT DATE/TIME: {current_time}

USER REQUEST:
{original_request}

AGENT'S LATEST RESPONSE:
{agent_response}

ITERATION: {iteration}

Judge this response:

1. SEND_TO_USER: Should this response be shown to the user?
   - YES if: agent is talking TO the user (answers, confirmations, asking questions)
   - NO if: internal reflection, meta-commentary, thinking out loud

2. CONTINUE_TASK: Should the agent continue working?
   - YES ONLY if: agent has a SPECIFIC, CURRENT task that is clearly incomplete
   - NO if: task completed, nothing more to do, or agent said it's done
   - NO if: the "incomplete task" is from RECALLED MEMORIES (check timestamps!)

CRITICAL - Recalled memories vs current tasks:
- Text inside [Memory recall]...[End of recall] is HISTORICAL CONTEXT
- Timestamps like [2024-01-25 10:30] indicate WHEN something happened
- If a "task" is from days/hours ago, it's NOT a current task - ignore it
- Only consider tasks the user EXPLICITLY requested in THIS message

If continue_task=true, you MUST specify exactly what to continue in "continue_with".
If you can't name a specific current task, set continue_task=false.

Respond with JSON only:
{{"send_to_user": true/false, "continue_task": true/false, "continue_with": "specific task or null", "reason": "brief explanation"}}"""

            response = await self.client.agents.messages.create(
                agent_id=agent_id,
                messages=[{"role": "user", "content": prompt}],
            )

            # Extract response
            result_text = None
            for msg in response.messages:
                if hasattr(msg, 'message_type') and msg.message_type == 'assistant_message':
                    content = msg.content
                    if isinstance(content, str):
                        result_text = content
                    elif isinstance(content, list):
                        for part in content:
                            if hasattr(part, 'text'):
                                result_text = part.text
                                break
                    break

            if not result_text:
                logger.warning("Hippocampus judge_response returned no response")
                return default_result

            # Parse JSON
            try:
                result = json.loads(result_text)
            except json.JSONDecodeError:
                import re
                json_match = re.search(r'\{[^{}]*\}', result_text)
                if json_match:
                    result = json.loads(json_match.group())
                else:
                    logger.warning(f"Hippocampus judge_response invalid JSON: {result_text}")
                    return default_result

            send_to_user = result.get("send_to_user", True)
            continue_task = result.get("continue_task", False)
            reason = result.get("reason", "")
            logger.info(f"Hippocampus judgment: send={send_to_user}, continue={continue_task}, reason={reason}")
            return {"send_to_user": send_to_user, "continue_task": continue_task, "reason": reason}

        except Exception as e:
            logger.warning(f"Hippocampus judge_response failed: {e}")
            return default_result
