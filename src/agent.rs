use std::collections::HashSet;
use std::sync::{Arc, Mutex, RwLock};

use anyhow::anyhow;
use chrono::Local;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use thiserror::Error;

mod tool_context;
mod tool_loop;
use tool_context::recent_tool_context_for_turn;
use tool_loop::{
    TurnExecutionContext, actor_turn_executor, complete_turn_with_tools_config_shared,
};
#[cfg(test)]
use tool_loop::{actor_turn_instruction, extract_image_views, image_view_message};

use crate::actor::background::{
    BackgroundResult, collect_user_notifications_from_events, queue_dmn_heartbeat,
};
use crate::actor::notification::NotificationGate;
use crate::actor::{ActorConfig, ActorRegistry, ActorRuntime};
use crate::config::Settings;
use crate::llm::prompts::PromptStore;
use crate::llm::response_format::normalize_message_envelope;
use crate::llm::{
    HistoricalToolCall, HistoricalToolResponse, LlmAttachment, LlmMessage, LlmRole, LlmRouter,
    LlmRouterConfig, PromptBuilder, dialect_for_model,
};
use crate::memory::message_metadata::MessageMetadata;
use crate::memory::messages::{MessageHistoryError, MessageRole, StoredMessage};
use crate::memory::recall::{Hippocampus, HippocampusConfig, HippocampusError};
use crate::scheduler::curator::{CuratorError, CuratorRunStats, MemoryCurator};
use crate::store::{MemoryStore, MemoryStoreError};
use crate::tools::registry::{
    ActorToolContext, SharedActorRegistry, ToolRuntime, requestable_tools_directory_for,
};
use crate::tools::shell::ShellTools;

#[derive(Debug, Error)]
pub enum AgentError {
    #[error(transparent)]
    MemoryStore(#[from] MemoryStoreError),
    #[error(transparent)]
    Messages(#[from] MessageHistoryError),
    #[error(transparent)]
    Hippocampus(#[from] HippocampusError),
    #[error(transparent)]
    Llm(#[from] anyhow::Error),
    #[error(transparent)]
    Json(#[from] serde_json::Error),
    #[error(transparent)]
    Curator(#[from] CuratorError),
}

pub type AgentResult<T> = Result<T, AgentError>;

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct AgentTurn {
    pub messages: Vec<LlmMessage>,
    pub recall: Option<String>,
    pub synthetic: bool,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct AgentOptions {
    pub use_hippocampus: bool,
    pub history_limit: usize,
}

impl Default for AgentOptions {
    fn default() -> Self {
        Self {
            use_hippocampus: true,
            history_limit: 20,
        }
    }
}

/// A single agent turn input. Build via [`TurnRequest::new`] and the
/// `with_*` setters; pass to [`Agent::chat_once`] or [`Agent::prepare_turn`].
#[derive(Clone, Debug, Default)]
pub struct TurnRequest {
    pub message: String,
    pub attachments: Vec<LlmAttachment>,
    pub metadata: Option<Value>,
    pub runtime: crate::tools::registry::ToolRuntime,
    pub options: AgentOptions,
}

impl TurnRequest {
    pub fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
            ..Self::default()
        }
    }

    pub fn with_attachments(mut self, attachments: Vec<LlmAttachment>) -> Self {
        self.attachments = attachments;
        self
    }

    pub fn with_metadata(mut self, metadata: Value) -> Self {
        self.metadata = Some(metadata);
        self
    }

    pub fn with_runtime(mut self, runtime: crate::tools::registry::ToolRuntime) -> Self {
        self.runtime = runtime;
        self
    }

    pub fn with_options(mut self, options: AgentOptions) -> Self {
        self.options = options;
        self
    }
}

pub struct Agent {
    settings: Settings,
    memory: Arc<MemoryStore>,
    prompts: PromptStore,
    router: Arc<RwLock<LlmRouter>>,
    shell: ShellTools,
    actor_registry: Option<SharedActorRegistry>,
    principal_actor_id: Option<String>,
    notification_gate: Mutex<NotificationGate>,
    processed_notification_events: Mutex<HashSet<String>>,
}

impl Agent {
    pub fn from_settings(settings: Settings) -> AgentResult<Self> {
        let memory = Arc::new(MemoryStore::from_settings(&settings)?);
        let prompts = PromptStore::new(&settings.paths.workspace_dir, &settings.paths.config_dir);
        let router = Arc::new(RwLock::new(LlmRouter::new(LlmRouterConfig::from_settings(
            &settings,
        ))));
        let shell = ShellTools::new(&settings.paths.workspace_dir);
        let (actor_registry, principal_actor_id) = if settings.background.actors_enabled {
            let mut registry = ActorRegistry::new();
            let principal_id = registry.spawn(
                ActorConfig::new("cortex", "Serve the user.").in_group("main"),
                None,
                true,
            );
            let runtime = ActorRuntime::new(registry);
            runtime
                .install_turn_executor(actor_turn_executor(
                    settings.clone(),
                    memory.clone(),
                    router.clone(),
                    shell.clone(),
                ))
                .map_err(|error| AgentError::Llm(anyhow!("actor runtime failed: {error}")))?;
            (Some(runtime), Some(principal_id))
        } else {
            (None, None)
        };
        Ok(Self {
            settings,
            memory,
            prompts,
            router,
            shell,
            actor_registry,
            principal_actor_id,
            notification_gate: Mutex::new(NotificationGate::new(15 * 60)),
            processed_notification_events: Mutex::new(HashSet::new()),
        })
    }

    pub fn memory(&self) -> &MemoryStore {
        self.memory.as_ref()
    }

    pub fn router_config(&self) -> AgentResult<LlmRouterConfig> {
        let router = self
            .router
            .read()
            .map_err(|error| AgentError::Llm(anyhow!("router lock poisoned: {error}")))?;
        Ok(router.config().clone())
    }

    pub fn reconfigure_models(
        &self,
        model: Option<&str>,
        aux_model: Option<&str>,
    ) -> AgentResult<serde_json::Value> {
        let mut router = self
            .router
            .write()
            .map_err(|error| AgentError::Llm(anyhow!("router lock poisoned: {error}")))?;
        let mut config = router.config().clone();
        let mut changed = serde_json::Map::new();

        if let Some(model) = model.map(str::trim).filter(|value| !value.is_empty())
            && model != config.model
        {
            changed.insert(
                "model".to_string(),
                json!({"old": config.model.clone(), "new": model}),
            );
            config.model = model.to_string();
        }
        if let Some(aux_model) = aux_model.map(str::trim).filter(|value| !value.is_empty())
            && aux_model != config.aux_model
        {
            changed.insert(
                "model_aux".to_string(),
                json!({"old": config.aux_model.clone(), "new": aux_model}),
            );
            config.aux_model = aux_model.to_string();
        }

        if !changed.is_empty() {
            *router = LlmRouter::new(config);
        }
        Ok(serde_json::Value::Object(changed))
    }

    /// Assemble the LLM messages for a single turn without calling the model.
    pub async fn prepare_turn(&self, req: &TurnRequest) -> AgentResult<AgentTurn> {
        let mut turn = prepare_turn(
            &self.settings,
            self.memory.as_ref(),
            &self.prompts,
            &req.message,
            req.attachments.clone(),
            req.metadata.as_ref(),
            &req.options,
        )?;
        let actor_context = self.actor_context_for_prompt_async().await?;
        // Actor context and the requestable directory are per-turn volatile —
        // they belong on the volatile system message so they don't invalidate
        // the stable cache prefix.
        if let Some(context) = actor_context
            && let Some(system) = volatile_system_message_mut(&mut turn.messages)
        {
            system.content.push_str("\n\n<actor_context>\n");
            system.content.push_str(&context);
            system.content.push_str("\n</actor_context>");
        }
        let directory = self.requestable_tools_directory_async(req).await?;
        if !directory.is_empty()
            && let Some(system) = volatile_system_message_mut(&mut turn.messages)
        {
            system.content.push_str("\n\n");
            system.content.push_str(&directory);
        }
        Ok(turn)
    }

    async fn requestable_tools_directory_async(
        &self,
        req: &TurnRequest,
    ) -> AgentResult<String> {
        if let (Some(registry), Some(actor_id)) =
            (&self.actor_registry, &self.principal_actor_id)
        {
            return registry
                .build_requestable_directory(actor_id)
                .await
                .map_err(|error| {
                    AgentError::Llm(anyhow!("requestable directory failed: {error}"))
                });
        }
        let runtime = self.with_actor_runtime(req.runtime.clone());
        let body = requestable_tools_directory_for(&runtime);
        if body.is_empty() {
            return Ok(String::new());
        }
        Ok(format!(
            "<available_on_request>\nTools below are NOT loaded. Call request_tool(name=...) to enable one for this turn.\n{body}\n</available_on_request>"
        ))
    }

    /// Run one full turn: prepare messages, call the model with tool support,
    /// persist user/assistant history, and return the final assistant response.
    pub async fn chat_once(&self, req: TurnRequest) -> AgentResult<String> {
        let turn = self.prepare_turn(&req).await?;
        let TurnRequest {
            message,
            metadata,
            runtime,
            ..
        } = req;
        if !turn.synthetic {
            self.memory
                .messages
                .add(MessageRole::User, &message, metadata)?;
        }
        let runtime = self.with_actor_runtime(runtime);
        let response = self
            .complete_turn_with_tools(turn.messages, runtime, !turn.synthetic)
            .await?;
        if !turn.synthetic {
            let history_content = assistant_history_content(&response);
            self.memory
                .messages
                .add(MessageRole::Assistant, &history_content, None)?;
        }
        if self.settings.background.curator_enabled {
            let _ = self.run_curator_once(false);
        }
        Ok(response)
    }

    pub fn actor_registry(&self) -> Option<SharedActorRegistry> {
        self.actor_registry.clone()
    }

    pub fn principal_actor_id(&self) -> Option<&str> {
        self.principal_actor_id.as_deref()
    }

    pub fn run_curator_once(&self, force: bool) -> AgentResult<CuratorRunStats> {
        let curator = MemoryCurator::new(self.settings.paths.memory_dir.join("curator_state.json"));
        Ok(curator.run(self.memory.as_ref(), force)?)
    }

    pub async fn process_background_heartbeat(
        &self,
        heartbeat_message: &str,
        reminders: &str,
    ) -> AgentResult<BackgroundResult> {
        let mut result = self
            .queue_background_heartbeat(heartbeat_message, reminders)
            .await?;
        if let Some(registry) = self.actor_registry.clone() {
            result.notifications = {
                let events = registry
                    .user_notification_events(50)
                    .await
                    .map_err(|error| {
                        AgentError::Llm(anyhow!("notification query failed: {error}"))
                    })?;
                let mut gate = self.notification_gate.lock().map_err(|error| {
                    AgentError::Llm(anyhow!("notification gate lock poisoned: {error}"))
                })?;
                let mut processed = self.processed_notification_events.lock().map_err(|error| {
                    AgentError::Llm(anyhow!("notification event lock poisoned: {error}"))
                })?;
                collect_user_notifications_from_events(events, &mut gate, &mut processed)
            };
        }
        Ok(result)
    }

    pub async fn process_background_heartbeat_quiet(
        &self,
        heartbeat_message: &str,
        reminders: &str,
    ) -> AgentResult<BackgroundResult> {
        self.queue_background_heartbeat(heartbeat_message, reminders)
            .await
    }

    async fn queue_background_heartbeat(
        &self,
        heartbeat_message: &str,
        reminders: &str,
    ) -> AgentResult<BackgroundResult> {
        let mut result = BackgroundResult::default();
        if let (Some(registry), Some(principal_id)) =
            (self.actor_registry.clone(), self.principal_actor_id.clone())
        {
            let dmn_actor_id =
                queue_dmn_heartbeat(&registry, &principal_id, heartbeat_message, reminders)
                    .await
                    .map_err(|error| AgentError::Llm(anyhow!("dmn queue failed: {error}")))?;
            result.dmn_actor_id = Some(dmn_actor_id);
        }

        if self.settings.background.curator_enabled {
            result.curator = Some(self.run_curator_once(false)?);
        }
        Ok(result)
    }

    fn with_actor_runtime(&self, mut runtime: ToolRuntime) -> ToolRuntime {
        if runtime.actor.is_none()
            && let (Some(registry), Some(actor_id)) =
                (self.actor_registry.clone(), self.principal_actor_id.clone())
        {
            runtime.actor = Some(ActorToolContext {
                runtime: registry,
                actor_id,
                is_subagent: false,
            });
        }
        runtime
    }

    async fn actor_context_for_prompt_async(&self) -> AgentResult<Option<String>> {
        let (Some(registry), Some(actor_id)) = (&self.actor_registry, &self.principal_actor_id)
        else {
            return Ok(None);
        };
        let context = registry
            .build_system_prompt(actor_id)
            .await
            .map_err(|error| AgentError::Llm(anyhow!("actor context failed: {error}")))?;
        Ok(Some(context))
    }

    async fn complete_turn_with_tools(
        &self,
        messages: Vec<LlmMessage>,
        runtime: ToolRuntime,
        record_tool_messages: bool,
    ) -> AgentResult<String> {
        self.complete_turn_with_tools_config(messages, runtime, false, record_tool_messages)
            .await
    }

    async fn complete_turn_with_tools_config(
        &self,
        messages: Vec<LlmMessage>,
        runtime: ToolRuntime,
        use_aux: bool,
        record_tool_messages: bool,
    ) -> AgentResult<String> {
        complete_turn_with_tools_config_shared(
            TurnExecutionContext {
                settings: self.settings.clone(),
                memory: self.memory.clone(),
                router: self.router.clone(),
                shell: self.shell.clone(),
            },
            messages,
            runtime,
            use_aux,
            record_tool_messages,
        )
        .await
    }
}


pub fn prepare_turn(
    settings: &Settings,
    memory: &MemoryStore,
    prompts: &PromptStore,
    message: &str,
    attachments: Vec<LlmAttachment>,
    metadata: Option<&Value>,
    options: &AgentOptions,
) -> AgentResult<AgentTurn> {
    let synthetic = MessageMetadata::from_value(metadata).is_internal();
    let raw_recent = memory.messages.get_recent(history_fetch_limit(options))?;
    let recent = history_records_for_turn(raw_recent.clone(), options.history_limit);
    let recall = if options.use_hippocampus && !synthetic {
        Hippocampus::new(HippocampusConfig {
            enabled: settings.background.hippocampus_enabled,
            ..Default::default()
        })
        .recall(memory, message, &recent)?
    } else {
        None
    };

    let tool_context = recent_tool_context_for_turn(&raw_recent, settings);
    let parts =
        build_system_prompt(memory, prompts, recall.as_deref(), tool_context.as_deref())?;
    let dialect = dialect_for_model(&settings.llm.llm_model);
    let mut messages = parts.into_messages();
    apply_cache_markers(&mut messages, dialect.as_ref());
    messages.extend(history_to_llm_messages(recent));
    let user_message = if attachments.is_empty() {
        LlmMessage::user(message.to_string())
    } else {
        LlmMessage::user_with_attachments(message.to_string(), attachments)
    };
    messages.push(user_message);

    Ok(AgentTurn {
        messages,
        recall,
        synthetic,
    })
}

/// System prompt split into a long-stable head (identity, persona,
/// instructions, stable memory blocks) and a per-turn-volatile tail (volatile
/// blocks, clock, recall, tool history). Letting them be separate system
/// messages lets Anthropic's prompt cache land a breakpoint between them.
#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct SystemParts {
    pub stable: String,
    pub volatile: String,
}

impl SystemParts {
    pub fn into_messages(self) -> Vec<LlmMessage> {
        let mut out = Vec::new();
        if !self.stable.trim().is_empty() {
            out.push(LlmMessage::system(self.stable));
        }
        if !self.volatile.trim().is_empty() {
            out.push(LlmMessage::system(self.volatile));
        }
        out
    }

    /// Flatten back to a single string. Used by tests that still assert on the
    /// monolithic prompt shape.
    pub fn render_joined(&self) -> String {
        match (self.stable.trim().is_empty(), self.volatile.trim().is_empty()) {
            (true, true) => String::new(),
            (false, true) => self.stable.clone(),
            (true, false) => self.volatile.clone(),
            (false, false) => format!("{}\n\n{}", self.stable, self.volatile),
        }
    }
}

fn build_system_prompt(
    memory: &MemoryStore,
    prompts: &PromptStore,
    recall: Option<&str>,
    tool_context: Option<&str>,
) -> AgentResult<SystemParts> {
    let identity = memory
        .blocks
        .get("identity")
        .map_err(MemoryStoreError::Blocks)?
        .map(|block| block.value)
        .unwrap_or_default();
    let instructions = prompts.load("agent_instructions", "You are Lethe.").text;
    let (memory_stable, memory_volatile) = memory.get_context_split()?;
    let clock_block = crate::store::format_clock_block();

    let mut stable_builder = PromptBuilder::new();
    stable_builder
        .block("identity_block", identity)
        .raw(instructions)
        .raw(memory_stable);

    let mut volatile_builder = PromptBuilder::new();
    volatile_builder.raw(memory_volatile).raw(clock_block);

    if let Some(tool_context) = tool_context {
        volatile_builder.block_with(
            "runtime_context",
            [("source", "recent_tool_history")],
            tool_context,
        );
    }
    if let Some(recall) = recall.filter(|value| !value.trim().is_empty()) {
        let timestamp = Local::now().format("%a %Y-%m-%d %H:%M:%S %Z").to_string();
        let body = format!(
            "<recall_block source=\"hippocampus\">\n{}\n</recall_block>",
            recall.trim()
        );
        volatile_builder.block_with(
            "runtime_context",
            [("source", "hippocampus"), ("timestamp", timestamp.as_str())],
            body,
        );
    }

    Ok(SystemParts {
        stable: stable_builder.render(),
        volatile: volatile_builder.render(),
    })
}

fn history_fetch_limit(options: &AgentOptions) -> usize {
    let visible_limit = if options.history_limit == 0 {
        AgentOptions::default().history_limit
    } else {
        options.history_limit
    };
    visible_limit.saturating_mul(8).max(100)
}

fn history_records_for_turn(recent: Vec<StoredMessage>, limit: usize) -> Vec<StoredMessage> {
    let mut history = Vec::new();
    let mut inside_internal_turn = false;
    for message in recent {
        let internal = MessageMetadata::from_value(Some(&message.metadata)).is_internal();
        if message.role.is_user() {
            inside_internal_turn = internal;
            if inside_internal_turn {
                continue;
            }
        } else if inside_internal_turn || internal {
            continue;
        }

        if is_visible_history_record(&message) {
            history.push(message);
        }
    }

    drop_history_before_first_user(&mut history);

    let visible_limit = if limit == 0 {
        AgentOptions::default().history_limit
    } else {
        limit
    };
    if history.len() > visible_limit {
        let start = history.len() - visible_limit;
        history.drain(0..start);
        drop_history_before_first_user(&mut history);
    }

    history
}


fn is_visible_history_record(message: &StoredMessage) -> bool {
    if MessageMetadata::from_value(Some(&message.metadata)).is_internal() {
        return false;
    }
    match message.role {
        MessageRole::User | MessageRole::Assistant | MessageRole::Tool => {
            // Tool results legitimately carry a tool_call_id in metadata
            // instead of inline text; assistant messages with tool_calls may
            // also have empty content. Both stay; the pairing pass filters
            // orphans later.
            !message.content.trim().is_empty()
                || MessageMetadata::from_value(Some(&message.metadata)).has_tool_calls()
                || message.metadata.get("tool_call_id").is_some()
        }
        _ => false,
    }
}

fn drop_history_before_first_user(history: &mut Vec<StoredMessage>) {
    let Some(first_user) = history.iter().position(|message| message.role.is_user()) else {
        history.clear();
        return;
    };
    if first_user > 0 {
        history.drain(0..first_user);
    }
}

/// Convert a slice of stored messages into the LLM message stream, preserving
/// assistant_tool_calls ↔ tool_response pairing so the wire format stays
/// valid (Anthropic enforces this; OpenAI is more lenient but still expects
/// matching ids). Orphans on either side are dropped.
fn history_to_llm_messages(history: Vec<StoredMessage>) -> Vec<LlmMessage> {
    let mut out = Vec::new();
    let mut iter = history.into_iter().peekable();

    while let Some(message) = iter.next() {
        match message.role {
            MessageRole::User if !message.content.trim().is_empty() => {
                out.push(LlmMessage::user(history_content_with_timestamp(&message)));
            }
            MessageRole::Assistant => {
                let calls = extract_historical_tool_calls(&message.metadata);
                let intended_tool_calls =
                    MessageMetadata::from_value(Some(&message.metadata)).has_tool_calls();
                if calls.is_empty() {
                    // The model was reported to have made tool calls but the
                    // payload is missing call_ids — we can't reconstruct a
                    // valid pair, so drop the chatter entirely instead of
                    // surfacing it as plain narration.
                    if intended_tool_calls {
                        continue;
                    }
                    let content = history_content_with_timestamp(&message);
                    if !content.trim().is_empty() {
                        out.push(LlmMessage::assistant(content));
                    }
                    continue;
                }

                // Collect the tool results that should follow this assistant
                // message. Anthropic requires every tool_use_id to have a
                // matching tool_result in the very next user message; we
                // greedily consume Tool-role messages while they match a
                // pending call_id.
                let expected: std::collections::HashSet<String> =
                    calls.iter().map(|call| call.call_id.clone()).collect();
                let mut responses: Vec<HistoricalToolResponse> = Vec::new();
                let mut seen: std::collections::HashSet<String> =
                    std::collections::HashSet::new();
                while let Some(next) = iter.peek() {
                    if !matches!(next.role, MessageRole::Tool) {
                        break;
                    }
                    let Some(call_id) =
                        next.metadata.get("tool_call_id").and_then(Value::as_str)
                    else {
                        // Tool message without a tool_call_id — orphan, skip.
                        iter.next();
                        continue;
                    };
                    if !expected.contains(call_id) {
                        // Belongs to a different call group; stop consuming.
                        break;
                    }
                    let call_id = call_id.to_string();
                    let tool_msg = iter.next().expect("peeked tool message");
                    if seen.insert(call_id.clone()) {
                        responses.push(HistoricalToolResponse {
                            call_id,
                            content: tool_msg.content,
                        });
                    }
                }

                // Drop the whole pair if any tool_use_id is missing its
                // response — Anthropic 400s on a mismatched id list.
                if seen.len() != expected.len() {
                    continue;
                }
                let text = history_content_with_timestamp(&message);
                out.push(LlmMessage::assistant_with_tool_calls(text, calls));
                out.push(LlmMessage::tool_results(responses));
            }
            // Orphaned tool result (no preceding assistant tool_call). Skip.
            MessageRole::Tool => continue,
            _ => continue,
        }
    }

    out
}

fn extract_historical_tool_calls(metadata: &Value) -> Vec<HistoricalToolCall> {
    metadata
        .get("tool_calls")
        .and_then(Value::as_array)
        .map(|calls| {
            calls
                .iter()
                .filter_map(|call| {
                    let call_id = call.get("call_id").or_else(|| call.get("id"))?.as_str()?;
                    let fn_name = call
                        .get("fn_name")
                        .or_else(|| call.get("function").and_then(|f| f.get("name")))?
                        .as_str()?;
                    let fn_arguments = call
                        .get("fn_arguments")
                        .cloned()
                        .or_else(|| {
                            call.get("function")
                                .and_then(|f| f.get("arguments"))
                                .and_then(|args| args.as_str())
                                .and_then(|raw| serde_json::from_str::<Value>(raw).ok())
                        })
                        .unwrap_or(Value::Object(serde_json::Map::new()));
                    Some(HistoricalToolCall {
                        call_id: call_id.to_string(),
                        fn_name: fn_name.to_string(),
                        fn_arguments,
                    })
                })
                .collect()
        })
        .unwrap_or_default()
}

fn assistant_history_content(response: &str) -> String {
    normalize_message_envelope(response).unwrap_or_else(|| response.to_string())
}

/// Return the last system message (the volatile half of the split prompt) so
/// per-turn additions (actor_context, directory) don't bust the stable cache.
fn volatile_system_message_mut(messages: &mut [LlmMessage]) -> Option<&mut LlmMessage> {
    messages
        .iter_mut()
        .rev()
        .find(|message| message.role == LlmRole::System)
}

/// Attach the dialect's cache hints to the system messages: the first system
/// message is the stable head, the last is the volatile tail. With two
/// messages this lands two cache breakpoints; with one (e.g. when only the
/// stable half exists) it lands one.
fn apply_cache_markers(messages: &mut [LlmMessage], dialect: &dyn crate::llm::PromptDialect) {
    let system_indices: Vec<usize> = messages
        .iter()
        .enumerate()
        .filter(|(_, message)| message.role == LlmRole::System)
        .map(|(index, _)| index)
        .collect();
    if let Some(&first) = system_indices.first()
        && let Some(hint) = dialect.cache_marker_for_stable()
    {
        messages[first].cache_control = Some(hint);
    }
    if let Some(&last) = system_indices.last()
        && system_indices.len() > 1
        && let Some(hint) = dialect.cache_marker_for_volatile()
    {
        messages[last].cache_control = Some(hint);
    }
}

/// Threshold above which a history message is prefixed with its timestamp.
/// Short bursty exchanges (sub-5-minute gaps) stay timestamp-free so the
/// model sees clean dialogue; longer gaps surface the time so the model can
/// reason about staleness without calling conversation_search.
const HISTORY_TIMESTAMP_THRESHOLD_SECONDS: i64 = 300;

fn history_content_with_timestamp(message: &StoredMessage) -> String {
    let Ok(created) = chrono::DateTime::parse_from_rfc3339(&message.created_at) else {
        return message.content.clone();
    };
    let created = created.with_timezone(&chrono::Utc);
    let age = chrono::Utc::now().signed_duration_since(created).num_seconds();
    if age < HISTORY_TIMESTAMP_THRESHOLD_SECONDS {
        return message.content.clone();
    }
    let local = created.with_timezone(&Local);
    let stamp = local.format("%a %Y-%m-%d %H:%M %Z").to_string();
    format!("[{stamp}] {}", message.content)
}

#[cfg(test)]
mod tests {
    use tempfile::tempdir;

    use super::*;
    use crate::actor::{ActorRunSpec, ModelTier};
    use crate::config::Settings;
    use crate::llm::LlmRole;
    use crate::memory::message_metadata::{MessageKind, MessageVisibility, metadata_value};

    fn settings(root: &std::path::Path) -> Settings {
        crate::config::test_settings(root)
    }

    /// Concatenate every System-role message into one string. The prompt is
    /// now split into (stable, volatile) parts, so tests checking individual
    /// fragments shouldn't care which half they landed in.
    fn system_content(messages: &[LlmMessage]) -> String {
        messages
            .iter()
            .filter(|message| message.role == LlmRole::System)
            .map(|message| message.content.as_str())
            .collect::<Vec<_>>()
            .join("\n\n")
    }

    #[test]
    fn prepare_turn_includes_memory_context_history_and_recall() {
        let tmp = tempdir().unwrap();
        let settings = settings(tmp.path());
        let memory = MemoryStore::from_settings(&settings).unwrap();
        let prompts = PromptStore::new(&settings.paths.workspace_dir, &settings.paths.config_dir);

        memory
            .notes
            .create(
                "Graph Email",
                "Use MSAL for graph email.",
                &["skill".to_string()],
                None,
            )
            .unwrap();
        memory
            .messages
            .add(MessageRole::User, "previous graph question", None)
            .unwrap();

        let turn = prepare_turn(
            &settings,
            &memory,
            &prompts,
            "How do I use graph email?",
            Vec::new(),
            None,
            &AgentOptions::default(),
        )
        .unwrap();

        assert_eq!(turn.messages[0].role, LlmRole::System);
        let system = system_content(&turn.messages);
        assert!(system.contains("<identity_block>"));
        assert!(system.contains("<memory_metadata>"));
        assert!(system.contains("<runtime_context source=\"hippocampus\""));
        assert!(system.contains("Graph Email"));
        assert!(
            turn.messages
                .iter()
                .any(|message| message.content == "previous graph question")
        );
        assert_eq!(
            turn.messages.last().unwrap().content,
            "How do I use graph email?"
        );
    }

    #[test]
    fn prepare_turn_excludes_tool_loop_chatter_from_history() {
        let tmp = tempdir().unwrap();
        let settings = settings(tmp.path());
        let memory = MemoryStore::from_settings(&settings).unwrap();
        let prompts = PromptStore::new(&settings.paths.workspace_dir, &settings.paths.config_dir);

        memory
            .messages
            .add(
                MessageRole::Assistant,
                "I will inspect that now.",
                Some(json!({"tool_calls": [{"name": "bash"}]})),
            )
            .unwrap();
        memory
            .messages
            .add(MessageRole::Tool, "secret tool output", Some(json!({"name": "bash"})))
            .unwrap();
        memory
            .messages
            .add(MessageRole::User, "previous visible user request", None)
            .unwrap();
        memory
            .messages
            .add(MessageRole::Assistant, "previous visible answer", None)
            .unwrap();

        let turn = prepare_turn(
            &settings,
            &memory,
            &prompts,
            "follow-up",
            Vec::new(),
            None,
            &AgentOptions {
                use_hippocampus: false,
                ..Default::default()
            },
        )
        .unwrap();

        let contents = turn
            .messages
            .iter()
            .map(|message| message.content.as_str())
            .collect::<Vec<_>>();
        assert!(!contents.contains(&"I will inspect that now."));
        assert!(!contents.contains(&"secret tool output"));
        assert!(contents.contains(&"previous visible user request"));
        assert!(contents.contains(&"previous visible answer"));
        let system = system_content(&turn.messages);
        assert!(system.contains("<runtime_context source=\"recent_tool_history\">"));
        assert!(system.contains("<tool_call name=\"bash\""));
        assert!(system.contains("secret tool output"));
    }

    #[test]
    fn prepare_turn_preserves_paired_tool_calls_and_responses() {
        let tmp = tempdir().unwrap();
        let settings = settings(tmp.path());
        let memory = MemoryStore::from_settings(&settings).unwrap();
        let prompts = PromptStore::new(&settings.paths.workspace_dir, &settings.paths.config_dir);

        memory
            .messages
            .add(MessageRole::User, "what's in foo.txt?", None)
            .unwrap();
        memory
            .messages
            .add(
                MessageRole::Assistant,
                "reading it",
                Some(json!({
                    "tool_calls": [{
                        "call_id": "call-abc",
                        "fn_name": "read_file",
                        "fn_arguments": {"file_path": "foo.txt"},
                    }]
                })),
            )
            .unwrap();
        memory
            .messages
            .add(
                MessageRole::Tool,
                "file contents: hello",
                Some(json!({"tool_call_id": "call-abc", "name": "read_file"})),
            )
            .unwrap();
        memory
            .messages
            .add(MessageRole::Assistant, "it says hello", None)
            .unwrap();

        let turn = prepare_turn(
            &settings,
            &memory,
            &prompts,
            "ok thanks",
            Vec::new(),
            None,
            &AgentOptions {
                history_limit: 20,
                use_hippocampus: false,
            },
        )
        .unwrap();

        // 2 system (stable + volatile) + user + assistant_with_tool_calls
        // + tool_results + assistant + new user = 7.
        assert_eq!(turn.messages.len(), 7);
        let call_msg = turn
            .messages
            .iter()
            .find(|m| !m.tool_calls.is_empty())
            .expect("assistant with tool_calls");
        assert_eq!(call_msg.tool_calls.len(), 1);
        assert_eq!(call_msg.tool_calls[0].call_id, "call-abc");
        assert_eq!(call_msg.tool_calls[0].fn_name, "read_file");
        let result_msg = turn
            .messages
            .iter()
            .find(|m| !m.tool_responses.is_empty())
            .expect("tool results");
        assert_eq!(result_msg.tool_responses.len(), 1);
        assert_eq!(result_msg.tool_responses[0].call_id, "call-abc");
        assert_eq!(result_msg.tool_responses[0].content, "file contents: hello");
    }

    #[test]
    fn prepare_turn_drops_orphan_tool_call_pairs() {
        let tmp = tempdir().unwrap();
        let settings = settings(tmp.path());
        let memory = MemoryStore::from_settings(&settings).unwrap();
        let prompts = PromptStore::new(&settings.paths.workspace_dir, &settings.paths.config_dir);

        memory
            .messages
            .add(MessageRole::User, "do two things", None)
            .unwrap();
        // Assistant emitted two tool calls but only one matching result is
        // persisted — the whole pair must be dropped to avoid Anthropic 400s.
        memory
            .messages
            .add(
                MessageRole::Assistant,
                "running both",
                Some(json!({
                    "tool_calls": [
                        {"call_id": "c1", "fn_name": "read_file", "fn_arguments": {}},
                        {"call_id": "c2", "fn_name": "read_file", "fn_arguments": {}},
                    ]
                })),
            )
            .unwrap();
        memory
            .messages
            .add(
                MessageRole::Tool,
                "first result",
                Some(json!({"tool_call_id": "c1"})),
            )
            .unwrap();
        // No tool_result for c2 — orphan.

        let turn = prepare_turn(
            &settings,
            &memory,
            &prompts,
            "follow-up",
            Vec::new(),
            None,
            &AgentOptions {
                history_limit: 20,
                use_hippocampus: false,
            },
        )
        .unwrap();

        assert!(
            !turn
                .messages
                .iter()
                .any(|m| !m.tool_calls.is_empty() || !m.tool_responses.is_empty()),
            "orphan pair must be dropped entirely"
        );
        assert!(
            turn.messages
                .iter()
                .any(|m| m.content == "do two things")
        );
    }

    #[test]
    fn prepare_turn_excludes_internal_metadata_turns_from_history() {
        let tmp = tempdir().unwrap();
        let settings = settings(tmp.path());
        let memory = MemoryStore::from_settings(&settings).unwrap();
        let prompts = PromptStore::new(&settings.paths.workspace_dir, &settings.paths.config_dir);
        let internal = metadata_value(
            MessageVisibility::Internal,
            MessageKind::Heartbeat,
            "heartbeat",
        );

        memory
            .messages
            .add(MessageRole::User, "internal heartbeat prompt", Some(internal.clone()))
            .unwrap();
        memory
            .messages
            .add(MessageRole::Assistant, "internal heartbeat answer", None)
            .unwrap();
        memory
            .messages
            .add(MessageRole::User, "visible question", None)
            .unwrap();

        let turn = prepare_turn(
            &settings,
            &memory,
            &prompts,
            "follow-up",
            Vec::new(),
            None,
            &AgentOptions {
                use_hippocampus: false,
                ..Default::default()
            },
        )
        .unwrap();

        let contents = turn
            .messages
            .iter()
            .map(|message| message.content.as_str())
            .collect::<Vec<_>>();
        assert!(!contents.contains(&"internal heartbeat prompt"));
        assert!(!contents.contains(&"internal heartbeat answer"));
        assert!(contents.contains(&"visible question"));
    }

    #[test]
    fn assistant_history_content_normalizes_message_envelope() {
        let normalized = assistant_history_content(
            r#"{"messages":["doing pretty well","I have thoughts when you have a sec"]}"#,
        );

        assert_eq!(
            normalized,
            "doing pretty well\n\nI have thoughts when you have a sec"
        );
    }

    #[test]
    fn internal_metadata_turn_skips_recall() {
        let tmp = tempdir().unwrap();
        let settings = settings(tmp.path());
        let memory = MemoryStore::from_settings(&settings).unwrap();
        let prompts = PromptStore::new(&settings.paths.workspace_dir, &settings.paths.config_dir);
        let metadata = metadata_value(
            MessageVisibility::Internal,
            MessageKind::Heartbeat,
            "heartbeat",
        );

        let turn = prepare_turn(
            &settings,
            &memory,
            &prompts,
            "heartbeat prompt",
            Vec::new(),
            Some(&metadata),
            &AgentOptions::default(),
        )
        .unwrap();

        assert!(turn.synthetic);
        assert!(turn.recall.is_none());
        // The recall block has both an outer <runtime_context source="hippocampus"...>
        // attribute and an inner <recall_block source="hippocampus"> tag. The
        // outer attribute name can now appear in static instruction prose, so
        // we look for the inner tag which only appears when recall actually
        // renders.
        assert!(
            !turn.messages[0]
                .content
                .contains("<recall_block source=\"hippocampus\">")
        );
    }

    #[tokio::test]
    async fn agent_prepare_turn_includes_principal_actor_context_when_enabled() {
        let tmp = tempdir().unwrap();
        let settings = settings(tmp.path());
        let agent = Agent::from_settings(settings).unwrap();

        let turn = agent
            .prepare_turn(&TurnRequest::new("Please research this in parallel"))
            .await
            .unwrap();

        assert!(agent.actor_registry().is_some());
        assert!(agent.principal_actor_id().is_some());
        let system = system_content(&turn.messages);
        assert!(system.contains("<actor_context>"));
        assert!(system.contains("runtime role: cortex"));
        assert!(system.contains("<available_on_request>"));
    }

    #[tokio::test]
    async fn agent_prepare_turn_omits_actor_context_when_disabled() {
        let tmp = tempdir().unwrap();
        let mut settings = settings(tmp.path());
        settings.background.actors_enabled = false;
        let agent = Agent::from_settings(settings).unwrap();

        let turn = agent
            .prepare_turn(&TurnRequest::new("Handle this directly"))
            .await
            .unwrap();

        assert!(agent.actor_registry().is_none());
        assert!(!turn.messages[0].content.contains("<actor_context>"));
    }

    #[test]
    fn agent_reconfigures_router_models() {
        let tmp = tempdir().unwrap();
        let agent = Agent::from_settings(settings(tmp.path())).unwrap();

        let changed = agent
            .reconfigure_models(Some("main-next"), Some("aux-next"))
            .unwrap();
        let config = agent.router_config().unwrap();

        assert_eq!(config.model, "main-next");
        assert_eq!(config.aux_model, "aux-next");
        assert_eq!(changed["model"]["old"], "test-model");
        assert_eq!(changed["model_aux"]["new"], "aux-next");
    }

    #[test]
    fn image_view_payload_is_stripped_and_injected_as_binary_message() {
        let payload = json!({
            "status": "ok",
            "message": "Viewing image",
            "_image_view": {
                "path": "/tmp/image.png",
                "mime_type": "image/png",
                "data": "aGVsbG8=",
                "name": "image.png"
            }
        })
        .to_string();

        let (tool_response, images) = extract_image_views(payload);
        assert!(!tool_response.contains("_image_view"));
        assert_eq!(images.len(), 1);

        let message = image_view_message(images[0].clone());
        assert_eq!(message.content.parts().len(), 2);
        assert!(message.content.parts()[1].is_image());
    }

    #[test]
    fn actor_turn_instruction_tracks_first_turn_and_inbox() {
        let spec = ActorRunSpec {
            actor_id: "a1".to_string(),
            name: "worker".to_string(),
            system_prompt: "system".to_string(),
            turn_number: 1,
            max_turns: 3,
            model: ModelTier::Aux,
            has_pending_messages: true,
            requested_tools: vec![],
        };

        let first = actor_turn_instruction(&spec);
        assert!(first.contains("Begin your actor task"));
        assert!(first.contains("pending inbox"));
        assert!(first.contains("turn 1/3"));

        let later = actor_turn_instruction(&ActorRunSpec {
            turn_number: 2,
            has_pending_messages: false,
            ..spec
        });
        assert!(later.contains("Continue your actor task"));
        assert!(later.contains("send_message"));
        assert!(later.contains("turn 2/3"));
    }
}
