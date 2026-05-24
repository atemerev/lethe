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

fn embedded_prompt(name: &str) -> Option<&'static str> {
    match name.trim_end_matches(".md") {
        "agent_instructions" => Some(include_str!("../../config/prompts/agent_instructions.md")),
        "agent_tools" => Some(include_str!("../../config/prompts/agent_tools.md")),
        "llm_summarize" => Some(include_str!("../../config/prompts/llm_summarize.md")),
        "llm_summarize_update" => {
            Some(include_str!("../../config/prompts/llm_summarize_update.md"))
        }
        "llm_summarize_system" => {
            Some(include_str!("../../config/prompts/llm_summarize_system.md"))
        }
        "notification_review" => Some(include_str!("../../config/prompts/notification_review.md")),
        "heartbeat_message" => Some(include_str!("../../config/prompts/heartbeat_message.md")),
        "heartbeat_message_full" => Some(include_str!(
            "../../config/prompts/heartbeat_message_full.md"
        )),
        "heartbeat_summarize" => Some(include_str!("../../config/prompts/heartbeat_summarize.md")),
        "llm_heartbeat_system" => {
            Some(include_str!("../../config/prompts/llm_heartbeat_system.md"))
        }
        "hippocampus_relevance" => Some(include_str!(
            "../../config/prompts/hippocampus_relevance.md"
        )),
        "hippocampus_analyze" => Some(include_str!("../../config/prompts/hippocampus_analyze.md")),
        "notes_extract" => Some(include_str!("../../config/prompts/notes_extract.md")),
        _ => None,
    }
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
