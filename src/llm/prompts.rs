use std::collections::HashMap;
use std::fs;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub enum PromptSource {
    Workspace(PathBuf),
    Config(PathBuf),
    Embedded,
    Fallback,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct PromptTemplate {
    pub name: String,
    pub source: PromptSource,
    pub text: String,
}

#[derive(Clone, Debug)]
pub struct PromptStore {
    workspace_dir: PathBuf,
    config_dir: PathBuf,
}

impl PromptStore {
    pub fn new(workspace_dir: impl Into<PathBuf>, config_dir: impl Into<PathBuf>) -> Self {
        Self {
            workspace_dir: workspace_dir.into(),
            config_dir: config_dir.into(),
        }
    }

    pub fn load(&self, name: &str, fallback: &str) -> PromptTemplate {
        let file_name = prompt_file_name(name);
        for candidate in self.candidate_paths(&file_name) {
            if let Ok(text) = fs::read_to_string(&candidate) {
                let trimmed = text.trim().to_string();
                if !trimmed.is_empty() {
                    let source = if candidate.starts_with(self.workspace_dir.join("prompts")) {
                        PromptSource::Workspace(candidate)
                    } else {
                        PromptSource::Config(candidate)
                    };
                    return PromptTemplate {
                        name: name.to_string(),
                        source,
                        text: trimmed,
                    };
                }
            }
        }

        if let Some(text) = embedded_prompt(name) {
            return PromptTemplate {
                name: name.to_string(),
                source: PromptSource::Embedded,
                text: text.trim().to_string(),
            };
        }

        PromptTemplate {
            name: name.to_string(),
            source: PromptSource::Fallback,
            text: fallback.to_string(),
        }
    }

    pub fn render(
        &self,
        name: &str,
        variables: &HashMap<String, String>,
        fallback: &str,
    ) -> PromptTemplate {
        let mut template = self.load(name, fallback);
        for (key, value) in variables {
            template.text = template.text.replace(&format!("{{{key}}}"), value);
        }
        template
    }

    fn candidate_paths(&self, file_name: &str) -> Vec<PathBuf> {
        vec![
            self.workspace_dir.join("prompts").join(file_name),
            self.config_dir.join("prompts").join(file_name),
            self.config_dir
                .join("workspace")
                .join("prompts")
                .join(file_name),
        ]
    }
}

fn prompt_file_name(name: &str) -> String {
    if Path::new(name).extension().is_some() {
        name.to_string()
    } else {
        format!("{name}.md")
    }
}

/// Prompt templates that ship with an embedded default AND are loaded through
/// [`PromptStore`]. A file at `<workspace>/prompts/<name>.md` (or
/// `<config_dir>/prompts/`) overrides each of these at load time — they are
/// exactly the set `lethe prompts export` writes out for editing. Prompts that
/// are `include_str!`'d directly into other modules are compiled in and are
/// not overridable, so they are intentionally absent here.
pub const EMBEDDED_PROMPTS: &[(&str, &str)] = &[
    (
        "agent_instructions",
        include_str!("../../config/prompts/agent_instructions.md"),
    ),
    (
        "llm_summarize",
        include_str!("../../config/prompts/llm_summarize.md"),
    ),
    (
        "llm_summarize_update",
        include_str!("../../config/prompts/llm_summarize_update.md"),
    ),
    (
        "llm_summarize_system",
        include_str!("../../config/prompts/llm_summarize_system.md"),
    ),
    (
        "notification_review",
        include_str!("../../config/prompts/notification_review.md"),
    ),
    (
        "heartbeat_message",
        include_str!("../../config/prompts/heartbeat_message.md"),
    ),
    (
        "heartbeat_message_full",
        include_str!("../../config/prompts/heartbeat_message_full.md"),
    ),
    (
        "heartbeat_summarize",
        include_str!("../../config/prompts/heartbeat_summarize.md"),
    ),
    (
        "llm_heartbeat_system",
        include_str!("../../config/prompts/llm_heartbeat_system.md"),
    ),
    (
        "hippocampus_relevance",
        include_str!("../../config/prompts/hippocampus_relevance.md"),
    ),
    (
        "hippocampus_analyze",
        include_str!("../../config/prompts/hippocampus_analyze.md"),
    ),
    (
        "notes_extract",
        include_str!("../../config/prompts/notes_extract.md"),
    ),
    (
        "tool_loop_wrap_up",
        include_str!("../../config/prompts/tool_loop_wrap_up.md"),
    ),
    (
        "active_tasks",
        include_str!("../../config/prompts/active_tasks.md"),
    ),
    (
        "heartbeat_open_work",
        include_str!("../../config/prompts/heartbeat_open_work.md"),
    ),
    (
        "actor_previous_turn",
        include_str!("../../config/prompts/actor_previous_turn.md"),
    ),
    (
        "actor_restart_notice",
        include_str!("../../config/prompts/actor_restart_notice.md"),
    ),
    (
        "actor_max_turns_handoff",
        include_str!("../../config/prompts/actor_max_turns_handoff.md"),
    ),
];

/// The overridable prompt templates as `(name, embedded_text)` pairs.
pub fn embedded_prompts() -> &'static [(&'static str, &'static str)] {
    EMBEDDED_PROMPTS
}

fn embedded_prompt(name: &str) -> Option<&'static str> {
    let key = name.trim_end_matches(".md");
    EMBEDDED_PROMPTS
        .iter()
        .find(|(n, _)| *n == key)
        .map(|(_, text)| *text)
}

#[cfg(test)]
mod tests {
    use std::fs;

    use tempfile::tempdir;

    use super::*;

    #[test]
    fn workspace_prompt_wins_over_config_prompt() {
        let tmp = tempdir().unwrap();
        let workspace = tmp.path().join("workspace");
        let config = tmp.path().join("config");
        fs::create_dir_all(workspace.join("prompts")).unwrap();
        fs::create_dir_all(config.join("prompts")).unwrap();
        fs::write(workspace.join("prompts/example.md"), "workspace").unwrap();
        fs::write(config.join("prompts/example.md"), "config").unwrap();

        let store = PromptStore::new(&workspace, &config);
        let prompt = store.load("example", "fallback");

        assert_eq!(prompt.text, "workspace");
        assert!(matches!(prompt.source, PromptSource::Workspace(_)));
    }

    #[test]
    fn embedded_prompt_allows_single_binary_startup() {
        let tmp = tempdir().unwrap();
        let store = PromptStore::new(tmp.path().join("workspace"), tmp.path().join("config"));
        let prompt = store.load("agent_instructions", "");

        assert!(prompt.text.contains("<communication_style>"));
        assert_eq!(prompt.source, PromptSource::Embedded);
    }

    #[test]
    fn render_replaces_brace_format_tokens() {
        let tmp = tempdir().unwrap();
        let workspace = tmp.path().join("workspace");
        fs::create_dir_all(workspace.join("prompts")).unwrap();
        fs::write(workspace.join("prompts/greeting.md"), "hello {name}").unwrap();

        let mut variables = HashMap::new();
        variables.insert("name".to_string(), "lethe".to_string());
        let store = PromptStore::new(&workspace, tmp.path().join("config"));
        let prompt = store.render("greeting", &variables, "");

        assert_eq!(prompt.text, "hello lethe");
    }
}
