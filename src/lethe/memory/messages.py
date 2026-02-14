"""Message history - Conversation storage with hybrid search.

Stores conversation messages with vector embeddings for semantic search.
"""

import json
from datetime import datetime, timezone
from typing import Optional, List
import uuid

import lancedb
from lancedb.embeddings import get_registry
import logging

logger = logging.getLogger(__name__)

# Embedding model - same as archival for consistency
EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIM = 384


class MessageHistory:
    """Conversation message storage with hybrid search.
    
    Stores messages with role, content, and embeddings.
    Supports retrieval by time range, semantic search, or keyword search.
    """
    
    TABLE_NAME = "message_history"
    
    def __init__(self, db: lancedb.DBConnection, embedding_model: str = EMBEDDING_MODEL):
        """Initialize message history.
        
        Args:
            db: LanceDB connection
            embedding_model: Sentence transformer model name
        """
        self.db = db
        self.model_name = embedding_model
        
        # Get embedding function from registry
        self.embedder = get_registry().get("sentence-transformers").create(
            name=embedding_model
        )
        
        self._ensure_table()
    
    def _ensure_table(self):
        """Create table if it doesn't exist."""
        if self.TABLE_NAME not in self.db.table_names():
            init_vector = [0.0] * EMBEDDING_DIM
            self.db.create_table(
                self.TABLE_NAME,
                data=[{
                    "id": "_init_",
                    "role": "system",
                    "content": "",
                    "vector": init_vector,
                    "metadata": "{}",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }]
            )
            logger.info(f"Created table {self.TABLE_NAME}")
            
            # Create FTS index for keyword search
            table = self._get_table()
            table.create_fts_index("content", replace=True)
            logger.info("Created FTS index on content column")
    
    def _get_table(self):
        """Get the messages table."""
        return self.db.open_table(self.TABLE_NAME)
    
    def _embed(self, text) -> List[float]:
        """Generate embedding for text (handles multimodal content)."""
        # Handle multimodal content (list of parts)
        if isinstance(text, list):
            text_parts = []
            for part in text:
                if isinstance(part, dict) and part.get("type") == "text":
                    text_parts.append(part.get("text", ""))
            text = " ".join(text_parts)
        
        if not text or not text.strip():
            return [0.0] * EMBEDDING_DIM
        return self.embedder.compute_query_embeddings(text)[0]
    
    def add(
        self,
        role: str,
        content,  # str or list (multimodal)
        metadata: Optional[dict] = None,
    ) -> str:
        """Add a message to history.
        
        Args:
            role: Message role (user, assistant, system, tool)
            content: Message content (str or multimodal list)
            metadata: Optional metadata (tool_call_id, etc.)
            
        Returns:
            Message ID
        """
        message_id = f"msg-{uuid.uuid4()}"
        now = datetime.now(timezone.utc).isoformat()
        
        # Generate embedding (handles multimodal)
        vector = self._embed(content)
        
        # Serialize multimodal content as JSON for storage
        if isinstance(content, list):
            content_str = json.dumps(content)
        else:
            content_str = content
        
        table = self._get_table()
        table.add([{
            "id": message_id,
            "role": role,
            "content": content_str,
            "vector": vector,
            "metadata": json.dumps(metadata or {}),
            "created_at": now,
        }])
        
        logger.debug(f"Added message {message_id} ({role})")
        return message_id
    
    def get(self, message_id: str) -> Optional[dict]:
        """Get a message by ID."""
        table = self._get_table()
        results = table.search().where(f"id = '{message_id}'").limit(1).to_list()
        
        if not results:
            return None
        
        r = results[0]
        return {
            "id": r["id"],
            "role": r["role"],
            "content": r["content"],
            "metadata": json.loads(r["metadata"]) if r["metadata"] else {},
            "created_at": r["created_at"],
        }
    
    def get_recent(self, limit: int = 20) -> List[dict]:
        """Get recent messages (oldest first for context).
        
        Uses PyArrow for efficient sorting since LanceDB search() doesn't
        reliably support ORDER BY.
        """
        table = self._get_table()
        
        # Get full table as Arrow and sort by created_at descending
        arrow_table = table.to_arrow()
        sorted_table = arrow_table.sort_by([("created_at", "descending")])
        
        messages = []
        for i in range(min(limit + 1, sorted_table.num_rows)):  # +1 for possible _init_
            row_id = sorted_table["id"][i].as_py()
            if row_id == "_init_":
                continue
            
            metadata_str = sorted_table["metadata"][i].as_py()
            messages.append({
                "id": row_id,
                "role": sorted_table["role"][i].as_py(),
                "content": sorted_table["content"][i].as_py(),
                "metadata": json.loads(metadata_str) if metadata_str else {},
                "created_at": sorted_table["created_at"][i].as_py(),
            })
            
            if len(messages) >= limit:
                break
        
        # Reverse to get oldest first (chronological order for context)
        return list(reversed(messages))
    
    def search(
        self,
        query: str,
        limit: int = 20,
        search_type: str = "hybrid",
    ) -> List[dict]:
        """Search messages with hybrid search (vector + FTS).
        
        Args:
            query: Search query
            limit: Max results
            search_type: "hybrid", "vector", or "fts"
            
        Returns:
            List of matching messages with scores
        """
        table = self._get_table()
        
        if search_type == "fts":
            # Full-text search only
            results = table.search(query, query_type="fts").limit(limit).to_list()
        elif search_type == "vector":
            # Vector search only
            query_vector = self._embed(query)
            results = table.search(query_vector).limit(limit).to_list()
        else:
            # Hybrid: Run both and merge with RRF (Reciprocal Rank Fusion)
            query_vector = self._embed(query)
            
            # Vector search
            vector_results = table.search(query_vector).limit(limit * 2).to_list()
            
            # FTS search
            try:
                fts_results = table.search(query, query_type="fts").limit(limit * 2).to_list()
            except Exception:
                fts_results = []
            
            # RRF fusion (k=60 is standard)
            k = 60
            scores = {}
            
            for rank, r in enumerate(vector_results):
                doc_id = r["id"]
                scores[doc_id] = scores.get(doc_id, 0) + 1.0 / (k + rank + 1)
            
            for rank, r in enumerate(fts_results):
                doc_id = r["id"]
                scores[doc_id] = scores.get(doc_id, 0) + 1.0 / (k + rank + 1)
            
            # Sort by combined score and get top results
            sorted_ids = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)[:limit]
            
            # Build result list preserving order
            id_to_result = {r["id"]: r for r in vector_results + fts_results}
            results = []
            for doc_id in sorted_ids:
                if doc_id in id_to_result:
                    r = id_to_result[doc_id]
                    r["_score"] = scores[doc_id]
                    results.append(r)
        
        messages = []
        for r in results:
            if r["id"] == "_init_":
                continue
            messages.append({
                "id": r["id"],
                "role": r["role"],
                "content": r["content"],
                "metadata": json.loads(r["metadata"]) if r["metadata"] else {},
                "created_at": r["created_at"],
                "score": r.get("_distance", r.get("_score", 0)),
            })
        
        return messages
    
    def search_by_role(
        self,
        query: str,
        role: str,
        limit: int = 20,
    ) -> List[dict]:
        """Search messages by role with vector search.
        
        Args:
            query: Search query
            role: Filter by role (user, assistant, etc.)
            limit: Max results
            
        Returns:
            List of matching messages
        """
        table = self._get_table()
        query_vector = self._embed(query)
        
        results = (
            table.search(query_vector)
            .where(f"role = '{role}'")
            .limit(limit)
            .to_list()
        )
        
        messages = []
        for r in results:
            if r["id"] == "_init_":
                continue
            messages.append({
                "id": r["id"],
                "role": r["role"],
                "content": r["content"],
                "metadata": json.loads(r["metadata"]) if r["metadata"] else {},
                "created_at": r["created_at"],
                "score": r.get("_distance", r.get("_score", 0)),
            })
        
        return messages
    
    def get_by_role(self, role: str, limit: int = 50) -> List[dict]:
        """Get messages by role (no search, just filter)."""
        table = self._get_table()
        results = table.search().where(f"role = '{role}'").limit(limit).to_list()
        
        messages = []
        for r in results:
            messages.append({
                "id": r["id"],
                "role": r["role"],
                "content": r["content"],
                "metadata": json.loads(r["metadata"]) if r["metadata"] else {},
                "created_at": r["created_at"],
            })
        
        messages.sort(key=lambda m: m["created_at"])
        return messages
    
    def delete(self, message_id: str) -> bool:
        """Delete a message."""
        table = self._get_table()
        table.delete(f"id = '{message_id}'")
        return True
    
    def cleanup_search_results(self, tool_names: set = None) -> int:
        """Remove stored tool results for search tools that cause recursive bloat.
        
        Identifies tool results by matching tool_call_id to the assistant message's
        tool_calls metadata, then checking the function name.
        
        Args:
            tool_names: Tool names to clean up (default: conversation_search, archival_search)
        
        Returns:
            Number of messages deleted
        """
        if tool_names is None:
            tool_names = {"conversation_search", "archival_search"}
        
        table = self._get_table()
        arrow = table.to_arrow()
        
        roles = arrow.column("role").to_pylist()
        metadata_list = arrow.column("metadata").to_pylist()
        ids = arrow.column("id").to_pylist()
        
        # Build tool_call_id → tool_name lookup from assistant messages
        id_to_name = {}
        for role, meta_str in zip(roles, metadata_list):
            if role != "assistant":
                continue
            try:
                meta = json.loads(meta_str) if meta_str else {}
                for tc in meta.get("tool_calls", []):
                    tc_id = tc.get("id", "")
                    fn_name = tc.get("function", {}).get("name", "")
                    if tc_id and fn_name:
                        id_to_name[tc_id] = fn_name
            except (json.JSONDecodeError, TypeError):
                continue
        
        # Find tool messages whose tool_call_id maps to a search tool
        to_delete = []
        for msg_id, role, meta_str in zip(ids, roles, metadata_list):
            if role != "tool":
                continue
            try:
                meta = json.loads(meta_str) if meta_str else {}
                tc_id = meta.get("tool_call_id", "")
                tool_name = id_to_name.get(tc_id, "")
                if tool_name in tool_names:
                    to_delete.append(msg_id)
            except (json.JSONDecodeError, TypeError):
                continue
        
        if to_delete:
            for msg_id in to_delete:
                table.delete(f"id = '{msg_id}'")
            logger.info(f"Cleaned up {len(to_delete)} search result messages from history")
        
        return len(to_delete)
    
    def count(self) -> int:
        """Get total message count."""
        table = self._get_table()
        return table.count_rows() - 1  # Exclude init row
    
    def clear(self) -> int:
        """Clear all messages."""
        count = self.count()
        table = self._get_table()
        table.delete("id != '_init_'")
        logger.info(f"Cleared {count} messages")
        return count
    
    def get_context_window(
        self,
        max_messages: int = 50,
        max_chars: int = 50000,
    ) -> List[dict]:
        """Get messages for LLM context window."""
        messages = self.get_recent(limit=max_messages)
        
        total_chars = 0
        result = []
        
        for msg in reversed(messages):
            msg_chars = len(msg["content"])
            if total_chars + msg_chars > max_chars:
                break
            result.insert(0, msg)
            total_chars += msg_chars
        
        return result
