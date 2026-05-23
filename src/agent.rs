use std::collections::{HashMap, HashSet};
use std::future::Future;
use std::sync::{Arc, Mutex, RwLock};

use anyhow::anyhow;
use chrono::Local;
use genai::chat::{ChatMessage, ContentPart, MessageContent, ToolCall, ToolResponse};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use thiserror::Error;

use crate::actor::{
    ActorConfig, ActorError, ActorRegistry, ActorRunSpec, ActorRuntime, ActorTurnExecutor,
    ModelTier,
};
use crate::background::{
    BackgroundResult, collect_user_notifications_from_events, queue_dmn_heartbeat,
};
use crate::config::Settings;
use crate::curator::{CuratorError, CuratorRunStats, MemoryCurator};
use crate::hippocampus::{Hippocampus, HippocampusConfig, HippocampusError};
use crate::llm::{
    LlmAttachment, LlmMessage, LlmRole, LlmRouter, LlmRouterConfig, build_chat_request,
};
use crate::message_metadata::MessageMetadata;
use crate::messages::{MessageHistoryError, StoredMessage};
use crate::notification::NotificationGate;
use crate::prompts::PromptStore;
use crate::response_format::normalize_message_envelope;
use crate::store::{MemoryStore, MemoryStoreError};
use crate::telegram::TelegramClient;
use crate::tools::registry::{ActorToolContext, SharedActorRegistry, ToolRegistry, ToolRuntime};
use crate::tools::shell::ShellTools;

const MAX_TOOL_ITERATIONS: usize = 8;
const TELEGRAM_TOOL_TYPING_REFRESH_SECONDS: u64 = 3;
const RECENT_TOOL_CONTEXT_GROUPS: usize = 2;
const OLD_TOOL_RESULT_PREVIEW_LINES: usize = 5;
const OLD_TOOL_RESULT_PREVIEW_CHARS: usize = 2_000;
const TOOL_CONTEXT_MIN_CHARS: usize = 64 * 1024;
const TOOL_CONTEXT_MAX_CHARS: usize = 400_000;
const TOOL_CONTEXT_SHARE_NUMERATOR: usize = 3;
const TOOL_CONTEXT_SHARE_DENOMINATOR: usize = 10;
const SEARCH_RESULT_SKIP_TOOLS: &[&str] = &["conversation_search", "archival_search"];
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

#[derive(Clone)]
struct TurnExecutionContext {
    settings: Settings,
    memory: Arc<MemoryStore>,
    router: Arc<RwLock<LlmRouter>>,
    shell: ShellTools,
}

impl Agent {
    pub fn from_settings(settings: Settings) -> AgentResult<Self> {
        let memory = Arc::new(MemoryStore::from_settings(&settings)?);
        let prompts = PromptStore::new(&settings.workspace_dir, &settings.config_dir);
        let router = Arc::new(RwLock::new(LlmRouter::new(LlmRouterConfig::from_settings(
            &settings,
        ))));
        let shell = ShellTools::new(&settings.workspace_dir);
        let (actor_registry, principal_actor_id) = if settings.actors_enabled {
            let mut registry = ActorRegistry::new();
            let principal_id = registry.spawn(
                ActorConfig::new(
                    "cortex",
                    "Serve the user. Handle quick tasks directly. Delegate long or complex tasks to subagents.",
                )
                .in_group("main"),
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

    pub fn prepare_turn(&self, message: &str, options: &AgentOptions) -> AgentResult<AgentTurn> {
        self.prepare_turn_with_attachments(message, Vec::new(), options)
    }

    pub async fn prepare_turn_async(
        &self,
        message: &str,
        options: &AgentOptions,
    ) -> AgentResult<AgentTurn> {
        self.prepare_turn_with_attachments_async(message, Vec::new(), options)
            .await
    }

    pub fn prepare_turn_with_attachments(
        &self,
        message: &str,
        attachments: Vec<LlmAttachment>,
        options: &AgentOptions,
    ) -> AgentResult<AgentTurn> {
        self.prepare_turn_with_attachments_metadata(message, attachments, None, options)
    }

    pub fn prepare_turn_with_attachments_metadata(
        &self,
        message: &str,
        attachments: Vec<LlmAttachment>,
        metadata: Option<&Value>,
        options: &AgentOptions,
    ) -> AgentResult<AgentTurn> {
        let mut turn = prepare_turn(
            &self.settings,
            self.memory.as_ref(),
            &self.prompts,
            message,
            attachments,
            metadata,
            options,
        )?;
        if let Some(actor_context) = self.actor_context_for_prompt()?
            && let Some(system) = turn.messages.first_mut()
        {
            system.content.push_str("\n\n<actor_context>\n");
            system.content.push_str(&actor_context);
            system.content.push_str("\n</actor_context>");
        }
        Ok(turn)
    }

    pub async fn prepare_turn_with_attachments_async(
        &self,
        message: &str,
        attachments: Vec<LlmAttachment>,
        options: &AgentOptions,
    ) -> AgentResult<AgentTurn> {
        self.prepare_turn_with_attachments_metadata_async(message, attachments, None, options)
            .await
    }

    pub async fn prepare_turn_with_attachments_metadata_async(
        &self,
        message: &str,
        attachments: Vec<LlmAttachment>,
        metadata: Option<&Value>,
        options: &AgentOptions,
    ) -> AgentResult<AgentTurn> {
        let mut turn = prepare_turn(
            &self.settings,
            self.memory.as_ref(),
            &self.prompts,
            message,
            attachments,
            metadata,
            options,
        )?;
        if let Some(actor_context) = self.actor_context_for_prompt_async().await?
            && let Some(system) = turn.messages.first_mut()
        {
            system.content.push_str("\n\n<actor_context>\n");
            system.content.push_str(&actor_context);
            system.content.push_str("\n</actor_context>");
        }
        Ok(turn)
    }

    pub async fn chat_once(&self, message: &str, options: &AgentOptions) -> AgentResult<String> {
        self.chat_once_with_runtime(message, options, ToolRuntime::default())
            .await
    }

    pub async fn chat_once_with_metadata(
        &self,
        message: &str,
        metadata: Value,
        options: &AgentOptions,
    ) -> AgentResult<String> {
        self.chat_once_with_attachments_metadata_runtime(
            message,
            Vec::new(),
            Some(metadata),
            options,
            ToolRuntime::default(),
        )
        .await
    }

    pub async fn chat_once_with_runtime(
        &self,
        message: &str,
        options: &AgentOptions,
        runtime: ToolRuntime,
    ) -> AgentResult<String> {
        self.chat_once_with_attachments_runtime(message, Vec::new(), options, runtime)
            .await
    }

    pub async fn chat_once_with_attachments_runtime(
        &self,
        message: &str,
        attachments: Vec<LlmAttachment>,
        options: &AgentOptions,
        runtime: ToolRuntime,
    ) -> AgentResult<String> {
        self.chat_once_with_attachments_metadata_runtime(
            message,
            attachments,
            None,
            options,
            runtime,
        )
        .await
    }

    pub async fn chat_once_with_attachments_metadata_runtime(
        &self,
        message: &str,
        attachments: Vec<LlmAttachment>,
        metadata: Option<Value>,
        options: &AgentOptions,
        runtime: ToolRuntime,
    ) -> AgentResult<String> {
        let turn = self
            .prepare_turn_with_attachments_metadata_async(
                message,
                attachments,
                metadata.as_ref(),
                options,
            )
            .await?;
        if !turn.synthetic {
            self.memory.messages.add("user", message, metadata)?;
        }
        let runtime = self.with_actor_runtime(runtime);
        let response = self
            .complete_turn_with_tools(turn.messages, runtime, !turn.synthetic)
            .await?;
        if !turn.synthetic {
            let history_content = assistant_history_content(&response);
            self.memory
                .messages
                .add("assistant", &history_content, None)?;
        }
        if self.settings.curator_enabled {
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
        let curator = MemoryCurator::new(self.settings.memory_dir.join("curator_state.json"));
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

        if self.settings.curator_enabled {
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

    fn actor_context_for_prompt(&self) -> AgentResult<Option<String>> {
        let (Some(registry), Some(actor_id)) = (&self.actor_registry, &self.principal_actor_id)
        else {
            return Ok(None);
        };
        let context = registry
            .build_system_prompt_blocking(actor_id)
            .map_err(|error| AgentError::Llm(anyhow!("actor context failed: {error}")))?;
        Ok(Some(context))
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

fn actor_turn_executor(
    settings: Settings,
    memory: Arc<MemoryStore>,
    router: Arc<RwLock<LlmRouter>>,
    shell: ShellTools,
) -> ActorTurnExecutor {
    let context = TurnExecutionContext {
        settings,
        memory,
        router,
        shell,
    };
    Arc::new(move |spec: ActorRunSpec, runtime: ActorRuntime| {
        let context = context.clone();
        Box::pin(async move {
            let tool_runtime = ToolRuntime {
                actor: Some(ActorToolContext {
                    runtime: runtime.clone(),
                    actor_id: spec.actor_id.clone(),
                    is_subagent: true,
                }),
                requested_tools: spec.requested_tools.clone(),
                ..ToolRuntime::default()
            };
            let messages = vec![
                LlmMessage::system(spec.system_prompt.clone()),
                LlmMessage::user(actor_turn_instruction(&spec)),
            ];
            complete_turn_with_tools_config_shared(
                context,
                messages,
                tool_runtime,
                spec.model == ModelTier::Aux,
                false,
            )
            .await
            .map_err(|error| ActorError::Runtime(error.to_string()))
        })
    })
}

async fn complete_turn_with_tools_config_shared(
    context: TurnExecutionContext,
    messages: Vec<LlmMessage>,
    runtime: ToolRuntime,
    use_aux: bool,
    record_tool_messages: bool,
) -> AgentResult<String> {
    let mut active_tools = runtime
        .requested_tools
        .iter()
        .map(|name| name.trim().to_string())
        .filter(|name| !name.is_empty())
        .collect::<HashSet<_>>();
    let registry = ToolRegistry::with_runtime(
        context.memory.as_ref(),
        context.settings.workspace_dir.clone(),
        context.settings.cache_dir.clone(),
        &context.shell,
        runtime,
    );
    let mut request = build_chat_request(messages);
    let mut last_text = String::new();

    for iteration in 0..MAX_TOOL_ITERATIONS {
        request.tools = Some(registry.tools_for_active(&active_tools));
        tracing::debug!(
            iteration,
            messages = request.messages.len(),
            tools = request.tools.as_ref().map_or(0, Vec::len),
            active_tools = ?active_tools,
            "llm tool loop iteration"
        );
        let router = context
            .router
            .read()
            .map_err(|error| AgentError::Llm(anyhow!("router lock poisoned: {error}")))?
            .clone();
        let response = router.exec_chat_request(request.clone(), use_aux).await?;
        let text = response.first_text().unwrap_or_default().to_string();
        let tool_calls = response
            .tool_calls()
            .into_iter()
            .cloned()
            .collect::<Vec<_>>();
        tracing::info!(
            iteration,
            text_chars = text.chars().count(),
            tool_calls = tool_calls.len(),
            "llm response received"
        );

        if tool_calls.is_empty() {
            return Ok(text);
        }

        if !text.trim().is_empty() {
            last_text = text.clone();
        }
        if record_tool_messages {
            context.memory.messages.add(
                "assistant",
                &text,
                Some(json!({ "tool_calls": tool_calls_metadata(&tool_calls) })),
            )?;
        }
        request
            .messages
            .push(assistant_tool_message(text, tool_calls.clone()));

        let mut image_views = Vec::new();
        for call in tool_calls {
            let call_id = call.call_id.clone();
            let tool_name = call.fn_name.clone();
            tracing::info!(
                iteration,
                tool = %tool_name,
                call_id = %call_id,
                args = %truncate_log_text(&call.fn_arguments.to_string(), 1200),
                "tool call started"
            );
            let should_stop_after_tool =
                matches!(call.fn_name.as_str(), "terminate" | "restart_self");
            let raw_result = if call.fn_name == "request_tool" {
                request_tool_for_turn(&registry, &mut active_tools, &call.fn_arguments)
            } else if registry.tool_is_active(&call.fn_name, &active_tools) {
                match registry.telegram_typing_context() {
                    Some((token, chat_id)) => {
                        with_telegram_tool_typing(
                            token,
                            chat_id,
                            registry.execute_async(&call.fn_name, &call.fn_arguments),
                        )
                        .await
                    }
                    None => {
                        registry
                            .execute_async(&call.fn_name, &call.fn_arguments)
                            .await
                    }
                }
            } else if registry.tool_is_available(&call.fn_name) {
                format!(
                    "Tool '{}' is available but not loaded. Call request_tool(name=\"{}\") first.",
                    call.fn_name, call.fn_name
                )
            } else {
                format!("Unknown tool: {}", call.fn_name)
            };
            let (result, views) = extract_image_views(raw_result);
            tracing::info!(
                iteration,
                tool = %tool_name,
                call_id = %call_id,
                result_chars = result.chars().count(),
                result = %truncate_log_text(&result, 1200),
                "tool call completed"
            );
            image_views.extend(views);
            if record_tool_messages {
                context.memory.messages.add(
                    "tool",
                    &result,
                    Some(json!({
                        "tool_call_id": call.call_id.clone(),
                        "name": call.fn_name.clone(),
                    })),
                )?;
            }
            let stop_result = result.clone();
            request
                .messages
                .push(ChatMessage::from(ToolResponse::new(call.call_id, result)));
            if should_stop_after_tool {
                return Ok(stop_result);
            }
        }
        for image_view in image_views {
            request.messages.push(image_view_message(image_view));
        }
    }

    if last_text.trim().is_empty() {
        Ok("Tool iteration limit reached before a final response.".to_string())
    } else {
        Ok(last_text)
    }
}

fn actor_turn_instruction(spec: &ActorRunSpec) -> String {
    let inbox = if spec.has_pending_messages {
        "You have pending inbox messages in the actor context. Account for them before acting."
    } else {
        "No pending inbox messages are visible beyond the actor context."
    };
    if spec.turn_number == 1 {
        format!(
            "Begin your actor task now. {inbox}\nUse tools as needed. If you finish, call terminate(result=..., outcome=\"success\"). This is turn {}/{}.",
            spec.turn_number, spec.max_turns
        )
    } else {
        format!(
            "Continue your actor task. {inbox}\nReport progress with send_message(..., channel=\"task_update\", kind=\"progress\") when useful, and call terminate(...) when done. This is turn {}/{}.",
            spec.turn_number, spec.max_turns
        )
    }
}

fn request_tool_for_turn(
    registry: &ToolRegistry<'_>,
    active_tools: &mut HashSet<String>,
    args: &Value,
) -> String {
    let name = args
        .get("name")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .trim();
    if name.is_empty() {
        return "Error: tool name is required.".to_string();
    }
    if !registry.tool_is_available(name) {
        let available = registry.requestable_tool_names().join(", ");
        return format!("Unknown tool: {name}. Available extended tools: {available}");
    }
    if registry.tool_is_active(name, active_tools) {
        return format!("Tool '{name}' is already available. You can use it now.");
    }
    active_tools.insert(name.to_string());
    format!("Tool '{name}' is now available. You can use it in the next tool call.")
}

async fn with_telegram_tool_typing<F, T>(token: String, chat_id: i64, future: F) -> T
where
    F: Future<Output = T>,
{
    let Ok(client) = TelegramClient::new(token, Vec::new()) else {
        return future.await;
    };

    let _ = client.send_chat_action(chat_id, "typing").await;
    let typing_task = tokio::spawn(telegram_tool_typing_loop(client, chat_id));
    let output = future.await;
    typing_task.abort();
    let _ = typing_task.await;
    output
}

async fn telegram_tool_typing_loop(client: TelegramClient, chat_id: i64) {
    loop {
        tokio::time::sleep(std::time::Duration::from_secs(
            TELEGRAM_TOOL_TYPING_REFRESH_SECONDS,
        ))
        .await;
        if let Err(error) = client.send_chat_action(chat_id, "typing").await {
            tracing::debug!(chat_id, error = %error, "telegram tool typing action failed");
            return;
        }
    }
}

fn truncate_log_text(value: &str, limit: usize) -> String {
    let mut truncated = value.chars().take(limit).collect::<String>();
    if value.chars().count() > limit {
        truncated.push_str("...[truncated]");
    }
    truncated
}

fn assistant_tool_message(text: String, tool_calls: Vec<ToolCall>) -> ChatMessage {
    let mut parts = Vec::new();
    if !text.trim().is_empty() {
        parts.push(ContentPart::Text(text));
    }
    parts.extend(tool_calls.into_iter().map(ContentPart::ToolCall));
    ChatMessage::assistant(MessageContent::from_parts(parts))
}

fn tool_calls_metadata(tool_calls: &[ToolCall]) -> Vec<Value> {
    tool_calls
        .iter()
        .map(|call| {
            json!({
                "id": call.call_id,
                "type": "function",
                "function": {
                    "name": call.fn_name,
                    "arguments": call.fn_arguments.to_string(),
                },
                "call_id": call.call_id,
                "fn_name": call.fn_name,
                "fn_arguments": call.fn_arguments,
            })
        })
        .collect()
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct ImageView {
    path: String,
    attachment: LlmAttachment,
}

fn extract_image_views(result: String) -> (String, Vec<ImageView>) {
    let Ok(mut value) = serde_json::from_str::<Value>(&result) else {
        return (result, vec![]);
    };
    let Some(object) = value.as_object_mut() else {
        return (result, vec![]);
    };
    let Some(image) = object.remove("_image_view") else {
        return (result, vec![]);
    };

    let Some(data) = image.get("data").and_then(Value::as_str) else {
        return (json_without_image_view(value, result), vec![]);
    };
    let Some(mime_type) = image.get("mime_type").and_then(Value::as_str) else {
        return (json_without_image_view(value, result), vec![]);
    };
    let path = image
        .get("path")
        .and_then(Value::as_str)
        .unwrap_or("image")
        .to_string();
    let name = image
        .get("name")
        .and_then(Value::as_str)
        .map(str::to_string)
        .or_else(|| {
            std::path::Path::new(&path)
                .file_name()
                .and_then(|name| name.to_str())
                .map(str::to_string)
        });
    let attachment = LlmAttachment {
        content_type: mime_type.to_string(),
        base64_content: data.to_string(),
        name,
    };
    (
        json_without_image_view(value, result),
        vec![ImageView { path, attachment }],
    )
}

fn json_without_image_view(value: Value, fallback: String) -> String {
    serde_json::to_string(&value).unwrap_or(fallback)
}

fn image_view_message(image: ImageView) -> ChatMessage {
    ChatMessage::user(MessageContent::from_parts(vec![
        ContentPart::Text(format!("[Image from {}]", image.path)),
        ContentPart::from_binary_base64(
            image.attachment.content_type,
            image.attachment.base64_content,
            image.attachment.name,
        ),
    ]))
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
            enabled: settings.hippocampus_enabled,
            ..Default::default()
        })
        .recall(memory, message, &recent)?
    } else {
        None
    };

    let tool_context = recent_tool_context_for_turn(&raw_recent, settings);
    let system = build_system_prompt(memory, prompts, recall.as_deref(), tool_context.as_deref())?;
    let mut messages = vec![LlmMessage::system(system)];
    messages.extend(recent.into_iter().filter_map(history_to_llm_message));
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

fn build_system_prompt(
    memory: &MemoryStore,
    prompts: &PromptStore,
    recall: Option<&str>,
    tool_context: Option<&str>,
) -> AgentResult<String> {
    let identity = memory
        .blocks
        .get("identity")
        .map_err(MemoryStoreError::Blocks)?
        .map(|block| block.value)
        .unwrap_or_default();
    let instructions = prompts.load("agent_instructions", "You are Lethe.").text;
    let tools_doc = prompts.load("agent_tools", "").text;
    let memory_context = memory.get_context_for_prompt()?;
    let mut parts = Vec::new();
    if !identity.trim().is_empty() {
        parts.push(format!(
            "<identity_block>\n{}\n</identity_block>",
            identity.trim()
        ));
    }
    parts.push(instructions);
    if !tools_doc.trim().is_empty() {
        parts.push(format!(
            "<tool_reference_block>\n{}\n</tool_reference_block>",
            tools_doc.trim()
        ));
    }
    parts.push(memory_context);
    if let Some(tool_context) = tool_context.filter(|value| !value.trim().is_empty()) {
        parts.push(format!(
            "<runtime_context source=\"recent_tool_history\">\n{tool_context}\n</runtime_context>"
        ));
    }
    if let Some(recall) = recall.filter(|value| !value.trim().is_empty()) {
        let timestamp = Local::now().format("%a %Y-%m-%d %H:%M:%S %Z");
        parts.push(format!(
            "<runtime_context source=\"hippocampus\" timestamp=\"{timestamp}\">\n<recall_block source=\"hippocampus\">\n{recall}\n</recall_block>\n</runtime_context>"
        ));
    }
    Ok(parts
        .into_iter()
        .filter(|part| !part.trim().is_empty())
        .collect::<Vec<_>>()
        .join("\n\n"))
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
        if message.role.as_str() == "user" {
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

#[derive(Clone, Debug)]
struct ToolCallRecord {
    id: String,
    name: String,
    arguments: String,
}

#[derive(Clone, Debug)]
struct ToolResultRecord {
    call_id: String,
    name: String,
    content: String,
}

#[derive(Clone, Debug)]
struct ToolHistoryGroup {
    created_at: String,
    assistant_text: String,
    calls: Vec<ToolCallRecord>,
    results: Vec<ToolResultRecord>,
}

fn recent_tool_context_for_turn(recent: &[StoredMessage], settings: &Settings) -> Option<String> {
    let mut groups = Vec::new();
    let mut current: Option<ToolHistoryGroup> = None;
    let mut inside_internal_turn = false;

    for message in recent {
        let internal = MessageMetadata::from_value(Some(&message.metadata)).is_internal();
        if message.role.as_str() == "user" {
            inside_internal_turn = internal;
            if let Some(group) = current.take() {
                groups.push(group);
            }
            continue;
        }
        if inside_internal_turn || internal {
            continue;
        }

        match message.role.as_str() {
            "assistant" => {
                let calls = tool_calls_from_metadata(&message.metadata);
                if calls.is_empty() {
                    if let Some(group) = current.take() {
                        groups.push(group);
                    }
                } else {
                    if let Some(group) = current.take() {
                        groups.push(group);
                    }
                    current = Some(ToolHistoryGroup {
                        created_at: message.created_at.clone(),
                        assistant_text: message.content.clone(),
                        calls,
                        results: Vec::new(),
                    });
                }
            }
            "tool" => {
                if let Some(group) = current.as_mut()
                    && let Some(result) = tool_result_from_message(message)
                    && !SEARCH_RESULT_SKIP_TOOLS.contains(&result.name.as_str())
                {
                    group.results.push(result);
                }
            }
            _ => {
                if let Some(group) = current.take() {
                    groups.push(group);
                }
            }
        }
    }
    if let Some(group) = current {
        groups.push(group);
    }

    groups.retain(|group| {
        group
            .calls
            .iter()
            .any(|call| !SEARCH_RESULT_SKIP_TOOLS.contains(&call.name.as_str()))
            && !group.results.is_empty()
    });
    if groups.is_empty() {
        return None;
    }

    let start = groups.len().saturating_sub(RECENT_TOOL_CONTEXT_GROUPS);
    let selected = &groups[start..];
    let latest_index = selected.len().saturating_sub(1);
    let mut parts = vec![format!(
        "<recent_tool_context groups=\"{}\">",
        selected.len()
    )];
    for (index, group) in selected.iter().enumerate() {
        parts.push(format_tool_history_group(group, index == latest_index));
    }
    parts.push("</recent_tool_context>".to_string());

    Some(cap_context_text(
        &parts.join("\n"),
        tool_context_budget_chars(settings),
        "recent tool context",
    ))
}

fn tool_calls_from_metadata(metadata: &Value) -> Vec<ToolCallRecord> {
    metadata
        .get("tool_calls")
        .and_then(Value::as_array)
        .map(|calls| {
            calls
                .iter()
                .filter_map(|call| {
                    let id = call
                        .get("id")
                        .or_else(|| call.get("call_id"))
                        .and_then(Value::as_str)
                        .unwrap_or_default()
                        .to_string();
                    let name = call
                        .get("function")
                        .and_then(|function| function.get("name"))
                        .or_else(|| call.get("fn_name"))
                        .or_else(|| call.get("name"))
                        .and_then(Value::as_str)
                        .unwrap_or_default()
                        .trim()
                        .to_string();
                    if name.is_empty() {
                        return None;
                    }
                    let arguments = call
                        .get("function")
                        .and_then(|function| function.get("arguments"))
                        .or_else(|| call.get("fn_arguments"))
                        .or_else(|| call.get("arguments"))
                        .map(format_jsonish)
                        .unwrap_or_default();
                    Some(ToolCallRecord {
                        id,
                        name,
                        arguments,
                    })
                })
                .collect()
        })
        .unwrap_or_default()
}

fn tool_result_from_message(message: &StoredMessage) -> Option<ToolResultRecord> {
    let name = message
        .metadata
        .get("name")
        .and_then(Value::as_str)
        .unwrap_or("tool")
        .trim()
        .to_string();
    if name.is_empty() {
        return None;
    }
    Some(ToolResultRecord {
        call_id: message
            .metadata
            .get("tool_call_id")
            .or_else(|| message.metadata.get("call_id"))
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string(),
        name,
        content: message.content.clone(),
    })
}

fn format_tool_history_group(group: &ToolHistoryGroup, full_latest: bool) -> String {
    let mut results_by_id = HashMap::<&str, Vec<&ToolResultRecord>>::new();
    let mut unmatched_results = Vec::new();
    for result in &group.results {
        if result.call_id.trim().is_empty() {
            unmatched_results.push(result);
        } else {
            results_by_id
                .entry(result.call_id.as_str())
                .or_default()
                .push(result);
        }
    }

    let mut parts = vec![format!("<tool_turn timestamp=\"{}\">", group.created_at)];
    if !group.assistant_text.trim().is_empty() {
        parts.push(format!(
            "<assistant_tool_prelude>\n{}\n</assistant_tool_prelude>",
            group.assistant_text.trim()
        ));
    }

    for call in &group.calls {
        if SEARCH_RESULT_SKIP_TOOLS.contains(&call.name.as_str()) {
            continue;
        }
        parts.push(format!(
            "<tool_call name=\"{}\" id=\"{}\">",
            call.name, call.id
        ));
        if !call.arguments.trim().is_empty() {
            parts.push(format!("<arguments>\n{}\n</arguments>", call.arguments));
        }
        let mut attached = results_by_id
            .remove(call.id.as_str())
            .unwrap_or_default()
            .into_iter()
            .collect::<Vec<_>>();
        if attached.is_empty() && group.calls.len() == 1 {
            attached.append(&mut unmatched_results);
        } else {
            let mut index = 0;
            while index < unmatched_results.len() {
                if unmatched_results[index].name == call.name {
                    attached.push(unmatched_results.remove(index));
                } else {
                    index += 1;
                }
            }
        }
        for result in attached {
            parts.push(format_tool_result(result, full_latest));
        }
        parts.push("</tool_call>".to_string());
    }

    for results in results_by_id.into_values() {
        for result in results {
            parts.push(format_tool_result(result, full_latest));
        }
    }
    for result in unmatched_results {
        parts.push(format_tool_result(result, full_latest));
    }

    parts.push("</tool_turn>".to_string());
    parts.join("\n")
}

fn format_tool_result(result: &ToolResultRecord, full_latest: bool) -> String {
    let original_chars = result.content.chars().count();
    let original_lines = result.content.lines().count().max(1);
    let (content, mode) = if full_latest {
        (result.content.clone(), "full")
    } else {
        (preview_tool_result(&result.content), "preview")
    };
    format!(
        "<tool_result name=\"{}\" mode=\"{}\" chars=\"{}\" lines=\"{}\">\n{}\n</tool_result>",
        result.name, mode, original_chars, original_lines, content
    )
}

fn preview_tool_result(content: &str) -> String {
    let lines = content.lines().collect::<Vec<_>>();
    let mut preview = if lines.len() > OLD_TOOL_RESULT_PREVIEW_LINES {
        format!(
            "{}\n[... {} more lines skipped]",
            lines[..OLD_TOOL_RESULT_PREVIEW_LINES].join("\n"),
            lines.len() - OLD_TOOL_RESULT_PREVIEW_LINES
        )
    } else {
        content.to_string()
    };
    if preview.chars().count() > OLD_TOOL_RESULT_PREVIEW_CHARS {
        preview = format!(
            "{}\n[... {} chars skipped]",
            take_chars(&preview, OLD_TOOL_RESULT_PREVIEW_CHARS),
            preview.chars().count() - OLD_TOOL_RESULT_PREVIEW_CHARS
        );
    }
    preview
}

fn tool_context_budget_chars(settings: &Settings) -> usize {
    let proportional = settings
        .llm_context_limit
        .saturating_mul(4)
        .saturating_mul(TOOL_CONTEXT_SHARE_NUMERATOR)
        / TOOL_CONTEXT_SHARE_DENOMINATOR;
    proportional.clamp(TOOL_CONTEXT_MIN_CHARS, TOOL_CONTEXT_MAX_CHARS)
}

fn cap_context_text(value: &str, max_chars: usize, label: &str) -> String {
    let chars = value.chars().count();
    if chars <= max_chars {
        return value.to_string();
    }
    let keep = max_chars.saturating_sub(200).max(1);
    let tail_probe = value
        .chars()
        .rev()
        .take(2_000)
        .collect::<String>()
        .chars()
        .rev()
        .collect::<String>()
        .to_ascii_lowercase();
    let has_error_tail = [
        "error",
        "exception",
        "failed",
        "fatal",
        "traceback",
        "panic",
        "exit code",
    ]
    .iter()
    .any(|needle| tail_probe.contains(needle));
    let head_share = if has_error_tail { 60 } else { 70 };
    let head_chars = keep.saturating_mul(head_share) / 100;
    let tail_chars = keep.saturating_sub(head_chars);
    format!(
        "{}\n\n[... {} chars truncated from {label} ...]\n\n{}",
        take_chars(value, head_chars),
        chars.saturating_sub(keep),
        take_last_chars(value, tail_chars)
    )
}

fn take_chars(value: &str, limit: usize) -> String {
    value.chars().take(limit).collect()
}

fn take_last_chars(value: &str, limit: usize) -> String {
    let mut chars = value.chars().rev().take(limit).collect::<Vec<_>>();
    chars.reverse();
    chars.into_iter().collect()
}

fn format_jsonish(value: &Value) -> String {
    match value {
        Value::String(value) => value.clone(),
        other => serde_json::to_string(other).unwrap_or_default(),
    }
}

fn is_visible_history_record(message: &StoredMessage) -> bool {
    !message.content.trim().is_empty()
        && matches!(message.role.as_str(), "user" | "assistant")
        && message.metadata.get("tool_calls").is_none()
        && !MessageMetadata::from_value(Some(&message.metadata)).is_internal()
}

fn drop_history_before_first_user(history: &mut Vec<StoredMessage>) {
    let Some(first_user) = history
        .iter()
        .position(|message| message.role.as_str() == "user")
    else {
        history.clear();
        return;
    };
    if first_user > 0 {
        history.drain(0..first_user);
    }
}

fn history_to_llm_message(message: StoredMessage) -> Option<LlmMessage> {
    let content = message.content;
    if content.trim().is_empty() {
        return None;
    }
    if message.metadata.get("tool_calls").is_some() {
        return None;
    }
    match message.role.as_str() {
        "user" => Some(LlmMessage {
            role: LlmRole::User,
            content,
            attachments: vec![],
        }),
        "assistant" => Some(LlmMessage {
            role: LlmRole::Assistant,
            content,
            attachments: vec![],
        }),
        _ => None,
    }
}

fn assistant_history_content(response: &str) -> String {
    normalize_message_envelope(response).unwrap_or_else(|| response.to_string())
}

#[cfg(test)]
mod tests {
    use std::path::PathBuf;

    use tempfile::tempdir;

    use super::*;
    use crate::config::{RuntimeMode, Settings};
    use crate::message_metadata::{MessageKind, MessageVisibility, metadata_value};

    fn settings(root: &std::path::Path) -> Settings {
        Settings {
            agent_name: "lethe".to_string(),
            mode: RuntimeMode::Cli,
            telegram_bot_token: String::new(),
            telegram_allowed_user_ids: vec![],
            telegram_transcription_enabled: true,
            lethe_api_token: String::new(),
            lethe_api_host: "127.0.0.1".to_string(),
            lethe_api_port: 8080,
            openrouter_api_key: String::new(),
            openai_api_key: String::new(),
            llm_model: "test-model".to_string(),
            llm_model_aux: String::new(),
            llm_provider: String::new(),
            llm_api_base: String::new(),
            llm_context_limit: 100_000,
            lethe_home: root.to_path_buf(),
            config_dir: PathBuf::from("config"),
            workspace_dir: root.join("workspace"),
            memory_dir: root.join("data").join("memory"),
            db_path: root.join("data/lethe.db"),
            credentials_dir: root.join("credentials"),
            cache_dir: root.join("cache"),
            logs_dir: root.join("logs"),
            notes_dir: root.join("workspace/notes"),
            transcription_provider: String::new(),
            transcription_model: String::new(),
            transcription_language: String::new(),
            transcription_local_command: "whisper".to_string(),
            actors_enabled: true,
            hippocampus_enabled: true,
            curator_enabled: true,
            heartbeat_enabled: true,
            heartbeat_interval_seconds: 3600,
            debounce_seconds: 5.0,
            proactive_max_per_day: 4,
            proactive_cooldown_minutes: 60,
        }
    }

    #[test]
    fn prepare_turn_includes_memory_context_history_and_recall() {
        let tmp = tempdir().unwrap();
        let settings = settings(tmp.path());
        let memory = MemoryStore::from_settings(&settings).unwrap();
        let prompts = PromptStore::new(&settings.workspace_dir, &settings.config_dir);

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
            .add("user", "previous graph question", None)
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
        assert!(turn.messages[0].content.contains("<identity_block>"));
        assert!(turn.messages[0].content.contains("<tool_reference_block>"));
        assert!(turn.messages[0].content.contains("<memory_metadata>"));
        assert!(
            turn.messages[0]
                .content
                .contains("<runtime_context source=\"hippocampus\"")
        );
        assert!(turn.messages[0].content.contains("Graph Email"));
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
        let prompts = PromptStore::new(&settings.workspace_dir, &settings.config_dir);

        memory
            .messages
            .add(
                "assistant",
                "I will inspect that now.",
                Some(json!({"tool_calls": [{"name": "bash"}]})),
            )
            .unwrap();
        memory
            .messages
            .add("tool", "secret tool output", Some(json!({"name": "bash"})))
            .unwrap();
        memory
            .messages
            .add("user", "previous visible user request", None)
            .unwrap();
        memory
            .messages
            .add("assistant", "previous visible answer", None)
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
        assert!(
            turn.messages[0]
                .content
                .contains("<runtime_context source=\"recent_tool_history\">")
        );
        assert!(
            turn.messages[0]
                .content
                .contains("<tool_call name=\"bash\"")
        );
        assert!(turn.messages[0].content.contains("secret tool output"));
    }

    #[test]
    fn prepare_turn_fetches_enough_raw_history_to_survive_tool_spam() {
        let tmp = tempdir().unwrap();
        let settings = settings(tmp.path());
        let memory = MemoryStore::from_settings(&settings).unwrap();
        let prompts = PromptStore::new(&settings.workspace_dir, &settings.config_dir);

        memory
            .messages
            .add("user", "original thing to inspect", None)
            .unwrap();
        for index in 0..30 {
            memory
                .messages
                .add(
                    "assistant",
                    &format!("tool prelude {index}"),
                    Some(json!({"tool_calls": [{"name": "bash"}]})),
                )
                .unwrap();
            memory
                .messages
                .add("tool", &format!("tool output {index}"), None)
                .unwrap();
        }
        memory
            .messages
            .add("user", "did you inspect it?", None)
            .unwrap();

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
            turn.messages
                .iter()
                .any(|message| message.content == "original thing to inspect")
        );
        assert!(
            !turn
                .messages
                .iter()
                .skip(1)
                .any(|message| message.content.starts_with("tool prelude"))
        );
    }

    #[test]
    fn prepare_turn_excludes_internal_metadata_turns_from_history() {
        let tmp = tempdir().unwrap();
        let settings = settings(tmp.path());
        let memory = MemoryStore::from_settings(&settings).unwrap();
        let prompts = PromptStore::new(&settings.workspace_dir, &settings.config_dir);
        let internal = metadata_value(
            MessageVisibility::Internal,
            MessageKind::Heartbeat,
            "heartbeat",
        );

        memory
            .messages
            .add("user", "internal heartbeat prompt", Some(internal.clone()))
            .unwrap();
        memory
            .messages
            .add("assistant", "internal heartbeat answer", None)
            .unwrap();
        memory
            .messages
            .add("user", "visible question", None)
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
        let prompts = PromptStore::new(&settings.workspace_dir, &settings.config_dir);
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
        assert!(
            !turn.messages[0]
                .content
                .contains("<runtime_context source=\"hippocampus\"")
        );
    }

    #[test]
    fn agent_prepare_turn_includes_principal_actor_context_when_enabled() {
        let tmp = tempdir().unwrap();
        let settings = settings(tmp.path());
        let agent = Agent::from_settings(settings).unwrap();

        let turn = agent
            .prepare_turn("Please research this in parallel", &AgentOptions::default())
            .unwrap();

        assert!(agent.actor_registry().is_some());
        assert!(agent.principal_actor_id().is_some());
        assert!(turn.messages[0].content.contains("<actor_context>"));
        assert!(turn.messages[0].content.contains("You are the cortex"));
        assert!(turn.messages[0].content.contains("spawn_actor"));
    }

    #[test]
    fn agent_prepare_turn_omits_actor_context_when_disabled() {
        let tmp = tempdir().unwrap();
        let mut settings = settings(tmp.path());
        settings.actors_enabled = false;
        let agent = Agent::from_settings(settings).unwrap();

        let turn = agent
            .prepare_turn("Handle this directly", &AgentOptions::default())
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
