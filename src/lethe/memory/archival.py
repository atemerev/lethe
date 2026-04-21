"""Archival memory - Long-term semantic storage with hybrid search.

Uses LanceDB for vector + FTS hybrid search.
"""

import json
from datetime import datetime, timezone
from typing import Optional, List
import uuid

import lancedb
from lancedb.embeddings import get_registry
import logging

logger = logging.getLogger(__name__)


# Embedding model - small and fast, runs locally
EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIM = 384


class ArchivalMemory:
    """Long-term memory with hybrid search (vector + FTS).
    
    Stores passages/memories that can be searched semantically
    or by keywords. Uses LanceDB's built-in hybrid search.
    """
    
    TABLE_NAME = "archival_memory"
    
    def __init__(self, db: lancedb.DBConnection, embedding_model: str = EMBEDDING_MODEL):
        """Initialize archival memory.
        
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
        logger.info(f"Archival memory initialized with {embedding_model}")
    
    def _ensure_table(self):
        """Create table if it doesn't exist."""
        if self.TABLE_NAME not in self.db.list_tables():
            # Create with initial data (LanceDB requires data for schema inference)
            init_vector = [0.0] * EMBEDDING_DIM
            self.db.create_table(
                self.TABLE_NAME,
                data=[{
                    "id": "_init_",
                    "text": "",
                    "vector": init_vector,
                    "metadata": "{}",
                    "tags": "[]",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }],
                exist_ok=True,
            )
            logger.info(f"Created table {self.TABLE_NAME}")
            
            # Create FTS index
            table = self._get_table()
            table.create_fts_index("text", replace=True)
            logger.info("Created FTS index on text column")
    
    def _get_table(self):
        """Get the archival table."""
        return self.db.open_table(self.TABLE_NAME)
    
    def _embed(self, text: str) -> List[float]:
        """Generate embedding for text.
        
        Args:
            text: Text to embed
            
        Returns:
            Embedding vector
        """
        return self.embedder.compute_query_embeddings(text)[0]

    def _parse_json_field(self, value: str, fallback):
        """Parse a JSON field safely."""
        if not value:
            return fallback
        try:
            return json.loads(value)
        except Exception:
            return fallback

    def _vector_relevance(self, row: dict) -> float:
        """Convert vector distance to a higher-is-better relevance score in [0, 1]."""
        distance = row.get("_distance")
        if isinstance(distance, (int, float)):
            return 1.0 / (1.0 + max(0.0, float(distance)))
        return 0.0

    def _to_memory(self, row: dict, score: float) -> dict:
        """Convert a raw table row to a memory record."""
        return {
            "id": row["id"],
            "text": row["text"],
            "metadata": self._parse_json_field(row.get("metadata"), {}),
            "tags": self._parse_json_field(row.get("tags"), []),
            "created_at": row["created_at"],
            "score": score,
        }
    
    def add(
        self,
        text: str,
        metadata: Optional[dict] = None,
        tags: Optional[List[str]] = None,
    ) -> str:
        """Add a memory to archival storage.
        
        Args:
            text: Memory text
            metadata: Optional metadata dict
            tags: Optional list of tags
            
        Returns:
            Memory ID
        """
        memory_id = f"mem-{uuid.uuid4()}"
        now = datetime.now(timezone.utc).isoformat()
        
        # Generate embedding
        vector = self._embed(text)
        
        table = self._get_table()
        table.add([{
            "id": memory_id,
            "text": text,
            "vector": vector,
            "metadata": json.dumps(metadata or {}),
            "tags": json.dumps(tags or []),
            "created_at": now,
        }])
        
        logger.debug(f"Added memory {memory_id}: {text[:50]}...")
        return memory_id
    
    def search(
        self,
        query: str,
        limit: int = 10,
        search_type: str = "hybrid",
        tags: Optional[List[str]] = None,
    ) -> List[dict]:
        """Search archival memory.
        
        Args:
            query: Search query
            limit: Max results
            search_type: "hybrid", "vector", or "fts"
            tags: Optional tag filter
            
        Returns:
            List of matching memories with scores
        """
        table = self._get_table()
        limit = max(1, limit)
        search_type = (search_type or "hybrid").lower()
        
        # Generate embedding for vector search
        query_vector = self._embed(query)
        
        if search_type == "vector":
            # Pure vector search
            results = (
                table.search(query_vector)
                .limit(limit * 3)
                .to_list()
            )
            rows = [r for r in results if r.get("id") != "_init_"]
            memories = []
            for row in rows:
                memory = self._to_memory(row, self._vector_relevance(row))
                if tags and not any(t in memory["tags"] for t in tags):
                    continue
                memories.append(memory)
                if len(memories) >= limit:
                    break
            return memories
        elif search_type == "fts":
            # Pure FTS
            results = (
                table.search(query, query_type="fts")
                .limit(limit * 3)
                .to_list()
            )
            rows = [r for r in results if r.get("id") != "_init_"]
            memories = []
            for rank, row in enumerate(rows, start=1):
                # Rank-based relevance for consistent semantics (higher is better).
                memory = self._to_memory(row, 1.0 / rank)
                if tags and not any(t in memory["tags"] for t in tags):
                    continue
                memories.append(memory)
                if len(memories) >= limit:
                    break
            return memories

        # Hybrid search (default) with reciprocal-rank fusion.
        fetch_limit = max(limit * 3, 20)
        vector_results = table.search(query_vector).limit(fetch_limit).to_list()
        try:
            fts_results = table.search(query, query_type="fts").limit(fetch_limit).to_list()
        except Exception:
            fts_results = []

        vector_rows = [r for r in vector_results if r.get("id") != "_init_"]
        fts_rows = [r for r in fts_results if r.get("id") != "_init_"]

        fused: dict[str, dict] = {}
        for rank, row in enumerate(vector_rows, start=1):
            memory_id = row["id"]
            if memory_id not in fused:
                fused[memory_id] = {"row": row, "score": 0.0}
            fused[memory_id]["score"] += 1.0 / rank

        for rank, row in enumerate(fts_rows, start=1):
            memory_id = row["id"]
            if memory_id not in fused:
                fused[memory_id] = {"row": row, "score": 0.0}
            fused[memory_id]["score"] += 1.0 / rank

        ranked = sorted(fused.values(), key=lambda item: item["score"], reverse=True)
        if not ranked:
            return []

        max_score = ranked[0]["score"] or 1.0
        memories = []
        for item in ranked:
            normalized = item["score"] / max_score
            memory = self._to_memory(item["row"], normalized)
            if tags and not any(t in memory["tags"] for t in tags):
                continue
            memories.append(memory)
            if len(memories) >= limit:
                break

        return memories
    
    def get(self, memory_id: str) -> Optional[dict]:
        """Get a memory by ID.
        
        Args:
            memory_id: Memory ID
            
        Returns:
            Memory dict or None
        """
        table = self._get_table()
        results = table.search().where(f"id = '{memory_id}'").limit(1).to_list()
        
        if not results:
            return None
        
        r = results[0]
        return {
            "id": r["id"],
            "text": r["text"],
            "metadata": json.loads(r["metadata"]) if r["metadata"] else {},
            "tags": json.loads(r["tags"]) if r["tags"] else [],
            "created_at": r["created_at"],
        }
    
    def delete(self, memory_id: str) -> bool:
        """Delete a memory.
        
        Args:
            memory_id: Memory ID
            
        Returns:
            True if deleted
        """
        table = self._get_table()
        table.delete(f"id = '{memory_id}'")
        logger.debug(f"Deleted memory {memory_id}")
        return True
    
    def update_tags(self, memory_id: str, tags: List[str]) -> bool:
        """Update memory tags.
        
        Args:
            memory_id: Memory ID
            tags: New tags list
            
        Returns:
            True if updated
        """
        memory = self.get(memory_id)
        if not memory:
            return False
        
        table = self._get_table()
        table.delete(f"id = '{memory_id}'")
        
        # Re-add with updated tags (LanceDB doesn't have in-place update)
        vector = self._embed(memory["text"])
        table.add([{
            "id": memory_id,
            "text": memory["text"],
            "vector": vector,
            "metadata": json.dumps(memory["metadata"]),
            "tags": json.dumps(tags),
            "created_at": memory["created_at"],
        }])
        
        return True
    
    def count(self) -> int:
        """Get total memory count.
        
        Returns:
            Number of memories
        """
        table = self._get_table()
        total = table.count_rows()
        if total <= 0:
            return 0

        try:
            has_init = bool(table.search().where("id = '_init_'").limit(1).to_list())
        except Exception:
            has_init = total > 0

        return max(0, total - (1 if has_init else 0))
    
    def list_recent(self, limit: int = 50) -> List[dict]:
        """List recent memories.
        
        Args:
            limit: Max results
            
        Returns:
            List of recent memories
        """
        table = self._get_table()
        arrow_table = table.to_arrow()
        sorted_table = arrow_table.sort_by([("created_at", "descending")])

        memories = []
        for i in range(sorted_table.num_rows):
            memory_id = sorted_table["id"][i].as_py()
            if memory_id == "_init_":
                continue

            metadata = self._parse_json_field(sorted_table["metadata"][i].as_py(), {})
            tags_data = self._parse_json_field(sorted_table["tags"][i].as_py(), [])
            memories.append({
                "id": memory_id,
                "text": sorted_table["text"][i].as_py(),
                "metadata": metadata,
                "tags": tags_data,
                "created_at": sorted_table["created_at"][i].as_py(),
            })

            if len(memories) >= limit:
                break

        return memories
