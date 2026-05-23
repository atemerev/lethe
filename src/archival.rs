use std::cmp::Ordering;
use std::collections::HashMap;
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

const TABLE_NAME: &str = "archival_memory";
const INIT_ID: &str = "_init_";

#[derive(Debug, Error)]
pub enum ArchivalError {
    #[error("archival memory text is required")]
    EmptyText,
    #[error("archival metadata must be a JSON object")]
    InvalidMetadata,
    #[error(transparent)]
    Io(#[from] std::io::Error),
    #[error(transparent)]
    Json(#[from] serde_json::Error),
    #[error(transparent)]
    Lance(#[from] anyhow::Error),
}

pub type ArchivalResult<T> = Result<T, ArchivalError>;

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct ArchivalEntry {
    pub id: String,
    pub text: String,
    pub metadata: Value,
    pub tags: Vec<String>,
    pub created_at: String,
    pub score: f64,
}

#[derive(Clone, Debug)]
pub struct ArchivalMemory {
    lancedb_dir: PathBuf,
    embedder: EmbeddingEngine,
}

#[derive(Debug)]
struct ArchivalRow {
    id: String,
    text: String,
    metadata: String,
    tags: String,
    created_at: String,
    vector: Vec<f32>,
}

impl ArchivalMemory {
    pub fn open(lancedb_dir: impl Into<PathBuf>) -> ArchivalResult<Self> {
        let lancedb_dir = lancedb_dir.into();
        let memory = Self {
            embedder: EmbeddingEngine::from_env(&lancedb_dir),
            lancedb_dir,
        };
        memory.ensure_schema()?;
        Ok(memory)
    }

    #[cfg(test)]
    fn open_with_hash_embedder(
        lancedb_dir: impl Into<PathBuf>,
        dimensions: usize,
    ) -> ArchivalResult<Self> {
        let lancedb_dir = lancedb_dir.into();
        let memory = Self {
            embedder: EmbeddingEngine::with_hash_dimensions(dimensions),
            lancedb_dir,
        };
        memory.ensure_schema()?;
        Ok(memory)
    }

    pub fn add(
        &self,
        text: &str,
        metadata: Option<Value>,
        tags: &[String],
    ) -> ArchivalResult<String> {
        let text = text.trim();
        if text.is_empty() {
            return Err(ArchivalError::EmptyText);
        }
        let metadata = metadata.unwrap_or_else(|| json!({}));
        if !metadata.is_object() {
            return Err(ArchivalError::InvalidMetadata);
        }

        let id = format!("mem-{}", Uuid::new_v4());
        let now = Utc::now().to_rfc3339();
        let vector = self.embedder.embed_document(text)?;
        let row = ArchivalRow {
            id: id.clone(),
            text: text.to_string(),
            metadata: serde_json::to_string(&metadata)?,
            tags: serde_json::to_string(&clean_tags(tags))?,
            created_at: now,
            vector,
        };
        let batch = archival_batch(&[row])?;
        let db_path = db_uri(&self.lancedb_dir);
        run_lancedb(async move {
            let db = lancedb::connect(&db_path).execute().await?;
            let table = db.open_table(TABLE_NAME).execute().await?;
            table.add(batch).execute().await?;
            Ok(())
        })?;
        Ok(id)
    }

    pub fn search(
        &self,
        query: &str,
        limit: usize,
        tags: Option<&[String]>,
    ) -> ArchivalResult<Vec<ArchivalEntry>> {
        let query = query.trim();
        let limit = if limit == 0 { 10 } else { limit };
        let terms = query_terms(query);
        let tag_filter = clean_tags(tags.unwrap_or_default());
        let mut merged = HashMap::new();

        for mut entry in self.all()? {
            if !tags_match_any(&entry.tags, &tag_filter) {
                continue;
            }
            entry.score = score_entry(query, &terms, &entry);
            if terms.is_empty() || entry.score > 0.0 {
                merged.insert(entry.id.clone(), entry);
            }
        }

        if !query.is_empty() {
            match self.vector_search(query, limit * 3) {
                Ok(entries) => {
                    for entry in entries {
                        if !tags_match_any(&entry.tags, &tag_filter) {
                            continue;
                        }
                        merged
                            .entry(entry.id.clone())
                            .and_modify(|existing: &mut ArchivalEntry| {
                                existing.score += entry.score
                            })
                            .or_insert(entry);
                    }
                }
                Err(error) => {
                    tracing::warn!("archival vector search failed; using lexical results: {error}");
                }
            }
        }

        let mut entries = merged.into_values().collect::<Vec<_>>();
        entries.sort_by(compare_entries);
        entries.truncate(limit);
        Ok(entries)
    }

    pub fn get(&self, memory_id: &str) -> ArchivalResult<Option<ArchivalEntry>> {
        Ok(self
            .all()?
            .into_iter()
            .find(|entry| entry.id == memory_id && entry.id != INIT_ID))
    }

    pub fn delete(&self, memory_id: &str) -> ArchivalResult<bool> {
        if memory_id == INIT_ID {
            return Ok(false);
        }
        let db_path = db_uri(&self.lancedb_dir);
        let predicate = id_predicate(memory_id);
        let result = run_lancedb(async move {
            let db = lancedb::connect(&db_path).execute().await?;
            let table = db.open_table(TABLE_NAME).execute().await?;
            let result = table.delete(&predicate).await?;
            Ok(result.num_deleted_rows)
        })?;
        Ok(result > 0)
    }

    pub fn update_tags(&self, memory_id: &str, tags: &[String]) -> ArchivalResult<bool> {
        if memory_id == INIT_ID {
            return Ok(false);
        }
        let tags = serde_json::to_string(&clean_tags(tags))?;
        let db_path = db_uri(&self.lancedb_dir);
        let predicate = id_predicate(memory_id);
        let tags_expr = sql_string_literal(&tags);
        let updated = run_lancedb(async move {
            let db = lancedb::connect(&db_path).execute().await?;
            let table = db.open_table(TABLE_NAME).execute().await?;
            let result = table
                .update()
                .only_if(predicate)
                .column("tags", tags_expr)
                .execute()
                .await?;
            Ok(result.rows_updated)
        })?;
        Ok(updated > 0)
    }

    pub fn count(&self) -> ArchivalResult<usize> {
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

    pub fn list_recent(&self, limit: usize) -> ArchivalResult<Vec<ArchivalEntry>> {
        let mut entries = self.all()?;
        entries.sort_by(|left, right| {
            parse_time(&right.created_at)
                .cmp(&parse_time(&left.created_at))
                .then_with(|| left.id.cmp(&right.id))
        });
        entries.truncate(if limit == 0 { 50 } else { limit });
        Ok(entries)
    }

    pub fn all_entries(&self) -> ArchivalResult<Vec<ArchivalEntry>> {
        self.all()
    }

    pub fn format_entries(entries: &[ArchivalEntry]) -> String {
        if entries.is_empty() {
            return "No archival memories found.".to_string();
        }
        let mut lines = vec![format!("Found {} archival memory(s):", entries.len())];
        for entry in entries {
            let tags = if entry.tags.is_empty() {
                "none".to_string()
            } else {
                entry.tags.join(", ")
            };
            let score = if entry.score > 0.0 {
                format!(" score={:.2}", entry.score)
            } else {
                String::new()
            };
            lines.push(format!(
                "\n- [{}] {} ({tags}{score})\n  {}",
                entry.created_at,
                entry.id,
                preview(&entry.text, 300).replace('\n', " ")
            ));
        }
        lines.join("\n")
    }

    fn ensure_schema(&self) -> ArchivalResult<()> {
        std::fs::create_dir_all(&self.lancedb_dir)?;
        let db_path = db_uri(&self.lancedb_dir);
        run_lancedb(async move {
            let db = lancedb::connect(&db_path).execute().await?;
            let tables = db.table_names().execute().await?;
            if !tables.iter().any(|name| name == TABLE_NAME) {
                let init = ArchivalRow {
                    id: INIT_ID.to_string(),
                    text: String::new(),
                    metadata: "{}".to_string(),
                    tags: "[]".to_string(),
                    created_at: Utc::now().to_rfc3339(),
                    vector: vec![0.0; LEGACY_EMBEDDING_DIMENSIONS],
                };
                db.create_table(TABLE_NAME, archival_batch(&[init])?)
                    .execute()
                    .await?;
                let table = db.open_table(TABLE_NAME).execute().await?;
                if let Err(error) = table
                    .create_index(&["text"], Index::FTS(FtsIndexBuilder::default()))
                    .execute()
                    .await
                {
                    tracing::debug!("archival FTS index creation skipped: {error}");
                }
            }
            Ok(())
        })?;
        Ok(())
    }

    fn all(&self) -> ArchivalResult<Vec<ArchivalEntry>> {
        let db_path = db_uri(&self.lancedb_dir);
        let batches = run_lancedb(async move {
            let db = lancedb::connect(&db_path).execute().await?;
            let table = db.open_table(TABLE_NAME).execute().await?;
            let rows = table.count_rows(None).await?;
            let stream = table.query().limit(rows.max(1)).execute().await?;
            stream.try_collect::<Vec<_>>().await.map_err(Into::into)
        })?;
        entries_from_batches(&batches)
    }

    fn vector_search(&self, query: &str, limit: usize) -> ArchivalResult<Vec<ArchivalEntry>> {
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
        entries_from_batches(&batches)
    }
}

fn archival_batch(rows: &[ArchivalRow]) -> AnyhowResult<RecordBatch> {
    let dimension = rows
        .first()
        .map(|row| row.vector.len())
        .filter(|dimension| *dimension > 0)
        .ok_or_else(|| anyhow!("archival batch requires at least one vector"))?;
    if rows.iter().any(|row| row.vector.len() != dimension) {
        return Err(anyhow!("archival vectors have inconsistent dimensions"));
    }

    let schema = Arc::new(Schema::new(vec![
        Field::new("id", DataType::Utf8, false),
        Field::new("text", DataType::Utf8, false),
        Field::new(
            VECTOR_COLUMN,
            DataType::FixedSizeList(
                Arc::new(Field::new("item", DataType::Float32, true)),
                dimension as i32,
            ),
            false,
        ),
        Field::new("metadata", DataType::Utf8, false),
        Field::new("tags", DataType::Utf8, false),
        Field::new("created_at", DataType::Utf8, false),
    ]));

    RecordBatch::try_new(
        schema,
        vec![
            utf8_array(rows.iter().map(|row| row.id.as_str())),
            utf8_array(rows.iter().map(|row| row.text.as_str())),
            vector_array(
                rows.iter().map(|row| row.vector.clone()).collect(),
                dimension,
            ),
            utf8_array(rows.iter().map(|row| row.metadata.as_str())),
            utf8_array(rows.iter().map(|row| row.tags.as_str())),
            utf8_array(rows.iter().map(|row| row.created_at.as_str())),
        ],
    )
    .context("failed to build archival LanceDB batch")
}

fn entries_from_batches(batches: &[RecordBatch]) -> ArchivalResult<Vec<ArchivalEntry>> {
    let mut entries = Vec::new();
    for batch in batches {
        let ids = string_column(batch, "id")?;
        let texts = string_column(batch, "text")?;
        let metadata = string_column(batch, "metadata")?;
        let tags = string_column(batch, "tags")?;
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
            let tags = serde_json::from_str(tags.value(row)).unwrap_or_default();
            let score = distances
                .as_ref()
                .and_then(|values| values.get(row).copied())
                .map(semantic_score)
                .unwrap_or(0.0);
            entries.push(ArchivalEntry {
                id: id.to_string(),
                text: texts.value(row).to_string(),
                metadata,
                tags,
                created_at: created.value(row).to_string(),
                score,
            });
        }
    }
    Ok(entries)
}

fn compare_entries(left: &ArchivalEntry, right: &ArchivalEntry) -> Ordering {
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

fn score_entry(query: &str, terms: &[String], entry: &ArchivalEntry) -> f64 {
    if terms.is_empty() {
        return 1.0;
    }
    let query_lower = query.to_ascii_lowercase();
    let text_lower = entry.text.to_ascii_lowercase();
    let tags_lower = entry.tags.join(" ").to_ascii_lowercase();
    let metadata_lower = entry.metadata.to_string().to_ascii_lowercase();
    let mut score = 0.0;

    if !query_lower.is_empty() {
        if text_lower.contains(&query_lower) {
            score += 5.0;
        }
        if metadata_lower.contains(&query_lower) {
            score += 2.0;
        }
    }

    for term in terms {
        score += text_lower.matches(term).count() as f64;
        if tags_lower.split_whitespace().any(|tag| tag == term) {
            score += 3.0;
        } else if tags_lower.contains(term) {
            score += 1.5;
        }
        if metadata_lower.contains(term) {
            score += 1.0;
        }
    }

    score
}

fn semantic_score(distance: f64) -> f64 {
    1.0 / (1.0 + distance.max(0.0))
}

fn tags_match_any(entry_tags: &[String], filter: &[String]) -> bool {
    filter.is_empty() || filter.iter().any(|tag| entry_tags.contains(tag))
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

fn clean_tags(tags: &[String]) -> Vec<String> {
    let mut clean = Vec::new();
    for tag in tags {
        let tag = tag.trim().to_ascii_lowercase();
        if !tag.is_empty() && !clean.contains(&tag) {
            clean.push(tag);
        }
    }
    clean
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

    fn memory() -> ArchivalMemory {
        let tmp = tempdir().unwrap();
        let path = tmp.path().join("lancedb");
        let memory =
            ArchivalMemory::open_with_hash_embedder(path, LEGACY_EMBEDDING_DIMENSIONS).unwrap();
        std::mem::forget(tmp);
        memory
    }

    #[test]
    fn add_get_count_and_list_recent_memory() {
        let memory = memory();
        let id = memory
            .add(
                "User prefers cargo for Rust package management.",
                Some(json!({"source": "test"})),
                &["convention".to_string(), "rust".to_string()],
            )
            .unwrap();

        assert_eq!(memory.count().unwrap(), 1);
        let entry = memory.get(&id).unwrap().unwrap();
        assert_eq!(entry.id, id);
        assert_eq!(entry.metadata["source"], "test");
        assert_eq!(entry.tags, vec!["convention", "rust"]);

        let recent = memory.list_recent(10).unwrap();
        assert_eq!(recent.len(), 1);
        assert!(ArchivalMemory::format_entries(&recent).contains("cargo for Rust"));
    }

    #[test]
    fn search_ranks_text_and_filters_by_any_tag() {
        let memory = memory();
        let email = memory
            .add(
                "Graph API token lives in graph_tokens.json.",
                None,
                &["skill".to_string(), "email".to_string()],
            )
            .unwrap();
        memory
            .add(
                "Use cargo fmt before Rust commits.",
                None,
                &["convention".to_string(), "rust".to_string()],
            )
            .unwrap();

        let results = memory.search("graph api email", 10, None).unwrap();
        assert_eq!(results[0].id, email);
        assert!(results[0].score > 0.0);

        let filtered = memory
            .search("graph cargo", 10, Some(&["rust".to_string()]))
            .unwrap();
        assert_eq!(filtered.len(), 1);
        assert!(filtered[0].text.contains("cargo fmt"));
    }

    #[test]
    fn update_tags_delete_and_invalid_inputs() {
        let memory = memory();
        let id = memory.add("Remember this", None, &[]).unwrap();
        assert!(
            memory
                .update_tags(&id, &["Skill".to_string(), "skill".to_string()])
                .unwrap()
        );
        assert_eq!(memory.get(&id).unwrap().unwrap().tags, vec!["skill"]);
        assert!(memory.delete(&id).unwrap());
        assert!(memory.get(&id).unwrap().is_none());

        assert!(matches!(
            memory.add(" ", None, &[]).unwrap_err(),
            ArchivalError::EmptyText
        ));
        assert!(matches!(
            memory.add("text", Some(json!("bad")), &[]).unwrap_err(),
            ArchivalError::InvalidMetadata
        ));
    }
}
