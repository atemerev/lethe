//! The LLM tool-use iteration: runs the model, dispatches each requested
//! tool, surfaces results (and inline images) back into the next turn, and
//! gives observers (e.g. Telegram typing) a chance to wrap each tool call.
//!
//! Lives next to [`Agent`](super::Agent) but stays as free functions so it
//! can be shared between the user-facing chat path and the actor turn
//! executor without dragging Agent itself across the kameo boundary.

use std::collections::HashSet;
use std::sync::{Arc, RwLock};

use anyhow::anyhow;
use genai::chat::{ChatMessage, ContentPart, MessageContent, ToolCall, ToolResponse};
use serde_json::{Value, json};

use crate::actor::{ActorError, ActorRunSpec, ActorRuntime, ActorTurnExecutor, ModelTier};
use crate::config::Settings;
use crate::llm::{LlmAttachment, LlmMessage, LlmRouter, build_chat_request};
use crate::memory::MessageRole;
use crate::store::MemoryStore;
use crate::tools::registry::{ActorToolContext, BoxToolFuture, ToolRegistry, ToolRuntime};
use crate::tools::shell::ShellTools;

use super::{AgentError, AgentResult};

pub(super) const MAX_TOOL_ITERATIONS: usize = 8;

/// Bag of clones used to thread the agent's dependencies into the free-fn
/// tool loop. Cheap to clone because all members are already shared handles.
#[derive(Clone)]
pub(super) struct TurnExecutionContext {
    pub settings: Settings,
    pub memory: Arc<MemoryStore>,
    pub router: Arc<RwLock<LlmRouter>>,
    pub shell: ShellTools,
}

/// Build the actor turn executor that the [`ActorRuntime`] supervisor calls
/// to run each subagent turn. Wraps the standard tool loop with actor wiring.
pub(super) fn actor_turn_executor(
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

/// Run the LLM/tool loop end to end. Iterates up to [`MAX_TOOL_ITERATIONS`]
/// times: each iteration asks the model for the next move, executes any
/// returned tool calls, and feeds the results (plus any inline `_image_view`
/// payloads) back into the next request. Returns the final assistant text.
pub(super) async fn complete_turn_with_tools_config_shared(
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
        context.settings.paths.workspace_dir.clone(),
        context.settings.paths.cache_dir.clone(),
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
                MessageRole::Assistant,
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
                let inner: BoxToolFuture<'_> =
                    Box::pin(registry.execute_async(&call.fn_name, &call.fn_arguments));
                match registry.turn_observer() {
                    Some(observer) => observer.wrap_tool_call(&call.fn_name, inner).await,
                    None => inner.await,
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
                    MessageRole::Tool,
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

pub(super) fn actor_turn_instruction(spec: &ActorRunSpec) -> String {
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

pub(super) fn tool_calls_metadata(tool_calls: &[ToolCall]) -> Vec<Value> {
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
pub(super) struct ImageView {
    pub path: String,
    pub attachment: LlmAttachment,
}

pub(super) fn extract_image_views(result: String) -> (String, Vec<ImageView>) {
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

pub(super) fn image_view_message(image: ImageView) -> ChatMessage {
    ChatMessage::user(MessageContent::from_parts(vec![
        ContentPart::Text(format!("[Image from {}]", image.path)),
        ContentPart::from_binary_base64(
            image.attachment.content_type,
            image.attachment.base64_content,
            image.attachment.name,
        ),
    ]))
}
