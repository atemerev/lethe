"""Hippocampus - Autoassociative memory retrieval subagent.

The hippocampus agent analyzes incoming messages to detect topic changes,
then searches archival and conversation memory to provide relevant context
to the main agent.

Inspired by the biological hippocampus which consolidates and retrieves memories.
"""

import json
import logging
from typing import Optional

from letta_client import AsyncLetta

from lethe.config import Settings, get_settings

logger = logging.getLogger(__name__)

# Default persona for the hippocampus agent
HIPPOCAMPUS_PERSONA = """You are a memory retrieval assistant. Your job is to analyze conversations and detect when topics change.

When given a conversation history and a new message, you must:
1. Determine if the new message introduces a NEW topic or continues the existing topic
2. If it's a new topic, extract a concise search query to find relevant memories

You respond ONLY with valid JSON in this exact format:
{"new_topic": true/false, "search_query": "query string or null", "topic_summary": "brief summary or null"}

Examples:
- User asks about weather after discussing code -> {"new_topic": true, "search_query": "weather preferences", "topic_summary": "User asking about weather"}
- User continues asking about the same code -> {"new_topic": false, "search_query": null, "topic_summary": null}
- User mentions a person's name -> {"new_topic": true, "search_query": "person name context", "topic_summary": "User mentioning person"}

Be concise. Only mark as new_topic if there's a clear shift in subject matter."""


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
        """Analyze a new message to determine if memory recall is needed.
        
        Args:
            new_message: The new user message
            recent_messages: List of recent messages [{"role": "user"|"assistant", "content": "..."}]
            
        Returns:
            Dict with keys: new_topic (bool), search_query (str|None), topic_summary (str|None)
            Returns None if hippocampus is disabled or fails
        """
        if not self.enabled:
            return None

        try:
            agent_id = await self.get_or_create_agent()
            
            # Format the context for analysis
            context_lines = []
            for msg in recent_messages[-5:]:  # Last 5 messages
                role = msg.get("role", "unknown")
                content = msg.get("content", "")
                if isinstance(content, list):
                    # Handle multi-part content
                    content = " ".join(
                        part.get("text", "") for part in content 
                        if isinstance(part, dict) and part.get("type") == "text"
                    )
                context_lines.append(f"{role}: {content[:200]}")  # Truncate long messages
            
            context = "\n".join(context_lines) if context_lines else "(no previous messages)"
            
            prompt = f"""Recent conversation:
{context}

New message from user:
{new_message[:500]}

Analyze: Is this a NEW topic? If yes, what should I search for in memory?
Respond with JSON only."""

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
            # Try to extract JSON from the response (it might have extra text)
            try:
                # First try direct parse
                result = json.loads(result_text)
            except json.JSONDecodeError:
                # Try to find JSON in the response
                import re
                json_match = re.search(r'\{[^{}]*\}', result_text)
                if json_match:
                    result = json.loads(json_match.group())
                else:
                    logger.warning(f"Hippocampus returned invalid JSON: {result_text[:200]}")
                    return None

            logger.info(f"Hippocampus analysis: new_topic={result.get('new_topic')}, query={result.get('search_query')}")
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
        
        Args:
            main_agent_id: The main agent's ID to search
            query: Search query
            max_results: Maximum results to return
            
        Returns:
            Formatted string with search results, or empty string if none found
        """
        results = []
        
        try:
            # Search archival memory (passages)
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
                content = passage.content[:300] if len(passage.content) > 300 else passage.content
                results.append(f"[Archival] {content}")
                    
        except Exception as e:
            logger.warning(f"Archival search failed: {e}")

        try:
            # Search conversation history using organization-wide search with agent filter
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
                content = msg.content if isinstance(msg.content, str) else str(msg.content)
                content = content[:300] if len(content) > 300 else content
                role = getattr(msg, 'message_type', 'message').replace('_message', '')
                results.append(f"[{role}] {content}")
                    
        except Exception as e:
            logger.warning(f"Conversation search failed: {e}")

        if not results:
            return ""
            
        return "\n".join(results)

    async def augment_message(
        self,
        main_agent_id: str,
        new_message: str,
        recent_messages: list[dict],
    ) -> str:
        """Analyze message and augment with relevant memories if needed.
        
        This is the main entry point - call this before sending to the main agent.
        
        Args:
            main_agent_id: The main agent's ID (for memory search)
            new_message: The new user message
            recent_messages: Recent conversation messages
            
        Returns:
            The message, potentially prefixed with recalled memories
        """
        if not self.enabled:
            return new_message

        # Analyze for topic change
        analysis = await self.analyze_for_recall(new_message, recent_messages)
        
        if not analysis or not analysis.get("new_topic") or not analysis.get("search_query"):
            return new_message

        # Search memories
        query = analysis["search_query"]
        memories = await self.search_memories(main_agent_id, query)
        
        if not memories:
            return new_message

        # Augment the message with recalled memories
        topic_summary = analysis.get("topic_summary", "this topic")
        augmented = f"""[HIPPOCAMPUS RECALL - Found relevant context about {topic_summary}]
{memories}
[END RECALL]

{new_message}"""
        
        logger.info(f"Augmented message with hippocampus recall ({len(memories)} chars)")
        return augmented
