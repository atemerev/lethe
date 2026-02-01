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
    
    def _embed(self, text: str) -> List[float]:
        """Generate embedding for text."""
        if not text.strip():
            return [0.0] * EMBEDDING_DIM
        return self.embedder.compute_query_embeddings(text)[0]
    
    def add(
        self,
        role: str,
        content: str,
        metadata: Optional[dict] = None,
    ) -> str:
        """Add a message to history.
        
        Args:
            role: Message role (user, assistant, system, tool)
            content: Message content
            metadata: Optional metadata (tool_call_id, etc.)
            
        Returns:
            Message ID
        """
        message_id = f"msg-{uuid.uuid4()}"
        now = datetime.now(timezone.utc).isoformat()
        
        # Generate embedding
        vector = self._embed(content)
        
        table = self._get_table()
        table.add([{
            "id": message_id,
            "role": role,
            "content": content,
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
        """Get recent messages (oldest first for context)."""
        table = self._get_table()
        results = table.search().limit(limit + 10).to_list()
        
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
            })
        
        messages.sort(key=lambda m: m["created_at"])
        return messages[-limit:]
    
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
