use std::cmp::Ordering;
use std::collections::BTreeSet;
use std::fs;
use std::path::{Component, Path, PathBuf};
use std::sync::Arc;

use anyhow::{Context, Result as AnyhowResult, anyhow};
use arrow_array::RecordBatch;
use arrow_schema::{DataType, Field, Schema};
use chrono::Utc;
use futures::TryStreamExt;
use lancedb::index::Index;
use lancedb::index::scalar::FtsIndexBuilder;
use lancedb::query::{ExecutableQuery, QueryBase};
use serde::{Deserialize, Serialize};
use thiserror::Error;
use uuid::Uuid;
use walkdir::WalkDir;

use crate::semantic::{
    EmbeddingEngine, LEGACY_EMBEDDING_DIMENSIONS, VECTOR_COLUMN, distance_column, run_lancedb,
    string_column, utf8_array, vector_array,
};

const TABLE_NAME: &str = "notes";
const INIT_ID: &str = "_init_";

#[derive(Debug, Error)]
pub enum NoteError {
    #[error("note title is required")]
    EmptyTitle,
    #[error("unsafe notes subdirectory: {0}")]
    UnsafeSubdir(String),
    #[error(transparent)]
    Io(#[from] std::io::Error),
    #[error(transparent)]
    Walkdir(#[from] walkdir::Error),
    #[error(transparent)]
    Json(#[from] serde_json::Error),
    #[error(transparent)]
    Lance(#[from] anyhow::Error),
}

pub type NoteResult<T> = Result<T, NoteError>;

#[derive(Clone, Debug, Default, Eq, PartialEq, Serialize, Deserialize)]
pub struct NoteMetadata {
    pub title: String,
    pub tags: Vec<String>,
    pub created: String,
    pub updated: String,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct NoteSummary {
    pub title: String,
    pub tags: Vec<String>,
    pub file_path: PathBuf,
    pub created: String,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct NoteSearchResult {
    pub title: String,
    pub tags: Vec<String>,
    pub file_path: PathBuf,
    pub preview: String,
    pub score: f64,
    pub created: String,
}

#[derive(Clone, Debug)]
pub struct NoteStore {
    notes_dir: PathBuf,
    lancedb_dir: Option<PathBuf>,
    embedder: Option<EmbeddingEngine>,
}

#[derive(Debug)]
struct NoteRow {
    id: String,
    title: String,
    text: String,
    tags: String,
    file_path: String,
    vector: Vec<f32>,
    created_at: String,
    updated_at: String,
}

impl NoteStore {
    pub fn new(notes_dir: impl Into<PathBuf>) -> NoteResult<Self> {
        let notes_dir = notes_dir.into();
        fs::create_dir_all(&notes_dir)?;
        Ok(Self {
            notes_dir,
            lancedb_dir: None,
            embedder: None,
        })
    }

    pub fn new_with_lancedb(
        notes_dir: impl Into<PathBuf>,
        lancedb_dir: impl Into<PathBuf>,
    ) -> NoteResult<Self> {
        let notes_dir = notes_dir.into();
        let lancedb_dir = lancedb_dir.into();
        fs::create_dir_all(&notes_dir)?;
        let store = Self {
            notes_dir,
            embedder: Some(EmbeddingEngine::from_env(&lancedb_dir)),
            lancedb_dir: Some(lancedb_dir),
        };
        store.ensure_lancedb_schema()?;
        Ok(store)
    }

    pub fn create(
        &self,
        title: &str,
        content: &str,
        tags: &[String],
        subdir: Option<&str>,
    ) -> NoteResult<PathBuf> {
        let title = title.trim();
        if title.is_empty() {
            return Err(NoteError::EmptyTitle);
        }

        let target_dir = self.target_dir(subdir)?;
        fs::create_dir_all(&target_dir)?;

        let slug = slugify(title);
        let mut path = target_dir.join(format!("{slug}.md"));
        let mut counter = 1;
        while path.exists() {
            counter += 1;
            path = target_dir.join(format!("{slug}_{counter}.md"));
        }

        let today = Utc::now().format("%Y-%m-%d").to_string();
        let meta = NoteMetadata {
            title: title.to_string(),
            tags: clean_tags(tags),
            created: today.clone(),
            updated: today,
        };
        let file_content = format!("{}\n\n{}\n", render_frontmatter(&meta), content.trim());
        fs::write(&path, file_content)?;
        self.index_note_file(&path)?;
        Ok(path)
    }

    pub fn list_notes(&self, tags: Option<&[String]>) -> NoteResult<Vec<NoteSummary>> {
        let tag_filter = clean_tags(tags.unwrap_or_default());
        let mut notes = Vec::new();
        for path in self.markdown_files()? {
            let raw = fs::read_to_string(&path)?;
            let (meta, _) = parse_frontmatter(&raw);
            let note_tags = clean_tags(&meta.tags);
            if !tag_filter.is_empty() && !tag_filter.iter().all(|tag| note_tags.contains(tag)) {
                continue;
            }
            notes.push(NoteSummary {
                title: if meta.title.is_empty() {
                    path.file_stem()
                        .and_then(|stem| stem.to_str())
                        .unwrap_or("untitled")
                        .to_string()
                } else {
                    meta.title
                },
                tags: note_tags,
                file_path: path,
                created: meta.created,
            });
        }
        notes.sort_by(|left, right| left.file_path.cmp(&right.file_path));
        Ok(notes)
    }

    pub fn search(
        &self,
        query: &str,
        tags: Option<&[String]>,
        limit: usize,
    ) -> NoteResult<Vec<NoteSearchResult>> {
        let query = query.trim();
        let query_terms = query_terms(query);
        let tag_filter = clean_tags(tags.unwrap_or_default());
        let mut results = Vec::new();

        for path in self.markdown_files()? {
            let raw = fs::read_to_string(&path)?;
            let (meta, body) = parse_frontmatter(&raw);
            let note_tags = clean_tags(&meta.tags);
            if !tag_filter.is_empty() && !tag_filter.iter().all(|tag| note_tags.contains(tag)) {
                continue;
            }
            let title = if meta.title.is_empty() {
                path.file_stem()
                    .and_then(|stem| stem.to_str())
                    .unwrap_or("untitled")
                    .to_string()
            } else {
                meta.title
            };
            let score = score_note(query, &query_terms, &title, &note_tags, &body);
            if score <= 0.0 && !query_terms.is_empty() {
                continue;
            }
            results.push(NoteSearchResult {
                title,
                tags: note_tags,
                file_path: path,
                preview: preview(&body),
                score,
                created: meta.created,
            });
        }

        if let Some(mut indexed) = self.search_lancedb(query, limit * 3)? {
            for result in indexed.drain(..) {
                if !tag_filter.is_empty() && !tag_filter.iter().all(|tag| result.tags.contains(tag))
                {
                    continue;
                }
                if let Some(existing) = results
                    .iter_mut()
                    .find(|existing| existing.file_path == result.file_path)
                {
                    existing.score += result.score;
                } else {
                    results.push(result);
                }
            }
        }

        results.sort_by(compare_search_results);
        results.truncate(if limit == 0 { 5 } else { limit });
        Ok(results)
    }

    pub fn all_tags(&self) -> NoteResult<Vec<String>> {
        let mut tags = BTreeSet::new();
        for note in self.list_notes(None)? {
            tags.extend(note.tags);
        }
        Ok(tags.into_iter().collect())
    }

    pub fn reindex(&self) -> NoteResult<usize> {
        let files = self.markdown_files()?;
        self.rebuild_lancedb(&files)?;
        Ok(files.len())
    }

    pub fn format_list(notes: &[NoteSummary]) -> String {
        if notes.is_empty() {
            return "No notes found.".to_string();
        }
        let mut lines = vec![format!("{} notes:", notes.len())];
        for note in notes {
            let tags = format_tags(&note.tags);
            lines.push(format!(
                "- **{}** [{}] - {}",
                note.title,
                tags,
                note.file_path.display()
            ));
        }
        lines.join("\n")
    }

    pub fn format_search(query: &str, tags: &[String], results: &[NoteSearchResult]) -> String {
        if results.is_empty() {
            let tag_suffix = if tags.is_empty() {
                String::new()
            } else {
                format!(" (tags: {})", tags.join(","))
            };
            return format!("No notes found for: {query}{tag_suffix}");
        }

        let mut lines = vec![format!("Found {} notes:", results.len())];
        for result in results {
            let tags = format_tags(&result.tags);
            lines.push(format!(
                "\n**{}** [{}]\n  File: {}\n  {}",
                result.title,
                tags,
                result.file_path.display(),
                result.preview.replace('\n', " ")
            ));
        }
        lines.join("\n")
    }

    fn markdown_files(&self) -> NoteResult<Vec<PathBuf>> {
        let mut paths = Vec::new();
        for entry in WalkDir::new(&self.notes_dir).follow_links(false) {
            let entry = entry?;
            if !entry.file_type().is_file() {
                continue;
            }
            let path = entry.into_path();
            if path.extension().and_then(|ext| ext.to_str()) == Some("md") {
                paths.push(path);
            }
        }
        paths.sort();
        Ok(paths)
    }

    fn target_dir(&self, subdir: Option<&str>) -> NoteResult<PathBuf> {
        let Some(subdir) = subdir.map(str::trim).filter(|value| !value.is_empty()) else {
            return Ok(self.notes_dir.clone());
        };
        let path = Path::new(subdir);
        let safe = path.components().all(|component| {
            matches!(component, Component::Normal(_)) || matches!(component, Component::CurDir)
        });
        if path.is_absolute() || !safe {
            return Err(NoteError::UnsafeSubdir(subdir.to_string()));
        }
        Ok(self.notes_dir.join(path))
    }

    fn ensure_lancedb_schema(&self) -> NoteResult<()> {
        let Some(lancedb_dir) = &self.lancedb_dir else {
            return Ok(());
        };
        fs::create_dir_all(lancedb_dir)?;
        let db_path = db_uri(lancedb_dir);
        run_lancedb(async move {
            let db = lancedb::connect(&db_path).execute().await?;
            let tables = db.table_names().execute().await?;
            if !tables.iter().any(|name| name == TABLE_NAME) {
                let now = Utc::now().to_rfc3339();
                let init = NoteRow {
                    id: INIT_ID.to_string(),
                    title: String::new(),
                    text: String::new(),
                    tags: "[]".to_string(),
                    file_path: String::new(),
                    vector: vec![0.0; LEGACY_EMBEDDING_DIMENSIONS],
                    created_at: now.clone(),
                    updated_at: now,
                };
                db.create_table(TABLE_NAME, note_batch(&[init])?)
                    .execute()
                    .await?;
                let table = db.open_table(TABLE_NAME).execute().await?;
                if let Err(error) = table
                    .create_index(&["text"], Index::FTS(FtsIndexBuilder::default()))
                    .execute()
                    .await
                {
                    tracing::debug!("notes FTS index creation skipped: {error}");
                }
            }
            Ok(())
        })?;
        Ok(())
    }

    fn index_note_file(&self, path: &Path) -> NoteResult<()> {
        let Some(lancedb_dir) = &self.lancedb_dir else {
            return Ok(());
        };
        let Some(embedder) = &self.embedder else {
            return Ok(());
        };
        let row = self.note_row(path, embedder)?;
        let batch = note_batch(&[row])?;
        let db_path = db_uri(lancedb_dir);
        let file_path = path.display().to_string();
        run_lancedb(async move {
            let db = lancedb::connect(&db_path).execute().await?;
            let table = db.open_table(TABLE_NAME).execute().await?;
            table
                .delete(&format!("file_path = {}", sql_string_literal(&file_path)))
                .await?;
            table.add(batch).execute().await?;
            Ok(())
        })?;
        Ok(())
    }

    fn rebuild_lancedb(&self, files: &[PathBuf]) -> NoteResult<()> {
        let Some(lancedb_dir) = &self.lancedb_dir else {
            return Ok(());
        };
        let Some(embedder) = &self.embedder else {
            return Ok(());
        };
        self.ensure_lancedb_schema()?;
        let mut rows = Vec::new();
        for file in files {
            rows.push(self.note_row(file, embedder)?);
        }
        let batch = if rows.is_empty() {
            None
        } else {
            Some(note_batch(&rows)?)
        };
        let db_path = db_uri(lancedb_dir);
        run_lancedb(async move {
            let db = lancedb::connect(&db_path).execute().await?;
            let table = db.open_table(TABLE_NAME).execute().await?;
            table
                .delete(&format!("id != {}", sql_string_literal(INIT_ID)))
                .await?;
            if let Some(batch) = batch {
                table.add(batch).execute().await?;
            }
            if let Err(error) = table
                .create_index(&["text"], Index::FTS(FtsIndexBuilder::default()))
                .execute()
                .await
            {
                tracing::debug!("notes FTS index rebuild skipped: {error}");
            }
            Ok(())
        })?;
        Ok(())
    }

    fn note_row(&self, path: &Path, embedder: &EmbeddingEngine) -> NoteResult<NoteRow> {
        let raw = fs::read_to_string(path)?;
        let (meta, body) = parse_frontmatter(&raw);
        let tags = clean_tags(&meta.tags);
        let title = if meta.title.is_empty() {
            path.file_stem()
                .and_then(|stem| stem.to_str())
                .unwrap_or("untitled")
                .to_string()
        } else {
            meta.title
        };
        let text = format!("{}\n{}\n{}", title, tags.join(" "), body.trim());
        let vector = embedder.embed_document(&text)?;
        let now = Utc::now().to_rfc3339();
        Ok(NoteRow {
            id: format!("note-{}", Uuid::new_v4()),
            title,
            text,
            tags: serde_json::to_string(&tags)?,
            file_path: path.display().to_string(),
            vector,
            created_at: if meta.created.is_empty() {
                now.clone()
            } else {
                meta.created
            },
            updated_at: if meta.updated.is_empty() {
                now
            } else {
                meta.updated
            },
        })
    }

    fn search_lancedb(
        &self,
        query: &str,
        limit: usize,
    ) -> NoteResult<Option<Vec<NoteSearchResult>>> {
        let Some(lancedb_dir) = &self.lancedb_dir else {
            return Ok(None);
        };
        let Some(embedder) = &self.embedder else {
            return Ok(None);
        };
        if query.trim().is_empty() {
            return Ok(None);
        }
        let query_vector = embedder.embed_query(query.trim())?;
        let db_path = db_uri(lancedb_dir);
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
        Ok(Some(note_results_from_batches(&batches)?))
    }
}

fn note_batch(rows: &[NoteRow]) -> AnyhowResult<RecordBatch> {
    let dimension = rows
        .first()
        .map(|row| row.vector.len())
        .filter(|dimension| *dimension > 0)
        .ok_or_else(|| anyhow!("notes batch requires at least one vector"))?;
    if rows.iter().any(|row| row.vector.len() != dimension) {
        return Err(anyhow!("note vectors have inconsistent dimensions"));
    }

    let schema = Arc::new(Schema::new(vec![
        Field::new("id", DataType::Utf8, false),
        Field::new("title", DataType::Utf8, false),
        Field::new("text", DataType::Utf8, false),
        Field::new("tags", DataType::Utf8, false),
        Field::new("file_path", DataType::Utf8, false),
        Field::new(
            VECTOR_COLUMN,
            DataType::FixedSizeList(
                Arc::new(Field::new("item", DataType::Float32, true)),
                dimension as i32,
            ),
            false,
        ),
        Field::new("created_at", DataType::Utf8, false),
        Field::new("updated_at", DataType::Utf8, false),
    ]));

    RecordBatch::try_new(
        schema,
        vec![
            utf8_array(rows.iter().map(|row| row.id.as_str())),
            utf8_array(rows.iter().map(|row| row.title.as_str())),
            utf8_array(rows.iter().map(|row| row.text.as_str())),
            utf8_array(rows.iter().map(|row| row.tags.as_str())),
            utf8_array(rows.iter().map(|row| row.file_path.as_str())),
            vector_array(
                rows.iter().map(|row| row.vector.clone()).collect(),
                dimension,
            ),
            utf8_array(rows.iter().map(|row| row.created_at.as_str())),
            utf8_array(rows.iter().map(|row| row.updated_at.as_str())),
        ],
    )
    .context("failed to build notes LanceDB batch")
}

fn note_results_from_batches(batches: &[RecordBatch]) -> NoteResult<Vec<NoteSearchResult>> {
    let mut notes = Vec::new();
    for batch in batches {
        let ids = string_column(batch, "id")?;
        let titles = string_column(batch, "title")?;
        let texts = string_column(batch, "text")?;
        let tags = string_column(batch, "tags")?;
        let paths = string_column(batch, "file_path")?;
        let created = string_column(batch, "created_at")?;
        let distances = distance_column(batch);

        for row in 0..batch.num_rows() {
            if ids.value(row) == INIT_ID {
                continue;
            }
            let file_path = PathBuf::from(paths.value(row));
            let preview = fs::read_to_string(&file_path)
                .ok()
                .map(|raw| parse_frontmatter(&raw).1)
                .map(|body| preview(&body))
                .unwrap_or_else(|| preview(texts.value(row)));
            let note_tags = serde_json::from_str(tags.value(row)).unwrap_or_default();
            let score = distances
                .as_ref()
                .and_then(|values| values.get(row).copied())
                .map(semantic_score)
                .unwrap_or(0.0);
            notes.push(NoteSearchResult {
                title: titles.value(row).to_string(),
                tags: note_tags,
                file_path,
                preview,
                score,
                created: created.value(row).to_string(),
            });
        }
    }
    Ok(notes)
}

fn semantic_score(distance: f64) -> f64 {
    1.0 / (1.0 + distance.max(0.0))
}

fn db_uri(path: &Path) -> String {
    path.display().to_string()
}

fn sql_string_literal(value: &str) -> String {
    format!("'{}'", value.replace('\'', "''"))
}

pub fn slugify(title: &str) -> String {
    let mut slug = String::new();
    let mut previous_sep = false;
    for ch in title.trim().to_ascii_lowercase().chars() {
        if ch.is_ascii_alphanumeric() {
            slug.push(ch);
            previous_sep = false;
        } else if matches!(ch, ' ' | '\t' | '\n' | '\r' | '_' | '-') && !previous_sep {
            slug.push('_');
            previous_sep = true;
        }
        if slug.len() >= 80 {
            break;
        }
    }
    let slug = slug.trim_matches('_').to_string();
    if slug.is_empty() {
        "untitled".to_string()
    } else {
        slug
    }
}

pub fn parse_frontmatter(text: &str) -> (NoteMetadata, String) {
    if !text.starts_with("---") {
        return (NoteMetadata::default(), text.to_string());
    }
    let Some(end) = text[3..].find("\n---") else {
        return (NoteMetadata::default(), text.to_string());
    };
    let header = text[3..3 + end].trim();
    let body = text[3 + end + 4..].trim().to_string();
    let mut meta = NoteMetadata::default();

    for line in header.lines() {
        let line = line.trim();
        let Some((key, value)) = line.split_once(':') else {
            continue;
        };
        let key = key.trim();
        let value = value.trim();
        match key {
            "title" => meta.title = value.to_string(),
            "tags" => meta.tags = parse_tag_value(value),
            "created" => meta.created = value.to_string(),
            "updated" => meta.updated = value.to_string(),
            _ => {}
        }
    }

    (meta, body)
}

pub fn render_frontmatter(meta: &NoteMetadata) -> String {
    format!(
        "---\ntitle: {}\ntags: [{}]\ncreated: {}\nupdated: {}\n---",
        scalar(&meta.title),
        clean_tags(&meta.tags).join(", "),
        scalar(&meta.created),
        scalar(&meta.updated)
    )
}

pub fn normalize_tags(tags: &[String], existing_tags: &[String]) -> Vec<String> {
    let existing: BTreeSet<String> = clean_tags(existing_tags).into_iter().collect();
    let mut normalized = Vec::new();
    let mut seen = BTreeSet::new();

    for tag in clean_tags(tags) {
        let candidate = if existing.contains(&tag) {
            tag
        } else if tag.ends_with('s') && existing.contains(&tag[..tag.len() - 1]) {
            tag[..tag.len() - 1].to_string()
        } else if !tag.ends_with('s') && existing.contains(&format!("{tag}s")) {
            format!("{tag}s")
        } else {
            let swapped = if tag.contains('-') {
                tag.replace('-', "_")
            } else {
                tag.replace('_', "-")
            };
            if existing.contains(&swapped) {
                swapped
            } else {
                tag
            }
        };
        if seen.insert(candidate.clone()) {
            normalized.push(candidate);
        }
    }

    normalized
}

fn compare_search_results(left: &NoteSearchResult, right: &NoteSearchResult) -> Ordering {
    right
        .score
        .partial_cmp(&left.score)
        .unwrap_or(Ordering::Equal)
        .then_with(|| right.created.cmp(&left.created))
        .then_with(|| left.title.cmp(&right.title))
}

fn score_note(query: &str, terms: &[String], title: &str, tags: &[String], body: &str) -> f64 {
    if terms.is_empty() {
        return 1.0;
    }

    let title_lower = title.to_ascii_lowercase();
    let tags_lower = tags.join(" ").to_ascii_lowercase();
    let body_lower = body.to_ascii_lowercase();
    let query_lower = query.to_ascii_lowercase();
    let mut score = 0.0;

    if !query_lower.is_empty() {
        if title_lower.contains(&query_lower) {
            score += 8.0;
        }
        if body_lower.contains(&query_lower) {
            score += 3.0;
        }
    }

    for term in terms {
        if title_lower.contains(term) {
            score += 4.0;
        }
        if tags_lower.split_whitespace().any(|tag| tag == term) {
            score += 3.0;
        } else if tags_lower.contains(term) {
            score += 1.5;
        }
        score += body_lower.matches(term).count() as f64;
    }

    score
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
    let mut seen = BTreeSet::new();
    for tag in tags {
        let normalized = tag.trim().to_ascii_lowercase();
        if !normalized.is_empty() && seen.insert(normalized.clone()) {
            clean.push(normalized);
        }
    }
    clean
}

fn parse_tag_value(value: &str) -> Vec<String> {
    let trimmed = value.trim();
    if trimmed.starts_with('[') && trimmed.ends_with(']') {
        trimmed[1..trimmed.len() - 1]
            .split(',')
            .map(|value| {
                value
                    .trim()
                    .trim_matches('"')
                    .trim_matches('\'')
                    .to_string()
            })
            .collect::<Vec<_>>()
    } else {
        vec![trimmed.trim_matches('"').trim_matches('\'').to_string()]
    }
}

fn preview(body: &str) -> String {
    body.chars().take(300).collect()
}

fn format_tags(tags: &[String]) -> String {
    if tags.is_empty() {
        "none".to_string()
    } else {
        tags.join(", ")
    }
}

fn scalar(value: &str) -> String {
    value.replace(['\n', '\r'], " ").trim().to_string()
}

#[cfg(test)]
mod tests {
    use tempfile::tempdir;

    use super::*;

    #[test]
    fn creates_lists_searches_and_formats_notes() {
        let tmp = tempdir().unwrap();
        let store = NoteStore::new(tmp.path()).unwrap();

        let path = store
            .create(
                "Read UNIGE email via Microsoft Graph API",
                "## What\nAccess Outlook email.\n\n## How\nRefresh via MSAL and curl with Bearer token.",
                &["skill".to_string(), "email".to_string(), "graph-api".to_string()],
                None,
            )
            .unwrap();
        assert!(path.exists());
        assert_eq!(
            path.file_name().and_then(|name| name.to_str()).unwrap(),
            "read_unige_email_via_microsoft_graph_api.md"
        );

        store
            .create(
                "Use cargo for Rust package management",
                "Use `cargo build` and `cargo test` from the repository root.",
                &["convention".to_string(), "rust".to_string()],
                None,
            )
            .unwrap();

        let notes = store.list_notes(None).unwrap();
        assert_eq!(notes.len(), 2);
        assert!(NoteStore::format_list(&notes).contains("2 notes"));

        let skills = store.list_notes(Some(&["skill".to_string()])).unwrap();
        assert_eq!(skills.len(), 1);
        assert!(skills[0].title.contains("Graph API"));

        let results = store
            .search("how to read email with graph api", None, 5)
            .unwrap();
        assert!(!results.is_empty());
        assert!(results[0].title.contains("Graph API"));
        let formatted = NoteStore::format_search("email", &[], &results);
        assert!(formatted.contains("Found"));
        assert!(formatted.contains("File:"));
    }

    #[test]
    fn reindex_counts_markdown_files_and_unique_paths_do_not_overwrite() {
        let tmp = tempdir().unwrap();
        let store = NoteStore::new(tmp.path()).unwrap();
        let first = store
            .create("Same title", "first", &["test".to_string()], None)
            .unwrap();
        let second = store
            .create("Same title", "second", &["test".to_string()], None)
            .unwrap();

        assert_ne!(first, second);
        assert_eq!(store.reindex().unwrap(), 2);
        assert_eq!(store.search("second", None, 5).unwrap().len(), 1);
    }

    #[test]
    fn rejects_unsafe_subdirectories_and_empty_titles() {
        let tmp = tempdir().unwrap();
        let store = NoteStore::new(tmp.path()).unwrap();

        assert!(matches!(
            store.create("", "body", &[], None).unwrap_err(),
            NoteError::EmptyTitle
        ));
        assert!(matches!(
            store
                .create("title", "body", &[], Some("../outside"))
                .unwrap_err(),
            NoteError::UnsafeSubdir(_)
        ));
    }

    #[test]
    fn parses_frontmatter_and_normalizes_tags() {
        let (meta, body) = parse_frontmatter(
            "---\ntitle: Test\ntags: [skills, graph_api]\ncreated: 2026-05-22\nupdated: 2026-05-22\n---\n\nBody",
        );
        assert_eq!(meta.title, "Test");
        assert_eq!(body, "Body");

        let normalized = normalize_tags(
            &["Skills".to_string(), "graph-api".to_string()],
            &["skill".to_string(), "graph_api".to_string()],
        );
        assert_eq!(normalized, vec!["skill", "graph_api"]);
    }
}
