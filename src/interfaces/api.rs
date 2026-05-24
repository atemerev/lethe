use std::collections::HashMap;
use std::convert::Infallible;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use anyhow::{Result, bail};
use async_stream::stream;
use axum::extract::{Query, State};
use axum::http::{HeaderMap, HeaderValue, StatusCode, header};
use axum::response::sse::{Event, KeepAlive, Sse};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use tokio::sync::{Mutex, broadcast, mpsc};
use uuid::Uuid;

use crate::agent::{Agent, AgentOptions};
use crate::config::Settings;
use crate::conversation::{ConversationManager, ProcessCallback, ProcessContext};
use crate::llm::models::{available_providers, provider_for_model};
use crate::llm::prompts::PromptStore;
use crate::memory::message_metadata::{
    MessageKind, MessageVisibility, metadata_value as message_metadata_value,
};
use crate::scheduler::heartbeat::{Heartbeat, HeartbeatAction, HeartbeatConfig};
use crate::scheduler::proactive::{ActiveReminder, ProactiveRateLimiter, format_active_reminders};
use crate::todos::{TodoFilter, TodoManager};
use crate::tools::registry::{ClientToolContext, ToolRuntime};

const SESSION_QUEUE_DEPTH: usize = 32;
const PROACTIVE_QUEUE_DEPTH: usize = 64;

#[derive(Clone)]
pub struct ApiState {
    settings: Settings,
    agent: Arc<Agent>,
    conversations: ConversationManager,
    sessions: Arc<Mutex<ApiSessions>>,
    proactive_tx: broadcast::Sender<ApiEvent>,
}

#[derive(Debug, Default)]
struct ApiSessions {
    by_id: HashMap<String, ApiSession>,
    by_chat: HashMap<i64, String>,
}

#[derive(Debug)]
struct ApiSession {
    chat_id: i64,
    sender: mpsc::Sender<ApiEvent>,
}

struct ApiStreamGuard {
    state: ApiState,
    chat_id: i64,
    session_id: String,
    finished: bool,
}

impl ApiStreamGuard {
    fn new(state: ApiState, chat_id: i64, session_id: String) -> Self {
        Self {
            state,
            chat_id,
            session_id,
            finished: false,
        }
    }

    async fn finish(&mut self) {
        self.finished = true;
        self.state.unregister_session(&self.session_id).await;
    }
}

impl Drop for ApiStreamGuard {
    fn drop(&mut self) {
        if self.finished {
            return;
        }

        let state = self.state.clone();
        let chat_id = self.chat_id;
        let session_id = self.session_id.clone();
        if let Ok(handle) = tokio::runtime::Handle::try_current() {
            handle.spawn(async move {
                if state.session_matches_chat(chat_id, &session_id).await {
                    state.conversations.cancel(chat_id).await;
                }
                state.unregister_session(&session_id).await;
            });
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct ApiEvent {
    pub event: String,
    pub data: Value,
}

impl ApiEvent {
    pub fn new(event: impl Into<String>, data: Value) -> Self {
        Self {
            event: event.into(),
            data,
        }
    }

    fn into_sse(self) -> Event {
        Event::default()
            .event(self.event)
            .data(self.data.to_string())
    }
}

impl ApiState {
    pub fn new(settings: Settings, agent: Agent) -> Self {
        let (proactive_tx, _) = broadcast::channel(PROACTIVE_QUEUE_DEPTH);
        Self {
            conversations: ConversationManager::new(Duration::from_secs_f64(
                settings.debounce_seconds,
            )),
            settings,
            agent: Arc::new(agent),
            sessions: Arc::new(Mutex::new(ApiSessions::default())),
            proactive_tx,
        }
    }

    pub fn from_settings(settings: Settings) -> Result<Self> {
        let agent = Agent::from_settings(settings.clone())?;
        Ok(Self::new(settings, agent))
    }

    pub async fn send_proactive(&self, content: &str) -> bool {
        let content = content.trim();
        if content.is_empty() {
            return false;
        }
        self.proactive_tx
            .send(ApiEvent::new(
                "text",
                json!({
                    "content": content,
                    "parse_mode": "Markdown",
                    "message_id": 0,
                    "proactive": true,
                }),
            ))
            .is_ok()
    }

    async fn register_session(&self, chat_id: i64, sender: mpsc::Sender<ApiEvent>) -> String {
        let session_id = Uuid::new_v4().simple().to_string();
        let previous = {
            let mut sessions = self.sessions.lock().await;
            let previous_id = sessions.by_chat.insert(chat_id, session_id.clone());
            let previous = previous_id.and_then(|id| sessions.by_id.remove(&id));
            sessions
                .by_id
                .insert(session_id.clone(), ApiSession { chat_id, sender });
            previous
        };

        if let Some(previous) = previous {
            close_sender(previous.sender).await;
        }
        session_id
    }

    async fn unregister_session(&self, session_id: &str) {
        let mut sessions = self.sessions.lock().await;
        if let Some(session) = sessions.by_id.remove(session_id)
            && sessions.by_chat.get(&session.chat_id) == Some(&session_id.to_string())
        {
            sessions.by_chat.remove(&session.chat_id);
        }
    }

    async fn close_chat_session(&self, chat_id: i64) -> bool {
        let session = {
            let mut sessions = self.sessions.lock().await;
            let Some(session_id) = sessions.by_chat.remove(&chat_id) else {
                return false;
            };
            sessions.by_id.remove(&session_id)
        };
        if let Some(session) = session {
            close_sender(session.sender).await;
            true
        } else {
            false
        }
    }

    async fn session_matches_chat(&self, chat_id: i64, session_id: &str) -> bool {
        let sessions = self.sessions.lock().await;
        sessions
            .by_chat
            .get(&chat_id)
            .is_some_and(|id| id == session_id)
            && sessions.by_id.contains_key(session_id)
    }

    async fn send_to_session(&self, session_id: &str, event: &str, data: Value) -> bool {
        let sender = {
            let sessions = self.sessions.lock().await;
            sessions
                .by_id
                .get(session_id)
                .map(|session| session.sender.clone())
        };
        let Some(sender) = sender else {
            return false;
        };
        sender.send(ApiEvent::new(event, data)).await.is_ok()
    }

    async fn client_tool_context(
        &self,
        session_id: &str,
        chat_id: i64,
        last_message_id: Option<i64>,
    ) -> Option<ClientToolContext> {
        let sender = {
            let sessions = self.sessions.lock().await;
            sessions
                .by_id
                .get(session_id)
                .map(|session| session.sender.clone())
        }?;
        Some(ClientToolContext::new(
            chat_id,
            last_message_id,
            move |event| {
                sender
                    .try_send(ApiEvent::new(event.event, event.data))
                    .is_ok()
            },
        ))
    }
}

async fn close_sender(sender: mpsc::Sender<ApiEvent>) {
    let _ = sender.send(ApiEvent::new("typing_stop", json!({}))).await;
    let _ = sender.send(ApiEvent::new("done", json!({}))).await;
}

pub fn router(state: ApiState) -> Router {
    Router::new()
        .route("/health", get(health))
        .route("/chat", post(chat))
        .route("/cancel", post(cancel))
        .route("/configure", post(configure))
        .route("/model", get(model_get).post(model_post))
        .route("/events", get(events))
        .route("/file", get(serve_file))
        .with_state(state)
}

pub async fn serve(settings: Settings, port: u16) -> Result<()> {
    if settings.lethe_api_token.trim().is_empty() {
        bail!("LETHE_API_TOKEN must be set in API mode");
    }

    let state = ApiState::from_settings(settings.clone())?;
    let app = router(state.clone());
    let bind = format!("{}:{port}", settings.lethe_api_host);
    let listener = tokio::net::TcpListener::bind(&bind).await?;
    println!("Lethe Rust API listening on http://{bind}");

    let heartbeat_task = if settings.heartbeat_enabled {
        Some(tokio::spawn(api_heartbeat_loop(state.clone())))
    } else {
        None
    };

    let result = axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await;
    if let Some(task) = heartbeat_task {
        task.abort();
    }
    Ok(result?)
}

async fn shutdown_signal() {
    let _ = tokio::signal::ctrl_c().await;
}

async fn health() -> Json<Value> {
    Json(json!({"status": "ready"}))
}

#[derive(Debug, Deserialize)]
struct ChatRequest {
    message: String,
    #[serde(default)]
    user_id: i64,
    #[serde(default)]
    chat_id: Option<i64>,
    #[serde(default)]
    metadata: serde_json::Map<String, Value>,
}

async fn chat(
    State(state): State<ApiState>,
    headers: HeaderMap,
    Json(mut body): Json<ChatRequest>,
) -> Response {
    if let Some(response) = require_auth(&state, &headers) {
        return response;
    }
    if body.message.trim().is_empty() {
        return json_error(StatusCode::BAD_REQUEST, "message is required");
    }

    let chat_id = body.chat_id.unwrap_or(body.user_id);
    let (sender, mut receiver) = mpsc::channel::<ApiEvent>(SESSION_QUEUE_DEPTH);
    let session_id = state.register_session(chat_id, sender).await;
    body.metadata
        .insert("_api_session_id".to_string(), json!(session_id.clone()));

    let callback = process_chat_callback(state.clone());
    state
        .conversations
        .add_message(
            chat_id,
            body.user_id,
            body.message,
            Some(body.metadata),
            Some(callback),
        )
        .await;

    let stream_state = state.clone();
    let stream_session_id = session_id.clone();
    let stream_chat_id = chat_id;
    let event_stream = stream! {
        let mut guard = ApiStreamGuard::new(stream_state, stream_chat_id, stream_session_id);
        while let Some(event) = receiver.recv().await {
            let done = event.event == "done";
            yield Ok::<Event, Infallible>(event.into_sse());
            if done {
                break;
            }
        }
        guard.finish().await;
    };

    Sse::new(event_stream)
        .keep_alive(KeepAlive::default())
        .into_response()
}

fn process_chat_callback(state: ApiState) -> ProcessCallback {
    Arc::new(move |context: ProcessContext| {
        let state = state.clone();
        Box::pin(async move {
            process_chat_context(state, context).await;
            Ok(())
        })
    })
}

async fn process_chat_context(state: ApiState, context: ProcessContext) {
    let session_id = context
        .metadata
        .get("_api_session_id")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    if session_id.is_empty() {
        return;
    }

    let _ = state
        .send_to_session(&session_id, "typing_start", json!({}))
        .await;
    let tool_runtime = ToolRuntime {
        client: state
            .client_tool_context(
                &session_id,
                context.chat_id,
                metadata_i64(&context.metadata, "message_id"),
            )
            .await,
        ..ToolRuntime::default()
    };
    let response = state
        .agent
        .chat_once_with_runtime(&context.message, &AgentOptions::default(), tool_runtime)
        .await;

    match response {
        Ok(message) if !context.interrupt.is_interrupted() && !message.trim().is_empty() => {
            let _ = state
                .send_to_session(
                    &session_id,
                    "text",
                    json!({
                        "content": message,
                        "parse_mode": "Markdown",
                        "message_id": 0,
                    }),
                )
                .await;
        }
        Ok(_) => {}
        Err(error) if !context.interrupt.is_interrupted() => {
            let _ = state
                .send_to_session(
                    &session_id,
                    "text",
                    json!({
                        "content": format!("Error: {error}"),
                        "parse_mode": null,
                        "message_id": 0,
                    }),
                )
                .await;
        }
        Err(_) => {}
    }

    let _ = state
        .send_to_session(&session_id, "typing_stop", json!({}))
        .await;
    let _ = state.send_to_session(&session_id, "done", json!({})).await;
    state.unregister_session(&session_id).await;
}

#[derive(Debug, Deserialize)]
struct ChatIdRequest {
    #[serde(default)]
    chat_id: i64,
}

async fn cancel(
    State(state): State<ApiState>,
    headers: HeaderMap,
    Json(body): Json<ChatIdRequest>,
) -> Response {
    if let Some(response) = require_auth(&state, &headers) {
        return response;
    }
    let cancelled = if body.chat_id == 0 {
        false
    } else {
        let conversation = state.conversations.cancel(body.chat_id).await;
        let session = state.close_chat_session(body.chat_id).await;
        conversation || session
    };
    Json(json!({"status": "cancelled", "cancelled": cancelled})).into_response()
}

#[derive(Debug, Deserialize)]
struct ConfigureRequest {
    #[serde(default)]
    user_id: i64,
    #[serde(default)]
    username: String,
    #[serde(default)]
    first_name: String,
}

async fn configure(
    State(state): State<ApiState>,
    headers: HeaderMap,
    Json(body): Json<ConfigureRequest>,
) -> Response {
    if let Some(response) = require_auth(&state, &headers) {
        return response;
    }

    let mut human = format!("Name: {}\n", body.first_name.trim());
    if !body.username.trim().is_empty() {
        human.push_str(&format!("Telegram: @{}\n", body.username.trim()));
    }
    human.push_str(&format!("User ID: {}\n", body.user_id));

    match state
        .agent
        .memory()
        .blocks
        .update("human", Some(&human), None)
    {
        Ok(true) => Json(json!({"status": "configured"})).into_response(),
        Ok(false) => match state.agent.memory().blocks.create(
            "human",
            &human,
            "Information about the human user.",
            crate::memory::DEFAULT_BLOCK_LIMIT,
            false,
            false,
        ) {
            Ok(_) => Json(json!({"status": "configured"})).into_response(),
            Err(error) => json_error(StatusCode::INTERNAL_SERVER_ERROR, &error.to_string()),
        },
        Err(error) => json_error(StatusCode::INTERNAL_SERVER_ERROR, &error.to_string()),
    }
}

async fn model_get(State(state): State<ApiState>, headers: HeaderMap) -> Response {
    if let Some(response) = require_auth(&state, &headers) {
        return response;
    }
    let config = match state.agent.router_config() {
        Ok(config) => config,
        Err(error) => return json_error(StatusCode::INTERNAL_SERVER_ERROR, &error.to_string()),
    };
    Json(json!({
        "model": config.model,
        "model_aux": config.aux_model,
        "provider": model_provider(&config.model, &state.settings.llm_provider),
        "current_auth": "API",
        "available_providers": available_provider_ids(),
        "provider_info": available_providers(),
    }))
    .into_response()
}

#[derive(Debug, Deserialize)]
struct ModelUpdateRequest {
    model: Option<String>,
    model_aux: Option<String>,
}

async fn model_post(
    State(state): State<ApiState>,
    headers: HeaderMap,
    Json(body): Json<ModelUpdateRequest>,
) -> Response {
    if let Some(response) = require_auth(&state, &headers) {
        return response;
    }
    let changed = match state
        .agent
        .reconfigure_models(body.model.as_deref(), body.model_aux.as_deref())
    {
        Ok(changed) => changed,
        Err(error) => return json_error(StatusCode::INTERNAL_SERVER_ERROR, &error.to_string()),
    };
    let config = match state.agent.router_config() {
        Ok(config) => config,
        Err(error) => return json_error(StatusCode::INTERNAL_SERVER_ERROR, &error.to_string()),
    };
    Json(json!({
        "status": "updated",
        "model": config.model,
        "model_aux": config.aux_model,
        "provider": model_provider(&config.model, &state.settings.llm_provider),
        "changed": changed,
    }))
    .into_response()
}

async fn events(State(state): State<ApiState>, headers: HeaderMap) -> Response {
    if let Some(response) = require_auth(&state, &headers) {
        return response;
    }
    let mut receiver = state.proactive_tx.subscribe();
    let event_stream = stream! {
        loop {
            match receiver.recv().await {
                Ok(event) => yield Ok::<Event, Infallible>(event.into_sse()),
                Err(broadcast::error::RecvError::Lagged(_)) => continue,
                Err(broadcast::error::RecvError::Closed) => break,
            }
        }
    };
    Sse::new(event_stream)
        .keep_alive(KeepAlive::default())
        .into_response()
}

#[derive(Debug, Deserialize)]
struct FileQuery {
    path: String,
}

async fn serve_file(
    State(state): State<ApiState>,
    headers: HeaderMap,
    Query(query): Query<FileQuery>,
) -> Response {
    if let Some(response) = require_auth(&state, &headers) {
        return response;
    }
    let Some(path) = resolve_workspace_path(&state.settings.workspace_dir, &query.path) else {
        return json_error(StatusCode::FORBIDDEN, "path outside workspace");
    };
    if !path.is_file() {
        return json_error(StatusCode::NOT_FOUND, &format!("not found: {}", query.path));
    }
    match std::fs::read(path) {
        Ok(bytes) => {
            let mut response = bytes.into_response();
            response.headers_mut().insert(
                header::CONTENT_TYPE,
                HeaderValue::from_static("application/octet-stream"),
            );
            response
        }
        Err(error) => json_error(StatusCode::INTERNAL_SERVER_ERROR, &error.to_string()),
    }
}

fn require_auth(state: &ApiState, headers: &HeaderMap) -> Option<Response> {
    let expected = state.settings.lethe_api_token.trim();
    let presented = presented_api_token(headers);
    if expected.is_empty() {
        return Some(json_error(
            StatusCode::SERVICE_UNAVAILABLE,
            "server misconfigured",
        ));
    }
    if presented != expected {
        return Some(json_error(StatusCode::UNAUTHORIZED, "unauthorized"));
    }
    None
}

fn presented_api_token(headers: &HeaderMap) -> String {
    let bearer = headers
        .get(header::AUTHORIZATION)
        .and_then(|value| value.to_str().ok())
        .unwrap_or_default()
        .trim();
    if bearer.to_ascii_lowercase().starts_with("bearer ") {
        return bearer[7..].trim().to_string();
    }
    headers
        .get("x-lethe-token")
        .and_then(|value| value.to_str().ok())
        .unwrap_or_default()
        .trim()
        .to_string()
}

fn json_error(status: StatusCode, message: &str) -> Response {
    (status, Json(json!({"error": message}))).into_response()
}

fn metadata_i64(metadata: &serde_json::Map<String, Value>, key: &str) -> Option<i64> {
    metadata.get(key).and_then(Value::as_i64)
}

fn available_provider_ids() -> Vec<String> {
    available_providers()
        .into_iter()
        .map(|provider| provider.provider)
        .collect()
}

fn model_provider<'a>(model: &'a str, configured_provider: &'a str) -> &'a str {
    provider_for_model(model)
        .or_else(|| (!configured_provider.trim().is_empty()).then_some(configured_provider))
        .unwrap_or("")
}

fn resolve_workspace_path(workspace_root: &Path, raw_path: &str) -> Option<PathBuf> {
    if raw_path.trim().is_empty() {
        return None;
    }
    let root = workspace_root.canonicalize().ok()?;
    let requested = Path::new(raw_path);
    let candidate = if requested.is_absolute() {
        requested.to_path_buf()
    } else {
        root.join(requested)
    };
    let resolved = candidate.canonicalize().ok()?;
    resolved.starts_with(&root).then_some(resolved)
}

async fn api_heartbeat_loop(state: ApiState) {
    let mut heartbeat = Heartbeat::new(HeartbeatConfig::from_settings(&state.settings));
    let mut limiter = ProactiveRateLimiter::from_settings(&state.settings);
    let mut interval = tokio::time::interval(Duration::from_secs(
        heartbeat.config().interval_seconds.max(1),
    ));
    interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);

    loop {
        interval.tick().await;
        if let Err(error) = process_api_heartbeat_once(&state, &mut heartbeat, &mut limiter).await {
            tracing::warn!(error = %error, "api heartbeat failed");
        }
    }
}

async fn process_api_heartbeat_once(
    state: &ApiState,
    heartbeat: &mut Heartbeat,
    limiter: &mut ProactiveRateLimiter,
) -> Result<()> {
    let prompts = PromptStore::new(&state.settings.workspace_dir, &state.settings.config_dir);
    let reminders = active_reminders(&state.settings)?;
    let prompt = heartbeat.trigger(&prompts, &reminders);
    let response = state
        .agent
        .chat_once_with_metadata(
            &prompt.message,
            message_metadata_value(
                MessageVisibility::Internal,
                MessageKind::Heartbeat,
                "api_heartbeat",
            ),
            &AgentOptions::default(),
        )
        .await?;
    let _background = state
        .agent
        .process_background_heartbeat_quiet(&prompt.message, &reminders)
        .await?;
    let outcome = heartbeat.finish_response(&response, None);

    if outcome.action == HeartbeatAction::Send
        && limiter.allowed()
        && state.send_proactive(&outcome.message).await
    {
        limiter.record();
    }
    Ok(())
}

fn active_reminders(settings: &Settings) -> Result<String> {
    let manager = TodoManager::open(settings.db_path.clone())?;
    let todos = manager.list(TodoFilter {
        include_completed: false,
        limit: 20,
        ..Default::default()
    })?;
    let reminders = todos
        .into_iter()
        .map(|todo| ActiveReminder {
            title: todo.title,
            priority: todo.priority.as_str().to_string(),
            due: todo.due_date,
        })
        .collect::<Vec<_>>();
    Ok(format_active_reminders(&reminders, 10))
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;
    use std::time::Duration;

    use axum::http::HeaderValue;
    use tempfile::tempdir;
    use tokio::sync::Notify;
    use tokio::time::{sleep, timeout};

    use super::*;
    use crate::config::RuntimeMode;

    fn test_settings(root: &std::path::Path) -> Settings {
        Settings {
            agent_name: "lethe".to_string(),
            mode: RuntimeMode::Cli,
            telegram_bot_token: String::new(),
            telegram_allowed_user_ids: vec![],
            telegram_transcription_enabled: true,
            lethe_api_token: "secret".to_string(),
            lethe_api_host: "127.0.0.1".to_string(),
            lethe_api_port: 8080,
            openrouter_api_key: String::new(),
            openai_api_key: String::new(),
            llm_model: "openai/gpt-5".to_string(),
            llm_model_aux: "openai/gpt-5-mini".to_string(),
            llm_provider: String::new(),
            llm_api_base: String::new(),
            llm_context_limit: 100_000,
            lethe_home: root.to_path_buf(),
            config_dir: root.join("config"),
            workspace_dir: root.join("workspace"),
            memory_dir: root.join("data").join("memory"),
            db_path: root.join("data").join("lethe.db"),
            credentials_dir: root.join("credentials"),
            cache_dir: root.join("cache"),
            logs_dir: root.join("logs"),
            notes_dir: root.join("workspace").join("notes"),
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
    fn presented_token_prefers_bearer_then_custom_header() {
        let mut headers = HeaderMap::new();
        headers.insert("x-lethe-token", HeaderValue::from_static("fallback"));
        assert_eq!(presented_api_token(&headers), "fallback");

        headers.insert(
            header::AUTHORIZATION,
            HeaderValue::from_static("Bearer secret"),
        );
        assert_eq!(presented_api_token(&headers), "secret");
    }

    #[test]
    fn workspace_file_resolution_rejects_traversal() {
        let tmp = tempdir().unwrap();
        let workspace = tmp.path().join("workspace");
        std::fs::create_dir_all(&workspace).unwrap();
        std::fs::write(workspace.join("ok.txt"), "ok").unwrap();
        std::fs::write(tmp.path().join("outside.txt"), "no").unwrap();

        assert_eq!(
            resolve_workspace_path(&workspace, "ok.txt")
                .unwrap()
                .file_name()
                .unwrap(),
            "ok.txt"
        );
        assert!(resolve_workspace_path(&workspace, "../outside.txt").is_none());
    }

    #[test]
    fn api_event_preserves_event_name_and_data() {
        let event = ApiEvent::new("text", json!({"content": "hello"}));
        assert_eq!(event.event, "text");
        assert_eq!(event.data["content"], "hello");
    }

    #[tokio::test]
    async fn client_tool_context_routes_transport_tools_to_session_events() {
        let tmp = tempdir().unwrap();
        let state = ApiState::from_settings(test_settings(tmp.path())).unwrap();
        let (sender, mut receiver) = mpsc::channel::<ApiEvent>(SESSION_QUEUE_DEPTH);
        let session_id = state.register_session(99, sender).await;
        let context = state
            .client_tool_context(&session_id, 99, Some(42))
            .await
            .unwrap();

        let message_payload = context.send_message("progress", "html");
        let message: Value = serde_json::from_str(&message_payload).unwrap();
        assert_eq!(message["success"], true);
        let event = receiver.recv().await.unwrap();
        assert_eq!(event.event, "text");
        assert_eq!(event.data["content"], "progress");
        assert_eq!(event.data["parse_mode"], "HTML");
        assert_eq!(event.data["message_id"], 1);

        let reaction_payload = context.react("✅", 0);
        let reaction: Value = serde_json::from_str(&reaction_payload).unwrap();
        assert_eq!(reaction["success"], true);
        let event = receiver.recv().await.unwrap();
        assert_eq!(event.event, "reaction");
        assert_eq!(event.data["emoji"], "✅");
        assert_eq!(event.data["message_id"], 42);
    }

    #[tokio::test]
    async fn api_stream_guard_cancels_current_session_on_drop() {
        let tmp = tempdir().unwrap();
        let state = ApiState::from_settings(test_settings(tmp.path())).unwrap();
        let (sender, _receiver) = mpsc::channel::<ApiEvent>(SESSION_QUEUE_DEPTH);
        let session_id = state.register_session(99, sender).await;
        let started = Arc::new(Notify::new());
        let callback: ProcessCallback = {
            let started = started.clone();
            Arc::new(move |_context: ProcessContext| {
                let started = started.clone();
                Box::pin(async move {
                    started.notify_waiters();
                    sleep(Duration::from_secs(60)).await;
                    Ok(())
                })
            })
        };

        state
            .conversations
            .add_message(99, 7, "hello", None, Some(callback))
            .await;
        timeout(Duration::from_secs(1), started.notified())
            .await
            .unwrap();
        assert!(state.conversations.is_processing(99).await);

        drop(ApiStreamGuard::new(state.clone(), 99, session_id.clone()));

        timeout(Duration::from_secs(1), async {
            loop {
                if !state.conversations.is_processing(99).await
                    && !state.session_matches_chat(99, &session_id).await
                {
                    break;
                }
                sleep(Duration::from_millis(5)).await;
            }
        })
        .await
        .unwrap();
    }
}
