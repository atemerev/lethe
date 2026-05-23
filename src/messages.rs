use std::cmp::Ordering;
use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::Arc;

use anyhow::{Context, Result as AnyhowResult, anyhow};
use arrow_array::RecordBatch;
use arrow_schema::{DataType, Field, Schema};
use chrono::{DateTime, Utc};
use futures::TryStreamExt;
use lancedb::index::Index;
use lancedb::index::scalar::FtsIndexBuilder;
use lancedb::query::{ExecutableQuery, QueryBase};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use thiserror::Error;
use uuid::Uuid;

use crate::semantic::{
    EmbeddingEngine, LEGACY_EMBEDDING_DIMENSIONS, VECTOR_COLUMN, distance_column, run_lancedb,
    string_column, utf8_array, vector_array,
};

const TABLE_NAME: &str = "message_history";
const INIT_ID: &str = "_init_";

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
    Lance(#[from] anyhow::Error),
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
    lancedb_dir: PathBuf,
    embedder: EmbeddingEngine,
}

#[derive(Debug)]
struct MessageRow {
    id: String,
    role: String,
    content: String,
    metadata: String,
    created_at: String,
    vector: Vec<f32>,
}

impl MessageHistory {
    pub fn open(lancedb_dir: impl Into<PathBuf>) -> MessageHistoryResult<Self> {
        let lancedb_dir = lancedb_dir.into();
        let history = Self {
            embedder: EmbeddingEngine::from_env(&lancedb_dir),
            lancedb_dir,
        };
        history.ensure_schema()?;
        Ok(history)
    }

    #[cfg(test)]
    fn open_with_hash_embedder(
        lancedb_dir: impl Into<PathBuf>,
        dimensions: usize,
    ) -> MessageHistoryResult<Self> {
        let lancedb_dir = lancedb_dir.into();
        let history = Self {
            embedder: EmbeddingEngine::with_hash_dimensions(dimensions),
            lancedb_dir,
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
        let row = MessageRow {
            id: id.clone(),
            role: role.to_string(),
            content: content.to_string(),
            metadata: serde_json::to_string(&metadata)?,
            created_at: now,
            vector,
        };
        let batch = message_batch(&[row])?;
        let db_path = db_uri(&self.lancedb_dir);
        run_lancedb(async move {
            let db = lancedb::connect(&db_path).execute().await?;
            let table = db.open_table(TABLE_NAME).execute().await?;
            table.add(batch).execute().await?;
            Ok(())
        })?;
        Ok(id)
    }

    pub fn get(&self, message_id: &str) -> MessageHistoryResult<Option<StoredMessage>> {
        Ok(self
            .all()?
            .into_iter()
            .find(|message| message.id == message_id && message.id != INIT_ID))
    }

    pub fn get_recent(&self, limit: usize) -> MessageHistoryResult<Vec<StoredMessage>> {
        let mut messages = self.all()?;
        messages.sort_by(|left, right| {
            parse_time(&right.created_at)
                .cmp(&parse_time(&left.created_at))
                .then_with(|| right.id.cmp(&left.id))
        });
        messages.truncate(if limit == 0 { 20 } else { limit });
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
        let mut messages = self.all()?;
        messages.retain(|message| message.role == role);
        messages.sort_by(|left, right| {
            parse_time(&left.created_at)
                .cmp(&parse_time(&right.created_at))
                .then_with(|| left.id.cmp(&right.id))
        });
        messages.truncate(if limit == 0 { 50 } else { limit });
        Ok(messages)
    }

    pub fn all_messages(&self) -> MessageHistoryResult<Vec<StoredMessage>> {
        self.all()
    }

    pub fn delete(&self, message_id: &str) -> MessageHistoryResult<bool> {
        if message_id == INIT_ID {
            return Ok(false);
        }
        let db_path = db_uri(&self.lancedb_dir);
        let predicate = id_predicate(message_id);
        let deleted = run_lancedb(async move {
            let db = lancedb::connect(&db_path).execute().await?;
            let table = db.open_table(TABLE_NAME).execute().await?;
            let result = table.delete(&predicate).await?;
            Ok(result.num_deleted_rows)
        })?;
        Ok(deleted > 0)
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
        let db_path = db_uri(&self.lancedb_dir);
        let count = run_lancedb(async move {
            let db = lancedb::connect(&db_path).execute().await?;
            let table = db.open_table(TABLE_NAME).execute().await?;
            table
                .count_rows(Some(format!("id != {}", sql_string_literal(INIT_ID))))
                .await
                .map_err(Into::into)
        })?;
        Ok(count)
    }

    pub fn clear(&self) -> MessageHistoryResult<usize> {
        let count = self.count()?;
        let db_path = db_uri(&self.lancedb_dir);
        run_lancedb(async move {
            let db = lancedb::connect(&db_path).execute().await?;
            let table = db.open_table(TABLE_NAME).execute().await?;
            table
                .delete(&format!("id != {}", sql_string_literal(INIT_ID)))
                .await?;
            Ok(())
        })?;
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
                "\n- [{}] {} {}{}\n  {}",
                message.created_at,
                message.role,
                message.id,
                score,
                preview(&message.content, 240).replace('\n', " ")
            ));
        }
        lines.join("\n")
    }

    fn ensure_schema(&self) -> MessageHistoryResult<()> {
        std::fs::create_dir_all(&self.lancedb_dir)?;
        let db_path = db_uri(&self.lancedb_dir);
        run_lancedb(async move {
            let db = lancedb::connect(&db_path).execute().await?;
            let tables = db.table_names().execute().await?;
            if !tables.iter().any(|name| name == TABLE_NAME) {
                let init = MessageRow {
                    id: INIT_ID.to_string(),
                    role: "system".to_string(),
                    content: String::new(),
                    metadata: "{}".to_string(),
                    created_at: Utc::now().to_rfc3339(),
                    vector: vec![0.0; LEGACY_EMBEDDING_DIMENSIONS],
                };
                db.create_table(TABLE_NAME, message_batch(&[init])?)
                    .execute()
                    .await?;
                let table = db.open_table(TABLE_NAME).execute().await?;
                if let Err(error) = table
                    .create_index(&["content"], Index::FTS(FtsIndexBuilder::default()))
                    .execute()
                    .await
                {
                    tracing::debug!("message FTS index creation skipped: {error}");
                }
            }
            Ok(())
        })?;
        Ok(())
    }

    fn all(&self) -> MessageHistoryResult<Vec<StoredMessage>> {
        let db_path = db_uri(&self.lancedb_dir);
        let batches = run_lancedb(async move {
            let db = lancedb::connect(&db_path).execute().await?;
            let table = db.open_table(TABLE_NAME).execute().await?;
            let rows = table.count_rows(None).await?;
            let stream = table.query().limit(rows.max(1)).execute().await?;
            stream.try_collect::<Vec<_>>().await.map_err(Into::into)
        })?;
        messages_from_batches(&batches)
    }

    fn vector_search(&self, query: &str, limit: usize) -> MessageHistoryResult<Vec<StoredMessage>> {
        let query_vector = self.embedder.embed_query(query)?;
        let db_path = db_uri(&self.lancedb_dir);
        let limit = limit.max(1);
        let batches = run_lancedb(async move {
            let db = lancedb::connect(&db_path).execute().await?;
            let table = db.open_table(TABLE_NAME).execute().await?;
            let stream = table
                .query()
                .nearest_to(query_vector)?
                .limit(limit)
                .execute()
                .await?;
            stream.try_collect::<Vec<_>>().await.map_err(Into::into)
        })?;
        messages_from_batches(&batches)
    }
}

fn message_batch(rows: &[MessageRow]) -> AnyhowResult<RecordBatch> {
    let dimension = rows
        .first()
        .map(|row| row.vector.len())
        .filter(|dimension| *dimension > 0)
        .ok_or_else(|| anyhow!("message batch requires at least one vector"))?;
    if rows.iter().any(|row| row.vector.len() != dimension) {
        return Err(anyhow!("message vectors have inconsistent dimensions"));
    }

    let schema = Arc::new(Schema::new(vec![
        Field::new("id", DataType::Utf8, false),
        Field::new("role", DataType::Utf8, false),
        Field::new("content", DataType::Utf8, false),
        Field::new(
            VECTOR_COLUMN,
            DataType::FixedSizeList(
                Arc::new(Field::new("item", DataType::Float32, true)),
                dimension as i32,
            ),
            false,
        ),
        Field::new("metadata", DataType::Utf8, false),
        Field::new("created_at", DataType::Utf8, false),
    ]));

    RecordBatch::try_new(
        schema,
        vec![
            utf8_array(rows.iter().map(|row| row.id.as_str())),
            utf8_array(rows.iter().map(|row| row.role.as_str())),
            utf8_array(rows.iter().map(|row| row.content.as_str())),
            vector_array(
                rows.iter().map(|row| row.vector.clone()).collect(),
                dimension,
            ),
            utf8_array(rows.iter().map(|row| row.metadata.as_str())),
            utf8_array(rows.iter().map(|row| row.created_at.as_str())),
        ],
    )
    .context("failed to build message LanceDB batch")
}

fn messages_from_batches(batches: &[RecordBatch]) -> MessageHistoryResult<Vec<StoredMessage>> {
    let mut messages = Vec::new();
    for batch in batches {
        let ids = string_column(batch, "id")?;
        let roles = string_column(batch, "role")?;
        let contents = string_column(batch, "content")?;
        let metadata = string_column(batch, "metadata")?;
        let created = string_column(batch, "created_at")?;
        let distances = distance_column(batch);

        for row in 0..batch.num_rows() {
            let id = ids.value(row);
            if id == INIT_ID {
                continue;
            }
            let raw_metadata = metadata.value(row);
            let parsed_metadata = serde_json::from_str(raw_metadata).unwrap_or_else(|_| json!({}));
            let metadata = if parsed_metadata.is_object() {
                parsed_metadata
            } else {
                json!({})
            };
            let score = distances
                .as_ref()
                .and_then(|values| values.get(row).copied())
                .map(semantic_score)
                .unwrap_or(0.0);
            messages.push(StoredMessage {
                id: id.to_string(),
                role: roles.value(row).to_string(),
                content: contents.value(row).to_string(),
                metadata,
                created_at: created.value(row).to_string(),
                score,
            });
        }
    }
    Ok(messages)
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

fn semantic_score(distance: f64) -> f64 {
    1.0 / (1.0 + distance.max(0.0))
}

fn query_terms(query: &str) -> Vec<String> {
    let mut terms = Vec::new();
    let mut current = String::new();
    for ch in query.to_ascii_lowercase().chars() {
        if ch.is_ascii_alphanumeric() || matches!(ch, '_' | '-') {
            current.push(ch);
        } else if !current.is_empty() {
            terms.push(std::mem::take(&mut current));
        }
    }
    if !current.is_empty() {
        terms.push(current);
    }
    terms
}

fn clean_names(names: &[String]) -> HashSet<String> {
    names
        .iter()
        .map(|name| name.trim().to_string())
        .filter(|name| !name.is_empty())
        .collect()
}

fn preview(text: &str, max_chars: usize) -> String {
    text.chars().take(max_chars).collect()
}

fn db_uri(path: &Path) -> String {
    path.display().to_string()
}

fn id_predicate(id: &str) -> String {
    format!("id = {}", sql_string_literal(id))
}

fn sql_string_literal(value: &str) -> String {
    format!("'{}'", value.replace('\'', "''"))
}

#[cfg(test)]
mod tests {
    use tempfile::tempdir;

    use super::*;

    fn history() -> MessageHistory {
        let tmp = tempdir().unwrap();
        let path = tmp.path().join("lancedb");
        let history =
            MessageHistory::open_with_hash_embedder(path, LEGACY_EMBEDDING_DIMENSIONS).unwrap();
        std::mem::forget(tmp);
        history
    }

    #[test]
    fn add_get_recent_count_and_clear_messages() {
        let history = history();
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
        let history = history();
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
    fn context_window_keeps_recent_messages_within_char_budget() {
        let history = history();
        history.add("user", "one", None).unwrap();
        history.add("assistant", "two two", None).unwrap();
        history.add("user", "three three three", None).unwrap();

        let window = history.get_context_window(3, 10).unwrap();
        assert!(window.is_empty());

        let window = history.get_context_window(3, 25).unwrap();
        assert_eq!(window.len(), 2);
        assert_eq!(window[0].content, "two two");
    }

    #[test]
    fn cleanup_search_results_deletes_only_matching_tool_outputs() {
        let history = history();
        history
            .add(
                "assistant",
                "",
                Some(json!({
                    "tool_calls": [
                        {"id": "call-search", "function": {"name": "conversation_search"}},
                        {"id": "call-other", "function": {"name": "bash"}}
                    ]
                })),
            )
            .unwrap();
        history
            .add(
                "assistant",
                "tool call",
                Some(json!({
                    "tool_calls": [
                        {"id": "call-search", "function": {"name": "conversation_search"}},
                        {"id": "call-other", "function": {"name": "bash"}}
                    ]
                })),
            )
            .unwrap();
        let search_result = history
            .add(
                "tool",
                "recursive search result",
                Some(json!({"tool_call_id": "call-search"})),
            )
            .unwrap();
        let other_tool = history
            .add(
                "tool",
                "shell result",
                Some(json!({"tool_call_id": "call-other"})),
            )
            .unwrap();

        assert_eq!(history.cleanup_search_results(None).unwrap(), 1);
        assert!(history.get(&search_result).unwrap().is_none());
        assert!(history.get(&other_tool).unwrap().is_some());
    }

    #[test]
    fn invalid_inputs_are_rejected() {
        let history = history();
        assert!(matches!(
            history.add("", "content", None).unwrap_err(),
            MessageHistoryError::EmptyRole
        ));
        assert!(matches!(
            history
                .add("user", "content", Some(json!("bad")))
                .unwrap_err(),
            MessageHistoryError::InvalidMetadata
        ));
    }
}
