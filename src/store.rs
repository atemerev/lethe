use std::fs;
use std::path::{Path, PathBuf};

use chrono::{DateTime, Local, Utc};
use serde::{Deserialize, Serialize};
use thiserror::Error;

use crate::config::Settings;
use crate::memory::archival::{ArchivalEntry, ArchivalError, ArchivalMemory};
use crate::memory::messages::{MessageHistory, MessageHistoryError, StoredMessage};
use crate::memory::notes::{NoteError, NoteSearchResult, NoteStore};
use crate::memory::{BlockManager, MemoryBlock, MemoryDb, MemoryError};

#[derive(Debug, Error)]
pub enum MemoryStoreError {
    #[error(transparent)]
    Blocks(#[from] MemoryError),
    #[error(transparent)]
    Archival(#[from] ArchivalError),
    #[error(transparent)]
    Messages(#[from] MessageHistoryError),
    #[error(transparent)]
    Notes(#[from] NoteError),
    #[error(transparent)]
    Io(#[from] std::io::Error),
    #[error(transparent)]
    Sqlite(#[from] rusqlite::Error),
}

pub type MemoryStoreResult<T> = Result<T, MemoryStoreError>;

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct MemoryStats {
    pub memory_blocks: usize,
    pub archival_memories: usize,
    pub message_history: usize,
    pub total_messages: usize,
    pub notes: usize,
}

#[derive(Debug)]
pub struct MemoryStore {
    pub blocks: BlockManager,
    pub archival: ArchivalMemory,
    pub messages: MessageHistory,
    pub notes: NoteStore,
    db_path: PathBuf,
    memory_data_path: PathBuf,
    workspace_dir: PathBuf,
}

impl MemoryStore {
    pub fn from_settings(settings: &Settings) -> MemoryStoreResult<Self> {
        Self::open_with_data_path(
            &settings.workspace_dir,
            &settings.db_path,
            &settings.notes_dir,
            settings.memory_dir.join("lethe-memory.db"),
        )
    }

    pub fn open(
        workspace_dir: impl Into<PathBuf>,
        db_path: impl Into<PathBuf>,
        notes_dir: impl Into<PathBuf>,
    ) -> MemoryStoreResult<Self> {
        let workspace_dir = workspace_dir.into();
        let db_path = db_path.into();
        let notes_dir = notes_dir.into();
        let memory_data_path = memory_data_path_for(&db_path);
        Self::open_with_data_path(workspace_dir, db_path, notes_dir, memory_data_path)
    }

    pub fn open_with_data_path(
        workspace_dir: impl Into<PathBuf>,
        db_path: impl Into<PathBuf>,
        notes_dir: impl Into<PathBuf>,
        memory_data_path: impl Into<PathBuf>,
    ) -> MemoryStoreResult<Self> {
        let workspace_dir = workspace_dir.into();
        let db_path = db_path.into();
        let notes_dir = notes_dir.into();
        let memory_data_path = memory_data_path.into();

        fs::create_dir_all(&workspace_dir)?;
        fs::create_dir_all(workspace_dir.join("projects"))?;
        ensure_skills_bootstrap(&workspace_dir.join("skills"))?;

        let blocks = BlockManager::new(workspace_dir.join("memory"))?;
        blocks.init_embedded_defaults()?;
        let memory_db = MemoryDb::open(&memory_data_path)?;
        let archival = ArchivalMemory::from_db(memory_db.clone());
        let messages = MessageHistory::open(&memory_data_path)?;
        let notes = NoteStore::new_with_db(notes_dir, memory_db)?;

        Ok(Self {
            blocks,
            archival,
            messages,
            notes,
            db_path,
            memory_data_path,
            workspace_dir,
        })
    }

    pub fn db_path(&self) -> &Path {
        &self.db_path
    }

    pub fn memory_data_path(&self) -> &Path {
        &self.memory_data_path
    }

    pub fn workspace_dir(&self) -> &Path {
        &self.workspace_dir
    }

    pub fn stats(&self) -> MemoryStoreResult<MemoryStats> {
        let memory_blocks = self.blocks.list_blocks(true)?.len();
        let archival_memories = self.archival.count()?;
        let message_history = self.messages.count()?;
        let notes = self.notes.list_notes(None)?.len();
        Ok(MemoryStats {
            memory_blocks,
            archival_memories,
            message_history,
            total_messages: message_history,
            notes,
        })
    }

    pub fn get_context_for_prompt(&self) -> MemoryStoreResult<String> {
        let (stable, volatile) = self.get_context_split()?;
        Ok([stable, volatile]
            .into_iter()
            .filter(|part| !part.trim().is_empty())
            .collect::<Vec<_>>()
            .join("\n\n"))
    }

    pub fn get_context_split(&self) -> MemoryStoreResult<(String, String)> {
        let blocks = self.blocks.list_blocks(false)?;
        let mut stable_blocks = Vec::new();
        let mut volatile_blocks = Vec::new();

        for block in &blocks {
            if block.hidden || block.label == "identity" {
                continue;
            }
            let formatted = format_block(block);
            if is_stable_block(&block.label) {
                stable_blocks.push(formatted);
            } else {
                volatile_blocks.push(formatted);
            }
        }

        let mut stable_parts = Vec::new();
        let mut volatile_parts = Vec::new();
        if !stable_blocks.is_empty() {
            stable_parts.push(format!(
                "<memory_blocks_stable>\n{}\n</memory_blocks_stable>",
                stable_blocks.join("\n\n")
            ));
        }
        if !volatile_blocks.is_empty() {
            volatile_parts.push(format!(
                "<memory_blocks>\n{}\n</memory_blocks>",
                volatile_blocks.join("\n\n")
            ));
        }

        volatile_parts.push(self.format_memory_metadata(&blocks)?);

        Ok((stable_parts.join("\n\n"), volatile_parts.join("\n\n")))
    }

    pub fn search_archival(
        &self,
        query: &str,
        limit: usize,
        tags: Option<&[String]>,
    ) -> MemoryStoreResult<Vec<ArchivalEntry>> {
        self.archival
            .search(query, normalized_limit(limit, 10), tags)
            .map_err(Into::into)
    }

    pub fn search_messages(
        &self,
        query: &str,
        limit: usize,
        role: Option<&str>,
    ) -> MemoryStoreResult<Vec<StoredMessage>> {
        self.messages
            .search(query, normalized_limit(limit, 20), role)
            .map_err(Into::into)
    }

    pub fn search_notes(
        &self,
        query: &str,
        tags: Option<&[String]>,
        limit: usize,
    ) -> MemoryStoreResult<Vec<NoteSearchResult>> {
        self.notes
            .search(query, tags, normalized_limit(limit, 5))
            .map_err(Into::into)
    }

    fn format_memory_metadata(&self, blocks: &[MemoryBlock]) -> MemoryStoreResult<String> {
        let now = Utc::now();
        let last_modified = blocks
            .iter()
            .filter_map(|block| block.updated_at.or(block.created_at))
            .max()
            .unwrap_or(now);
        let message_count = self.messages.count()?;
        let archival_count = self.archival.count()?;

        let mut lines = vec![
            "<memory_metadata>".to_string(),
            format!("- now={}", format_timestamp(now)),
            format!(
                "- memory_blocks_last_modified={}",
                format_timestamp(last_modified)
            ),
            format!(
                "- {message_count} previous messages between you and the user are stored in recall memory (use tools to access them)"
            ),
            "- Timestamps on messages are for your reference only. Do not include timestamps in your responses.".to_string(),
        ];
        if archival_count > 0 {
            lines.push(format!(
                "- {archival_count} total memories you created are stored in archival memory (use tools to access them)"
            ));
        }
        lines.push("</memory_metadata>".to_string());
        Ok(lines.join("\n"))
    }
}

fn ensure_skills_bootstrap(skills_dir: &Path) -> std::io::Result<()> {
    fs::create_dir_all(skills_dir)?;
    let readme = skills_dir.join("README.md");
    if readme.exists() {
        return Ok(());
    }
    fs::write(
        readme,
        "# Skills\n\n\
This directory stores skill files with extended workflows and references.\n\
This README is intentionally always present so skills are discoverable.\n\n\
Use core tools to work with skills:\n\
- list_directory(\"workspace/skills/\")\n\
- read_file(\"workspace/skills/README.md\")\n\
- read_file(\"workspace/skills/<name>.md\")\n\
- grep_search(\"keyword\", path=\"workspace/skills/\")\n",
    )
}

fn format_block(block: &MemoryBlock) -> String {
    let mut lines = vec![
        format!("<{}>", block.label),
        "<description>".to_string(),
        block.description.clone(),
        "</description>".to_string(),
        "<metadata>".to_string(),
        format!("- chars={}/{}", block.value.len(), block.limit),
    ];
    if let Some(created_at) = block.created_at {
        lines.push(format!("- created_at={}", format_timestamp(created_at)));
    }
    if let Some(updated_at) = block.updated_at {
        lines.push(format!("- updated_at={}", format_timestamp(updated_at)));
    }
    lines.extend([
        "</metadata>".to_string(),
        "<value>".to_string(),
        block.value.clone(),
        "</value>".to_string(),
        format!("</{}>", block.label),
    ]);
    lines.join("\n")
}

fn is_stable_block(label: &str) -> bool {
    label == "human"
}

fn format_timestamp(time: DateTime<Utc>) -> String {
    time.with_timezone(&Local)
        .format("%a %Y-%m-%d %H:%M:%S %Z")
        .to_string()
}

fn memory_data_path_for(db_path: &Path) -> PathBuf {
    db_path
        .parent()
        .map(|parent| parent.join("memory").join("lethe-memory.db"))
        .unwrap_or_else(|| PathBuf::from("memory").join("lethe-memory.db"))
}

fn normalized_limit(limit: usize, default: usize) -> usize {
    if limit == 0 { default } else { limit }
}

#[cfg(test)]
mod tests {
    use serde_json::json;
    use tempfile::tempdir;

    use super::*;

    fn store() -> (tempfile::TempDir, MemoryStore) {
        let tmp = tempdir().unwrap();
        let workspace = tmp.path().join("workspace");
        let db_path = tmp.path().join("data").join("lethe.db");
        let notes_dir = workspace.join("notes");
        let store = MemoryStore::open(workspace, db_path, notes_dir).unwrap();
        (tmp, store)
    }

    #[test]
    fn open_initializes_workspace_blocks_and_stats() {
        let (tmp, store) = store();

        assert!(store.workspace_dir().join("skills/README.md").exists());
        assert!(store.workspace_dir().join("projects").exists());
        let labels = store
            .blocks
            .list_blocks(true)
            .unwrap()
            .into_iter()
            .map(|block| block.label)
            .collect::<Vec<_>>();
        assert_eq!(labels, vec!["human", "identity", "project"]);

        store
            .notes
            .create("Test note", "body", &["skill".to_string()], None)
            .unwrap();
        let stats = store.stats().unwrap();
        assert_eq!(stats.memory_blocks, 3);
        assert_eq!(stats.notes, 1);
        assert_eq!(stats.total_messages, 0);
        drop(tmp);
    }

    #[test]
    fn context_split_formats_blocks_and_metadata_counts() {
        let (_tmp, store) = store();
        store
            .blocks
            .update("human", Some("User prefers concise answers."), None)
            .unwrap();
        store
            .blocks
            .update("project", Some("Port Lethe to Rust."), None)
            .unwrap();
        store.messages.add("user", "hello", None).unwrap();
        store
            .archival
            .add(
                "Remember the Rust port context.",
                Some(json!({"source": "test"})),
                &[],
            )
            .unwrap();

        let (stable, volatile) = store.get_context_split().unwrap();
        assert!(stable.contains("<memory_blocks_stable>"));
        assert!(stable.contains("User prefers concise answers."));
        assert!(volatile.contains("<memory_blocks>"));
        assert!(volatile.contains("Port Lethe to Rust."));
        assert!(volatile.contains("- 1 previous messages"));
        assert!(volatile.contains("- 1 total memories"));
        assert!(!stable.contains("<identity>"));

        let combined = store.get_context_for_prompt().unwrap();
        assert!(combined.contains("<memory_metadata>"));
    }
}
