use std::cmp::Ordering;
use std::collections::{HashMap, HashSet};
use std::path::PathBuf;

use chrono::{DateTime, Utc};
use rusqlite::{Connection, OptionalExtension, params};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use thiserror::Error;
use uuid::Uuid;

use super::codec::{ensure_parent, f32_slice_as_bytes, open_conn, parent_dir, semantic_score};
use super::search::{indent_block, query_terms, search_result_text};
use super::semantic::{EmbeddingEngine, LEGACY_EMBEDDING_DIMENSIONS};

const TABLE_NAME: &str = "message_history";
const VEC_TABLE_NAME: &str = "message_history_vec";
const SEARCH_RESULT_MAX_LINES: usize = 50;

#[derive(Debug, Error)]
pub enum MessageHistoryError {
    #[error("message role is required")]
    EmptyRole,
    #[error("message metadata must be a JSON object")]
    InvalidMetadata,
    #[error(transparent)]
    Io(#[from] std::io::Error),
    #[error(transparent)]
    Json(#[from] serde_json::Error),
    #[error(transparent)]
    Sqlite(#[from] rusqlite::Error),
    #[error(transparent)]
    Embedding(#[from] anyhow::Error),
}

pub type MessageHistoryResult<T> = Result<T, MessageHistoryError>;

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct StoredMessage {
    pub id: String,
    pub role: String,
    pub content: String,
    pub metadata: Value,
    pub created_at: String,
    pub score: f64,
}

#[derive(Clone, Debug)]
pub struct MessageHistory {
    data_path: PathBuf,
    embedder: EmbeddingEngine,
}

impl MessageHistory {
    pub fn open(data_path: impl Into<PathBuf>) -> MessageHistoryResult<Self> {
        let data_path = data_path.into();
        let history = Self {
            embedder: EmbeddingEngine::from_env(parent_dir(&data_path)),
            data_path,
        };
        history.ensure_schema()?;
        Ok(history)
    }

    #[cfg(test)]
    fn open_with_hash_embedder(
        data_path: impl Into<PathBuf>,
        dimensions: usize,
    ) -> MessageHistoryResult<Self> {
        let data_path = data_path.into();
        let history = Self {
            embedder: EmbeddingEngine::with_hash_dimensions(dimensions),
            data_path,
        };
        history.ensure_schema()?;
        Ok(history)
    }

    pub fn add(
        &self,
        role: &str,
        content: &str,
        metadata: Option<Value>,
    ) -> MessageHistoryResult<String> {
        let role = role.trim();
        if role.is_empty() {
            return Err(MessageHistoryError::EmptyRole);
        }
        let metadata = metadata.unwrap_or_else(|| json!({}));
        if !metadata.is_object() {
            return Err(MessageHistoryError::InvalidMetadata);
        }

        let id = format!("msg-{}", Uuid::new_v4());
        let now = Utc::now().to_rfc3339();
        let vector = self.embedder.embed_document(content)?;
        let metadata_str = serde_json::to_string(&metadata)?;

        let mut conn = self.open_conn()?;
        let tx = conn.transaction()?;
        tx.execute(
            "INSERT INTO message_history (id, role, content, metadata, created_at) \
             VALUES (?, ?, ?, ?, ?)",
            params![id, role, content, metadata_str, now],
        )?;
        tx.execute(
            "INSERT INTO message_history_vec (id, embedding) VALUES (?, ?)",
            params![id, f32_slice_as_bytes(&vector)],
        )?;
        tx.commit()?;
        Ok(id)
    }

    pub fn get(&self, message_id: &str) -> MessageHistoryResult<Option<StoredMessage>> {
        let conn = self.open_conn()?;
        let message = conn
            .query_row(
                "SELECT id, role, content, metadata, created_at FROM message_history WHERE id = ?",
                params![message_id],
                row_to_message,
            )
            .optional()?;
        Ok(message)
    }

    pub fn get_recent(&self, limit: usize) -> MessageHistoryResult<Vec<StoredMessage>> {
        let conn = self.open_conn()?;
        let limit = if limit == 0 { 20 } else { limit };
        let mut stmt = conn.prepare(
            "SELECT id, role, content, metadata, created_at FROM message_history \
             ORDER BY created_at DESC, id DESC LIMIT ?",
        )?;
        let rows = stmt.query_map(params![limit as i64], row_to_message)?;
        let mut messages = Vec::new();
        for row in rows {
            messages.push(row?);
        }
        messages.reverse();
        Ok(messages)
    }

    pub fn search(
        &self,
        query: &str,
        limit: usize,
        role: Option<&str>,
    ) -> MessageHistoryResult<Vec<StoredMessage>> {
        let query = query.trim();
        let limit = if limit == 0 { 20 } else { limit };
        let terms = query_terms(query);
        let role = role.map(str::trim).filter(|role| !role.is_empty());
        let mut merged = HashMap::new();

        for mut message in self.all()? {
            if role.is_some_and(|role| message.role != role) {
                continue;
            }
            message.score = score_message(query, &terms, &message);
            if terms.is_empty() || message.score > 0.0 {
                merged.insert(message.id.clone(), message);
            }
        }

        if !query.is_empty() {
            match self.vector_search(query, limit * 4) {
                Ok(messages) => {
                    for message in messages {
                        if role.is_some_and(|role| message.role != role) {
                            continue;
                        }
                        merged
                            .entry(message.id.clone())
                            .and_modify(|existing: &mut StoredMessage| {
                                existing.score += message.score
                            })
                            .or_insert(message);
                    }
                }
                Err(error) => {
                    tracing::warn!("message vector search failed; using lexical results: {error}");
                }
            }
        }

        let mut messages = merged.into_values().collect::<Vec<_>>();
        messages.sort_by(compare_messages);
        messages.truncate(limit);
        Ok(messages)
    }

    pub fn search_by_role(
        &self,
        query: &str,
        role: &str,
        limit: usize,
    ) -> MessageHistoryResult<Vec<StoredMessage>> {
        self.search(query, limit, Some(role))
    }

    pub fn get_by_role(
        &self,
        role: &str,
        limit: usize,
    ) -> MessageHistoryResult<Vec<StoredMessage>> {
        let conn = self.open_conn()?;
        let limit = if limit == 0 { 50 } else { limit };
        let mut stmt = conn.prepare(
            "SELECT id, role, content, metadata, created_at FROM message_history \
             WHERE role = ? ORDER BY created_at, id LIMIT ?",
        )?;
        let rows = stmt.query_map(params![role, limit as i64], row_to_message)?;
        let mut messages = Vec::new();
        for row in rows {
            messages.push(row?);
        }
        Ok(messages)
    }

    pub fn all_messages(&self) -> MessageHistoryResult<Vec<StoredMessage>> {
        self.all()
    }

    pub fn delete(&self, message_id: &str) -> MessageHistoryResult<bool> {
        let mut conn = self.open_conn()?;
        let tx = conn.transaction()?;
        let removed = tx.execute(
            "DELETE FROM message_history WHERE id = ?",
            params![message_id],
        )?;
        tx.execute(
            "DELETE FROM message_history_vec WHERE id = ?",
            params![message_id],
        )?;
        tx.commit()?;
        Ok(removed > 0)
    }

    pub fn cleanup_search_results(
        &self,
        tool_names: Option<&[String]>,
    ) -> MessageHistoryResult<usize> {
        let names: HashSet<String> = tool_names
            .map(clean_names)
            .filter(|names| !names.is_empty())
            .unwrap_or_else(|| {
                ["conversation_search", "archival_search"]
                    .into_iter()
                    .map(str::to_string)
                    .collect()
            });

        let messages = self.all()?;
        let mut tool_call_names = HashMap::new();
        for message in &messages {
            if message.role != "assistant" {
                continue;
            }
            let Some(calls) = message.metadata.get("tool_calls").and_then(Value::as_array) else {
                continue;
            };
            for call in calls {
                let Some(call_id) = call.get("id").and_then(Value::as_str) else {
                    continue;
                };
                let Some(name) = call
                    .get("function")
                    .and_then(|function| function.get("name"))
                    .and_then(Value::as_str)
                else {
                    continue;
                };
                tool_call_names.insert(call_id.to_string(), name.to_string());
            }
        }

        let mut deleted = 0;
        for message in messages {
            if message.role != "tool" {
                continue;
            }
            let Some(call_id) = message.metadata.get("tool_call_id").and_then(Value::as_str) else {
                continue;
            };
            let Some(tool_name) = tool_call_names.get(call_id) else {
                continue;
            };
            if names.contains(tool_name) && self.delete(&message.id)? {
                deleted += 1;
            }
        }
        Ok(deleted)
    }

    pub fn count(&self) -> MessageHistoryResult<usize> {
        let conn = self.open_conn()?;
        let count: i64 =
            conn.query_row("SELECT COUNT(*) FROM message_history", [], |row| row.get(0))?;
        Ok(count as usize)
    }

    pub fn clear(&self) -> MessageHistoryResult<usize> {
        let count = self.count()?;
        let mut conn = self.open_conn()?;
        let tx = conn.transaction()?;
        tx.execute("DELETE FROM message_history", [])?;
        tx.execute("DELETE FROM message_history_vec", [])?;
        tx.commit()?;
        Ok(count)
    }

    pub fn get_context_window(
        &self,
        max_messages: usize,
        max_chars: usize,
    ) -> MessageHistoryResult<Vec<StoredMessage>> {
        let messages = self.get_recent(max_messages)?;
        let mut total_chars = 0;
        let mut result = Vec::new();
        for message in messages.into_iter().rev() {
            let message_chars = message.content.chars().count();
            if total_chars + message_chars > max_chars {
                break;
            }
            total_chars += message_chars;
            result.insert(0, message);
        }
        Ok(result)
    }

    pub fn format_messages(messages: &[StoredMessage]) -> String {
        if messages.is_empty() {
            return "No messages found.".to_string();
        }
        let mut lines = vec![format!("Found {} message(s):", messages.len())];
        for message in messages {
            let score = if message.score > 0.0 {
                format!(" score={:.2}", message.score)
            } else {
                String::new()
            };
            lines.push(format!(
                "\n- [{}] {} {}{}\n{}",
                message.created_at,
                message.role,
                message.id,
                score,
                indent_block(
                    &search_result_text(&message.content, SEARCH_RESULT_MAX_LINES),
                    "  "
                )
            ));
        }
        lines.join("\n")
    }

    fn open_conn(&self) -> MessageHistoryResult<Connection> {
        Ok(open_conn(&self.data_path)?)
    }

    fn ensure_schema(&self) -> MessageHistoryResult<()> {
        ensure_parent(&self.data_path)?;
        let conn = self.open_conn()?;
        conn.execute_batch(&format!(
            "CREATE TABLE IF NOT EXISTS {table} (
                id          TEXT PRIMARY KEY,
                role        TEXT NOT NULL,
                content     TEXT NOT NULL,
                metadata    TEXT NOT NULL DEFAULT '{{}}',
                created_at  TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS {table}_created_at_idx ON {table} (created_at);
            CREATE INDEX IF NOT EXISTS {table}_role_idx ON {table} (role);
            CREATE VIRTUAL TABLE IF NOT EXISTS {vec_table} USING vec0(
                id TEXT PRIMARY KEY,
                embedding float[{dim}]
            );",
            table = TABLE_NAME,
            vec_table = VEC_TABLE_NAME,
            dim = LEGACY_EMBEDDING_DIMENSIONS,
        ))?;
        Ok(())
    }

    fn all(&self) -> MessageHistoryResult<Vec<StoredMessage>> {
        let conn = self.open_conn()?;
        let mut stmt = conn.prepare(
            "SELECT id, role, content, metadata, created_at FROM message_history ORDER BY id",
        )?;
        let rows = stmt.query_map([], row_to_message)?;
        let mut messages = Vec::new();
        for row in rows {
            messages.push(row?);
        }
        Ok(messages)
    }

    fn vector_search(&self, query: &str, limit: usize) -> MessageHistoryResult<Vec<StoredMessage>> {
        let query_vector = self.embedder.embed_query(query)?;
        let limit = limit.max(1);
        let conn = self.open_conn()?;
        let mut stmt = conn.prepare(
            "SELECT m.id, m.role, m.content, m.metadata, m.created_at, v.distance \
             FROM message_history_vec v \
             JOIN message_history m ON m.id = v.id \
             WHERE v.embedding MATCH ? AND k = ? \
             ORDER BY v.distance",
        )?;
        let rows = stmt.query_map(
            params![f32_slice_as_bytes(&query_vector), limit as i64],
            |row| {
                let mut message = row_to_message(row)?;
                let distance: f64 = row.get(5)?;
                message.score = semantic_score(distance);
                Ok(message)
            },
        )?;
        let mut messages = Vec::new();
        for row in rows {
            messages.push(row?);
        }
        Ok(messages)
    }
}

fn row_to_message(row: &rusqlite::Row<'_>) -> rusqlite::Result<StoredMessage> {
    let id: String = row.get(0)?;
    let role: String = row.get(1)?;
    let content: String = row.get(2)?;
    let metadata_raw: String = row.get(3)?;
    let created_at: String = row.get(4)?;
    let metadata = serde_json::from_str(&metadata_raw).unwrap_or_else(|_| json!({}));
    let metadata = if metadata.is_object() {
        metadata
    } else {
        json!({})
    };
    Ok(StoredMessage {
        id,
        role,
        content,
        metadata,
        created_at,
        score: 0.0,
    })
}

fn compare_messages(left: &StoredMessage, right: &StoredMessage) -> Ordering {
    right
        .score
        .partial_cmp(&left.score)
        .unwrap_or(Ordering::Equal)
        .then_with(|| parse_time(&right.created_at).cmp(&parse_time(&left.created_at)))
        .then_with(|| left.id.cmp(&right.id))
}

fn parse_time(value: &str) -> Option<DateTime<Utc>> {
    DateTime::parse_from_rfc3339(value)
        .ok()
        .map(|time| time.with_timezone(&Utc))
}

fn score_message(query: &str, terms: &[String], message: &StoredMessage) -> f64 {
    if terms.is_empty() {
        return 1.0;
    }
    let query_lower = query.to_ascii_lowercase();
    let content_lower = message.content.to_ascii_lowercase();
    let metadata_lower = message.metadata.to_string().to_ascii_lowercase();
    let mut score = 0.0;

    if !query_lower.is_empty() && content_lower.contains(&query_lower) {
        score += 5.0;
    }
    for term in terms {
        score += content_lower.matches(term).count() as f64;
        if metadata_lower.contains(term) {
            score += 1.0;
        }
    }
    score
}

fn clean_names(names: &[String]) -> HashSet<String> {
    names
        .iter()
        .map(|name| name.trim().to_string())
        .filter(|name| !name.is_empty())
        .collect()
}

#[cfg(test)]
mod tests {
    use tempfile::tempdir;

    use super::*;

    fn history() -> (tempfile::TempDir, MessageHistory) {
        let tmp = tempdir().unwrap();
        let path = tmp.path().join("messages.db");
        let history =
            MessageHistory::open_with_hash_embedder(path, LEGACY_EMBEDDING_DIMENSIONS).unwrap();
        (tmp, history)
    }

    #[test]
    fn add_get_recent_count_and_clear_messages() {
        let (_tmp, history) = history();
        let first = history.add("user", "hello", None).unwrap();
        let second = history.add("assistant", "hi there", None).unwrap();

        assert_eq!(history.count().unwrap(), 2);
        assert_eq!(history.get(&first).unwrap().unwrap().content, "hello");

        let recent = history.get_recent(2).unwrap();
        assert_eq!(
            recent.iter().map(|message| &message.id).collect::<Vec<_>>(),
            vec![&first, &second]
        );
        assert!(MessageHistory::format_messages(&recent).contains("hi there"));

        assert_eq!(history.clear().unwrap(), 2);
        assert_eq!(history.count().unwrap(), 0);
    }

    #[test]
    fn search_and_role_filters_rank_messages() {
        let (_tmp, history) = history();
        history.add("user", "Graph API email access", None).unwrap();
        history.add("assistant", "Use cargo fmt", None).unwrap();
        history
            .add("user", "Graph tokens are in a file", None)
            .unwrap();

        let results = history.search("graph email", 10, None).unwrap();
        assert_eq!(results[0].content, "Graph API email access");

        let assistant = history.search_by_role("cargo", "assistant", 10).unwrap();
        assert_eq!(assistant.len(), 1);
        assert_eq!(assistant[0].role, "assistant");

        let users = history.get_by_role("user", 10).unwrap();
        assert_eq!(users.len(), 2);
    }

    #[test]
    fn format_messages_preserves_search_result_lines() {
        let content = (0..60)
            .map(|index| format!("line {index}"))
            .collect::<Vec<_>>()
            .join("\n");
        let formatted = MessageHistory::format_messages(&[StoredMessage {
            id: "msg-test".to_string(),
            role: "assistant".to_string(),
            content,
            metadata: json!({}),
            created_at: "2026-01-01T00:00:00Z".to_string(),
            score: 0.0,
        }]);

        assert!(formatted.contains("  line 0\n  line 1"));
        assert!(formatted.contains("  line 49"));
        assert!(!formatted.contains("line 50"));
        assert!(formatted.contains("[... 10 more lines]"));
    }

    #[test]
    fn context_window_keeps_recent_messages_within_char_budget() {
        let (_tmp, history) = history();
        history.add("user", "one", None).unwrap();
        history.add("assistant", "two two", None).unwrap();
        history.add("user", "three three three", None).unwrap();

        let window = history.get_context_window(3, 10).unwrap();
        assert!(window.is_empty() || window.last().unwrap().content.len() <= 10);
    }
}
