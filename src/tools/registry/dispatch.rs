use std::time::{Duration, Instant};

use serde::Deserialize;
use serde_json::Value;
use uuid::Uuid;

use crate::actor::{ActorState, ActorToolCommand};
use crate::tools::shell::DEFAULT_TIMEOUT_SECONDS;

use super::args::*;
use super::{SharedActorRegistry, ToolRegistry};

impl<'a> ToolRegistry<'a> {
    pub async fn execute_async(&self, name: &str, args: &Value) -> String {
        if name.trim() == "spawn_chain" {
            return self.execute_spawn_chain(args).await;
        }
        if let Some(dispatch) = self.actor_tool_command(name, args) {
            return match dispatch {
                Ok((runtime, command)) => runtime.execute_actor_tool(command).await,
                Err(message) => message,
            };
        }
        self.execute(name, args)
    }

    pub fn execute(&self, name: &str, args: &Value) -> String {
        let name = name.trim();
        if name == "spawn_chain" {
            return "Error: spawn_chain requires async tool execution.".to_string();
        }
        if let Some(dispatch) = self.actor_tool_command(name, args) {
            return match dispatch {
                Ok((runtime, command)) => runtime.execute_actor_tool_blocking(command),
                Err(message) => message,
            };
        }
        match name {
            "read_file" => self.files.read_file(
                &string_arg(args, "file_path"),
                usize_arg(args, "offset", 0),
                usize_arg(args, "limit", 0),
            ),
            "write_file" => self
                .files
                .write_file(&string_arg(args, "file_path"), &string_arg(args, "content")),
            "edit_file" => self.files.edit_file(
                &string_arg(args, "file_path"),
                &string_arg(args, "old_string"),
                &string_arg(args, "new_string"),
                bool_arg(args, "replace_all", false),
            ),
            "list_directory" => self.files.list_directory(
                &string_arg_default(args, "path", "."),
                bool_arg(args, "show_hidden", false),
            ),
            "glob_search" => self.files.glob_search(
                &string_arg(args, "pattern"),
                &string_arg_default(args, "path", "."),
            ),
            "grep_search" => self.files.grep_search(
                &string_arg(args, "pattern"),
                &string_arg_default(args, "path", "."),
                &string_arg_default(args, "file_pattern", "*"),
            ),
            "view_image" => self.image.view_image(
                &string_arg(args, "file_path"),
                usize_arg(args, "max_size", 1568),
            ),
            "bash" => self.shell.bash(
                &string_arg(args, "command"),
                u64_arg(args, "timeout", DEFAULT_TIMEOUT_SECONDS),
                bool_arg(args, "run_in_background", false),
                bool_arg(args, "use_pty", false),
            ),
            "bash_output" => self.shell.bash_output(
                &string_arg(args, "shell_id"),
                &string_arg_default(args, "filter_pattern", ""),
                usize_arg(args, "last_lines", 0),
            ),
            "kill_bash" => self.shell.kill_bash(&string_arg(args, "shell_id")),
            "get_terminal_screen" => self.shell.get_terminal_screen(&string_arg(args, "shell_id")),
            "send_terminal_input" => self.shell.send_terminal_input(
                &string_arg(args, "shell_id"),
                &string_arg(args, "text"),
                bool_arg(args, "send_enter", true),
            ),
            "get_environment_info" => self.shell.get_environment_info(),
            "check_command_exists" => self
                .shell
                .check_command_exists(&string_arg(args, "command_name")),
            "memory_read" => match self.memory.blocks.get(&string_arg(args, "label")) {
                Ok(Some(block)) => serde_json::to_string_pretty(&block)
                    .unwrap_or_else(|error| format!("Error: {error}")),
                Ok(None) => format!("Block '{}' not found", string_arg(args, "label")),
                Err(error) => format!("Error: {error}"),
            },
            "memory_list" => {
                match self
                    .memory
                    .blocks
                    .list_blocks(bool_arg(args, "include_hidden", false))
                {
                    Ok(blocks) => serde_json::to_string_pretty(&blocks)
                        .unwrap_or_else(|error| format!("Error: {error}")),
                    Err(error) => format!("Error: {error}"),
                }
            }
            "memory_update" => match self.memory.blocks.update(
                &string_arg(args, "label"),
                Some(&string_arg(args, "value")),
                None,
            ) {
                Ok(true) => format!("Updated block '{}'", string_arg(args, "label")),
                Ok(false) => format!("Block '{}' not found", string_arg(args, "label")),
                Err(error) => format!("Error: {error}"),
            },
            "memory_append" => match self
                .memory
                .blocks
                .append(&string_arg(args, "label"), &string_arg(args, "text"))
            {
                Ok(true) => format!("Appended to block '{}'", string_arg(args, "label")),
                Ok(false) => format!("Block '{}' not found", string_arg(args, "label")),
                Err(error) => format!("Error: {error}"),
            },
            "archival_search" => match self.memory.search_archival(
                &string_arg(args, "query"),
                usize_arg(args, "limit", 10),
                None,
            ) {
                Ok(entries) => crate::archival::ArchivalMemory::format_entries(&entries),
                Err(error) => format!("Error: {error}"),
            },
            "archival_insert" => {
                match self
                    .memory
                    .archival
                    .add(&string_arg(args, "text"), None, &[])
                {
                    Ok(id) => format!("Stored in archival memory (id: {id})"),
                    Err(error) => format!("Error: {error}"),
                }
            }
            "conversation_search" => {
                let role = string_arg_default(args, "role", "");
                let result = if role.trim().is_empty() {
                    self.memory.search_messages(
                        &string_arg(args, "query"),
                        usize_arg(args, "limit", 10),
                        None,
                    )
                } else {
                    self.memory.search_messages(
                        &string_arg(args, "query"),
                        usize_arg(args, "limit", 10),
                        Some(&role),
                    )
                };
                match result {
                    Ok(messages) => crate::messages::MessageHistory::format_messages(&messages),
                    Err(error) => format!("Error: {error}"),
                }
            }
            "note_search" => {
                let tags = string_vec_arg(args, "tags");
                match self.memory.search_notes(
                    &string_arg(args, "query"),
                    optional_tags(&tags),
                    usize_arg(args, "limit", 5),
                ) {
                    Ok(results) => crate::notes::NoteStore::format_search(
                        &string_arg(args, "query"),
                        &tags,
                        &results,
                    ),
                    Err(error) => format!("Error: {error}"),
                }
            }
            "note_create" => {
                let tags = string_vec_arg(args, "tags");
                match self.memory.notes.create(
                    &string_arg(args, "title"),
                    &string_arg(args, "content"),
                    &tags,
                    None,
                ) {
                    Ok(path) => {
                        format!("Note saved: {} (tags: {})", path.display(), tags.join(", "))
                    }
                    Err(error) => format!("Error: {error}"),
                }
            }
            "note_list" => {
                let tags = string_vec_arg(args, "tags");
                match self.memory.notes.list_notes(optional_tags(&tags)) {
                    Ok(notes) => crate::notes::NoteStore::format_list(&notes),
                    Err(error) => format!("Error: {error}"),
                }
            }
            "todo_create" => {
                use crate::todos::{NewTodo, TodoManager, TodoPriority};
                let priority = TodoPriority::parse(&string_arg_default(args, "priority", "normal"))
                    .unwrap_or_default();
                match TodoManager::open(self.memory.db_path()) {
                    Ok(manager) => match manager.create(NewTodo {
                        title: string_arg(args, "title"),
                        description: nonempty_string(args, "description"),
                        priority,
                        due_date: nonempty_string(args, "due_date"),
                        tags: vec![],
                        source: Some("agent_tool".to_string()),
                    }) {
                        Ok(id) => format!("Created todo #{id}"),
                        Err(error) => format!("Error: {error}"),
                    },
                    Err(error) => format!("Error: {error}"),
                }
            }
            "todo_list" | "todo_complete" | "todo_update" | "todo_search" | "todo_remind_check"
            | "todo_reminded" => self.execute_todo_tool(name, args),
            "web_search" => self.web.web_search(
                &string_arg(args, "query"),
                usize_arg(args, "num_results", 10),
                bool_arg(args, "include_text", false),
                &string_arg_default(args, "category", ""),
            ),
            "fetch_webpage" => self
                .web
                .fetch_webpage(&string_arg(args, "url"), usize_arg(args, "max_chars", 5000)),
            "browser_open" => self.browser.open(&string_arg(args, "url")),
            "browser_snapshot" => self.browser.snapshot(
                bool_arg(args, "interactive_only", true),
                bool_arg(args, "compact", true),
            ),
            "browser_click" => self.browser.click(&string_arg(args, "ref_or_selector")),
            "browser_fill" => self.browser.fill(
                &string_arg(args, "ref_or_selector"),
                &string_arg(args, "text"),
            ),
            "telegram_send_message" => match &self.runtime.telegram {
                Some(context) => context.send_message(
                    &string_arg(args, "text"),
                    &string_arg_default(args, "parse_mode", ""),
                ),
                None => match &self.runtime.client {
                    Some(context) => context.send_message(
                        &string_arg(args, "text"),
                        &string_arg_default(args, "parse_mode", ""),
                    ),
                    None => "Telegram/client context not set. This tool only works during active user transport processing.".to_string(),
                },
            },
            "telegram_send_file" => match &self.runtime.telegram {
                Some(context) => context.send_file(
                    &string_arg(args, "file_path_or_url"),
                    &string_arg_default(args, "caption", ""),
                    bool_arg(args, "as_document", false),
                ),
                None => match &self.runtime.client {
                    Some(context) => context.send_file(
                        &string_arg(args, "file_path_or_url"),
                        &string_arg_default(args, "caption", ""),
                        bool_arg(args, "as_document", false),
                    ),
                    None => "Telegram/client context not set. This tool only works during active user transport processing.".to_string(),
                },
            },
            "telegram_react" => match &self.runtime.telegram {
                Some(context) => context.react(
                    &string_arg_default(args, "emoji", "👍"),
                    i64_arg(args, "message_id", 0),
                ),
                None => match &self.runtime.client {
                    Some(context) => context.react(
                        &string_arg_default(args, "emoji", "👍"),
                        i64_arg(args, "message_id", 0),
                    ),
                    None => "Telegram/client context not set. This tool only works during active user transport processing.".to_string(),
                },
            },
            other => format!("Unknown tool: {other}"),
        }
    }

    fn actor_tool_command(
        &self,
        name: &str,
        args: &Value,
    ) -> Option<Result<(SharedActorRegistry, ActorToolCommand), String>> {
        let name = name.trim();
        let Some(context) = &self.runtime.actor else {
            return matches!(
                name,
                "send_message"
                    | "wait_for_response"
                    | "discover_actors"
                    | "discover_recently_finished"
                    | "spawn_actor"
                    | "ping_actor"
                    | "kill_actor"
                    | "update_task_state"
                    | "get_task_state"
                    | "terminate"
                    | "restart_self"
            )
            .then(|| {
                Err("Actor context not set. This tool only works inside an actor.".to_string())
            });
        };
        let actor_id = context.actor_id.clone();
        let command = match name {
            "send_message" => ActorToolCommand::SendMessage {
                actor_id,
                target_id: string_arg(args, "actor_id"),
                content: string_arg(args, "content"),
                reply_to: nonempty_string(args, "reply_to"),
                channel: string_arg_default(args, "channel", ""),
                kind: string_arg_default(args, "kind", ""),
            },
            "wait_for_response" => ActorToolCommand::WaitForResponse { actor_id },
            "discover_actors" => ActorToolCommand::DiscoverActors {
                actor_id,
                group: nonempty_string(args, "group"),
                include_terminated: bool_arg(args, "include_terminated", false),
            },
            "discover_recently_finished" => ActorToolCommand::DiscoverRecentlyFinished {
                actor_id,
                group: nonempty_string(args, "group"),
                limit: usize_arg(args, "limit", 5),
            },
            "spawn_actor" => ActorToolCommand::SpawnActor {
                actor_id,
                name: string_arg(args, "name"),
                goals: string_arg(args, "goals"),
                group: nonempty_string(args, "group"),
                tools: string_arg_default(args, "tools", ""),
                model: string_arg_default(args, "model", "aux"),
                max_turns: usize_arg(args, "max_turns", 20),
            },
            "ping_actor" => ActorToolCommand::PingActor {
                actor_id,
                target_id: string_arg(args, "actor_id"),
            },
            "kill_actor" => ActorToolCommand::KillActor {
                actor_id,
                target_id: string_arg(args, "actor_id"),
            },
            "update_task_state" => ActorToolCommand::UpdateTaskState {
                actor_id,
                state: string_arg(args, "state"),
                note: string_arg_default(args, "note", ""),
            },
            "get_task_state" => ActorToolCommand::GetTaskState { actor_id },
            "terminate" => ActorToolCommand::Terminate {
                actor_id,
                result: string_arg_default(args, "result", ""),
                outcome: string_arg_default(args, "outcome", "success"),
                files_touched: string_arg_default(args, "files_touched", ""),
                follow_up: string_arg_default(args, "follow_up", ""),
            },
            "restart_self" => ActorToolCommand::RestartSelf {
                actor_id,
                new_goals: string_arg(args, "new_goals"),
            },
            _ => return None,
        };
        Some(Ok((context.runtime.clone(), command)))
    }

    async fn execute_spawn_chain(&self, args: &Value) -> String {
        const MAX_CHAIN_STEPS: usize = 5;
        const STEP_WAIT_TIMEOUT: Duration = Duration::from_secs(60 * 60);
        const STEP_POLL_INTERVAL: Duration = Duration::from_secs(1);

        let Some(context) = &self.runtime.actor else {
            return "Actor context not set. This tool only works inside an actor.".to_string();
        };
        let steps_text = string_arg(args, "steps");
        let steps = match serde_json::from_str::<Vec<ChainStep>>(&steps_text) {
            Ok(steps) if !steps.is_empty() => steps,
            Ok(_) => {
                return "Error: steps must be a non-empty JSON array of {name, goals} objects."
                    .to_string();
            }
            Err(error) => {
                return format!("Error: steps must be valid JSON array. Parse error: {error}");
            }
        };
        if steps.len() > MAX_CHAIN_STEPS {
            return format!(
                "Error: max {MAX_CHAIN_STEPS} steps in a chain (got {}).",
                steps.len()
            );
        }

        let tools = string_arg_default(args, "tools", "");
        let model = string_arg_default(args, "model", "aux");
        let max_turns = usize_arg(args, "max_turns", 20).max(1);
        let chain_id = Uuid::new_v4().to_string()[..6].to_string();
        let mut previous_result = String::new();
        let mut summaries = Vec::new();

        for (index, step) in steps.into_iter().enumerate() {
            let base_name = step
                .name
                .as_deref()
                .map(str::trim)
                .filter(|name| !name.is_empty())
                .unwrap_or("chain-step");
            let name = format!("{base_name}-{}-{}", index + 1, chain_id);
            let goals = step.goals.trim();
            if goals.is_empty() {
                return format!("Error: step {} has no goals.", index + 1);
            }
            let goals = goals.replace("{previous}", &previous_result);
            let spawn_result = context
                .runtime
                .execute_actor_tool(ActorToolCommand::SpawnActor {
                    actor_id: context.actor_id.clone(),
                    name: name.clone(),
                    goals,
                    group: None,
                    tools: tools.clone(),
                    model: model.clone(),
                    max_turns,
                })
                .await;
            let Some(child_id) = extract_spawned_actor_id(&spawn_result) else {
                return format!(
                    "Chain stopped at step {} ({name}):\n{spawn_result}",
                    index + 1
                );
            };

            let deadline = Instant::now() + STEP_WAIT_TIMEOUT;
            let result = loop {
                if Instant::now() >= deadline {
                    return format!(
                        "Chain timed out at step {} ({name}) after {} seconds.",
                        index + 1,
                        STEP_WAIT_TIMEOUT.as_secs()
                    );
                }
                match context.runtime.actor_info(&child_id).await {
                    Some(info) if info.state == ActorState::Terminated => {
                        break info.result.unwrap_or_else(|| "No result".to_string());
                    }
                    Some(_) => {
                        tokio::time::sleep(STEP_POLL_INTERVAL).await;
                    }
                    None => {
                        return format!(
                            "Chain stopped at step {} ({name}): actor {child_id} disappeared.",
                            index + 1
                        );
                    }
                }
            };

            let failed = is_failed_chain_result(&result);
            summaries.push(format!(
                "Step {} ({name}): {}",
                index + 1,
                truncate_tool_text(&result, 200)
            ));
            if failed {
                return format!(
                    "Chain stopped at step {} ({name}):\n{}",
                    index + 1,
                    summaries.join("\n")
                );
            }
            previous_result = result;
        }

        format!(
            "Chain complete ({} steps):\n{}\n\nFinal result:\n{}",
            summaries.len(),
            summaries.join("\n"),
            truncate_tool_text(&previous_result, 1000)
        )
    }

    fn execute_todo_tool(&self, name: &str, args: &Value) -> String {
        use crate::todos::{TodoFilter, TodoManager, TodoPriority, TodoStatus, TodoUpdate};
        let manager = match TodoManager::open(self.memory.db_path()) {
            Ok(manager) => manager,
            Err(error) => return format!("Error: {error}"),
        };
        match name {
            "todo_list" => {
                let status =
                    nonempty_string(args, "status").and_then(|value| TodoStatus::parse(&value));
                let priority =
                    nonempty_string(args, "priority").and_then(|value| TodoPriority::parse(&value));
                match manager.list(TodoFilter {
                    status,
                    priority,
                    include_completed: bool_arg(args, "include_completed", false),
                    limit: usize_arg(args, "limit", 50),
                }) {
                    Ok(todos) => TodoManager::format_list(&todos),
                    Err(error) => format!("Error: {error}"),
                }
            }
            "todo_complete" => match manager.complete(i64_arg(args, "todo_id", 0)) {
                Ok(true) => format!("Completed todo #{}", i64_arg(args, "todo_id", 0)),
                Ok(false) => format!("Todo #{} not found", i64_arg(args, "todo_id", 0)),
                Err(error) => format!("Error: {error}"),
            },
            "todo_update" => {
                let status = match parse_optional_todo_status(args, "status") {
                    Ok(status) => status,
                    Err(error) => return error,
                };
                let priority = match parse_optional_todo_priority(args, "priority") {
                    Ok(priority) => priority,
                    Err(error) => return error,
                };
                let todo_id = i64_arg(args, "todo_id", 0);
                match manager.update(
                    todo_id,
                    TodoUpdate {
                        title: nonempty_string(args, "title"),
                        description: nonempty_string(args, "description"),
                        status,
                        priority,
                        due_date: nonempty_string(args, "due_date"),
                    },
                ) {
                    Ok(true) => format!("Updated todo #{todo_id}"),
                    Ok(false) => format!("Todo #{todo_id} not found or no changes supplied"),
                    Err(error) => format!("Error: {error}"),
                }
            }
            "todo_search" => {
                match manager.search(&string_arg(args, "query"), usize_arg(args, "limit", 20)) {
                    Ok(todos) => TodoManager::format_search(&string_arg(args, "query"), &todos),
                    Err(error) => format!("Error: {error}"),
                }
            }
            "todo_remind_check" => match manager.due_reminders() {
                Ok(todos) => TodoManager::format_due_reminders(&todos),
                Err(error) => format!("Error: {error}"),
            },
            "todo_reminded" => match manager.mark_reminded(i64_arg(args, "todo_id", 0)) {
                Ok(true) => format!("Marked todo #{} as reminded", i64_arg(args, "todo_id", 0)),
                Ok(false) => format!("Todo #{} not found", i64_arg(args, "todo_id", 0)),
                Err(error) => format!("Error: {error}"),
            },
            _ => unreachable!(),
        }
    }
}

#[derive(Debug, Deserialize)]
struct ChainStep {
    #[serde(default)]
    name: Option<String>,
    #[serde(default)]
    goals: String,
}

fn extract_spawned_actor_id(value: &str) -> Option<String> {
    let after_marker = value.split("(id=").nth(1)?;
    let actor_id = after_marker.split(',').next()?.trim();
    (!actor_id.is_empty()).then(|| actor_id.to_string())
}

fn is_failed_chain_result(value: &str) -> bool {
    let prefix = value
        .chars()
        .take(80)
        .collect::<String>()
        .to_ascii_lowercase();
    prefix.contains("error")
        || prefix.contains("failure")
        || prefix.contains("failed")
        || prefix.contains("[outcome: failure]")
}

fn truncate_tool_text(value: &str, limit: usize) -> String {
    let mut truncated = value.chars().take(limit).collect::<String>();
    if value.chars().count() > limit {
        truncated.push_str("...[truncated]");
    }
    truncated
}

fn parse_optional_todo_status(
    args: &Value,
    key: &str,
) -> Result<Option<crate::todos::TodoStatus>, String> {
    let Some(value) = nonempty_string(args, key) else {
        return Ok(None);
    };
    crate::todos::TodoStatus::parse(&value)
        .map(Some)
        .ok_or_else(|| format!("Error: invalid todo status '{value}'"))
}

fn parse_optional_todo_priority(
    args: &Value,
    key: &str,
) -> Result<Option<crate::todos::TodoPriority>, String> {
    let Some(value) = nonempty_string(args, key) else {
        return Ok(None);
    };
    crate::todos::TodoPriority::parse(&value)
        .map(Some)
        .ok_or_else(|| format!("Error: invalid todo priority '{value}'"))
}
