use anyhow::{Result, anyhow};
use base64::{Engine as _, engine::general_purpose::STANDARD as BASE64_STANDARD};
use clap::{Parser, Subcommand};
use lethe::actor::ActorNamedEvent;
use lethe::agent::{Agent, AgentOptions};
use lethe::config::{RuntimeMode, Settings};
use lethe::conversation::transcription::{
    choose_transcription_provider, default_model_for_provider, infer_audio_format, transcribe_audio,
};
use lethe::conversation::{ConversationManager, ProcessCallback, ProcessContext};
use lethe::interfaces::telegram::{
    IncomingTelegramText, SharedTelegramTurnGuard, TelegramClient, TelegramToolContext,
    TelegramTurnGuard, VisibleTelegramChannel, image_mime_type_from_path, is_emoji_only_reply,
    split_telegram_messages,
};
use lethe::llm::prompts::{PromptSource, PromptStore};
use lethe::llm::{
    LlmAttachment, LlmMessage, LlmRouter, LlmRouterConfig, llm_auth_mode_for_settings,
};
use lethe::memory::BlockManager;
use lethe::memory::archival::ArchivalMemory;
use lethe::memory::message_metadata::{
    MessageKind, MessageVisibility, annotate_map, metadata_value as message_metadata_value,
};
use lethe::memory::messages::MessageHistory;
use lethe::memory::notes::NoteStore;
use lethe::memory::recall::{Hippocampus, HippocampusConfig};
use lethe::scheduler::curator::MemoryCurator;
use lethe::scheduler::heartbeat::{
    Heartbeat, HeartbeatAction, HeartbeatConfig, render_summary_prompt,
};
use lethe::scheduler::proactive::{ActiveReminder, ProactiveRateLimiter, format_active_reminders};
use lethe::store::MemoryStore;
use lethe::todos::{NewTodo, TodoFilter, TodoManager, TodoPriority, TodoStatus, TodoUpdate};
use lethe::tools::filesystem::FileTools;
use lethe::tools::registry::ToolRuntime;
use lethe::tools::shell::{DEFAULT_TIMEOUT_SECONDS, ShellTools};
use lethe::tools::web::WebTools;
use rand::Rng as _;
use serde_json::json;
use std::collections::HashSet;
use std::fs::{File, OpenOptions};
use std::future::Future;
use std::io::{self, Write};
use std::path::PathBuf;
use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::{Arc, Mutex};
use tracing_subscriber::EnvFilter;

#[derive(Debug, Parser)]
#[command(author, version, about)]
struct Cli {
    #[command(subcommand)]
    command: Option<Command>,
}

#[derive(Debug, Subcommand)]
enum Command {
    /// Validate the Rust runtime configuration and embedded prompt access.
    Check,
    /// Print a prompt template after workspace/config/embedded resolution.
    Prompt { name: String },
    /// Seed core memory block files from embedded defaults if they are missing.
    InitMemory,
    /// Inspect and initialize unified memory state.
    Memory {
        #[command(subcommand)]
        command: MemoryCommand,
    },
    /// Run local filesystem tools.
    Fs {
        #[command(subcommand)]
        command: FsCommand,
    },
    /// Run local shell tools.
    Sh {
        #[command(subcommand)]
        command: ShCommand,
    },
    /// Run web search and fetch tools.
    Web {
        #[command(subcommand)]
        command: WebCommand,
    },
    /// Transcribe a local audio file through the configured STT provider.
    Transcribe {
        file_path: String,
        #[arg(long)]
        mime_type: Option<String>,
    },
    /// Manage persistent todos stored in the local SQLite database.
    Todo {
        #[command(subcommand)]
        command: TodoCommand,
    },
    /// Manage persistent markdown notes.
    Note {
        #[command(subcommand)]
        command: NoteCommand,
    },
    /// Manage long-term archival memory.
    Archive {
        #[command(subcommand)]
        command: ArchiveCommand,
    },
    /// Manage durable conversation message history.
    Messages {
        #[command(subcommand)]
        command: MessageCommand,
    },
    /// Run the persisted local agent loop.
    Agent {
        #[command(subcommand)]
        command: AgentCommand,
    },
    /// Run heartbeat prompt and proactive-check helpers.
    Heartbeat {
        #[command(subcommand)]
        command: HeartbeatCommand,
    },
    /// Run Telegram transport commands.
    Telegram {
        #[command(subcommand)]
        command: TelegramCommand,
    },
    /// Run the authenticated HTTP API server.
    Api {
        #[arg(long)]
        port: Option<u16>,
    },
    /// Send a single user message through the configured universal LLM router.
    Chat {
        #[arg(short, long)]
        message: String,
        #[arg(long)]
        system: Option<String>,
        #[arg(long)]
        aux: bool,
    },
}

#[derive(Debug, Subcommand)]
enum FsCommand {
    /// Read a file with line numbers and truncation.
    Read {
        file_path: String,
        #[arg(long, default_value_t = 0)]
        offset: usize,
        #[arg(long, default_value_t = 0)]
        limit: usize,
    },
    /// Write content to a file, creating parents as needed.
    Write { file_path: String, content: String },
    /// Replace text in a file.
    Edit {
        file_path: String,
        old_string: String,
        #[arg(default_value = "")]
        new_string: String,
        #[arg(long)]
        replace_all: bool,
    },
    /// List a directory.
    List {
        #[arg(default_value = ".")]
        path: String,
        #[arg(long)]
        show_hidden: bool,
    },
    /// Search for files by glob pattern.
    Glob {
        pattern: String,
        #[arg(default_value = ".")]
        path: String,
    },
    /// Search file contents with a regex.
    Grep {
        pattern: String,
        #[arg(default_value = ".")]
        path: String,
        #[arg(long, default_value = "*")]
        file_pattern: String,
    },
}

#[derive(Debug, Subcommand)]
enum MemoryCommand {
    /// Initialize workspace memory directories and embedded block defaults.
    Init,
    /// Print memory store counts.
    Stats,
    /// Print combined prompt memory context.
    Context,
    /// Print stable and volatile prompt memory context separately.
    ContextSplit,
    /// Search recall memory and print an associative recall block.
    Recall {
        #[arg(short, long)]
        message: String,
    },
    /// Run deterministic memory curation and archival harvesting.
    Curate {
        #[arg(long)]
        force: bool,
    },
    /// List memory blocks.
    BlockList {
        #[arg(long)]
        include_hidden: bool,
    },
    /// Read one memory block.
    BlockRead { label: String },
    /// Create a memory block.
    BlockCreate {
        label: String,
        #[arg(default_value = "")]
        value: String,
        #[arg(long, default_value = "")]
        description: String,
        #[arg(long, default_value_t = lethe::memory::DEFAULT_BLOCK_LIMIT)]
        limit: usize,
        #[arg(long)]
        read_only: bool,
        #[arg(long)]
        hidden: bool,
    },
    /// Update a memory block value or description.
    BlockUpdate {
        label: String,
        #[arg(long)]
        value: Option<String>,
        #[arg(long)]
        description: Option<String>,
    },
    /// Append text to a memory block.
    BlockAppend { label: String, text: String },
    /// Replace the first matching string in a memory block.
    BlockReplace {
        label: String,
        old_string: String,
        new_string: String,
    },
    /// Delete a memory block.
    BlockDelete { label: String },
}

#[derive(Debug, Subcommand)]
enum ShCommand {
    /// Run a shell command with captured output or as a background process.
    Run {
        command: String,
        #[arg(long, default_value_t = DEFAULT_TIMEOUT_SECONDS)]
        timeout: u64,
        #[arg(long)]
        background: bool,
        #[arg(long)]
        pty: bool,
    },
    /// Print environment information visible to shell tools.
    Env,
    /// Check whether a command exists in PATH.
    Which { command_name: String },
}

#[derive(Debug, Subcommand)]
enum WebCommand {
    /// Print whether EXA_API_KEY is configured.
    Available,
    /// Search the web through Exa.
    Search {
        query: String,
        #[arg(long, default_value_t = 10)]
        num_results: usize,
        #[arg(long)]
        include_text: bool,
        #[arg(long, default_value = "")]
        category: String,
    },
    /// Fetch full page text through Exa.
    Fetch {
        url: String,
        #[arg(long, default_value_t = 5000)]
        max_chars: usize,
    },
}

#[derive(Debug, Subcommand)]
enum TodoCommand {
    /// Create a new todo.
    Create {
        title: String,
        #[arg(long)]
        description: Option<String>,
        #[arg(long, default_value = "normal")]
        priority: String,
        #[arg(long)]
        due_date: Option<String>,
        #[arg(long = "tag")]
        tags: Vec<String>,
        #[arg(long)]
        source: Option<String>,
    },
    /// List todos with optional status and priority filters.
    List {
        #[arg(long)]
        status: Option<String>,
        #[arg(long)]
        priority: Option<String>,
        #[arg(long)]
        include_completed: bool,
        #[arg(long, default_value_t = 50)]
        limit: usize,
    },
    /// Update an existing todo.
    Update {
        todo_id: i64,
        #[arg(long)]
        title: Option<String>,
        #[arg(long)]
        description: Option<String>,
        #[arg(long)]
        status: Option<String>,
        #[arg(long)]
        priority: Option<String>,
        #[arg(long)]
        due_date: Option<String>,
    },
    /// Mark a todo as completed.
    Complete { todo_id: i64 },
    /// Search active todos by title or description.
    Search {
        query: String,
        #[arg(long, default_value_t = 20)]
        limit: usize,
    },
    /// Check active todos due for a reminder.
    RemindCheck,
    /// Mark that a todo was just reminded.
    Reminded { todo_id: i64 },
    /// Delete a todo by ID.
    Delete { todo_id: i64 },
}

#[derive(Debug, Subcommand)]
enum NoteCommand {
    /// Create a persistent markdown note.
    Create {
        title: String,
        content: String,
        #[arg(long = "tag", value_delimiter = ',')]
        tags: Vec<String>,
        #[arg(long)]
        subdir: Option<String>,
    },
    /// List notes, optionally filtered by tags.
    List {
        #[arg(long = "tag", value_delimiter = ',')]
        tags: Vec<String>,
    },
    /// Search notes by title, tag, or body text.
    Search {
        query: String,
        #[arg(long = "tag", value_delimiter = ',')]
        tags: Vec<String>,
        #[arg(long, default_value_t = 5)]
        limit: usize,
    },
    /// Print all known note tags.
    Tags,
    /// Count markdown files in the note store.
    Reindex,
}

#[derive(Debug, Subcommand)]
enum ArchiveCommand {
    /// Add a long-term memory.
    Add {
        text: String,
        #[arg(long = "tag", value_delimiter = ',')]
        tags: Vec<String>,
        #[arg(long)]
        metadata: Option<String>,
    },
    /// Search long-term memories.
    Search {
        query: String,
        #[arg(long = "tag", value_delimiter = ',')]
        tags: Vec<String>,
        #[arg(long, default_value_t = 10)]
        limit: usize,
    },
    /// List recent long-term memories.
    Recent {
        #[arg(long, default_value_t = 50)]
        limit: usize,
    },
    /// Get a long-term memory by ID.
    Get { memory_id: String },
    /// Replace the tags on a long-term memory.
    Tag {
        memory_id: String,
        #[arg(long = "tag", value_delimiter = ',')]
        tags: Vec<String>,
    },
    /// Delete a long-term memory by ID.
    Delete { memory_id: String },
}

#[derive(Debug, Subcommand)]
enum MessageCommand {
    /// Add a message to durable history.
    Add {
        role: String,
        content: String,
        #[arg(long)]
        metadata: Option<String>,
    },
    /// Get recent messages in chronological order.
    Recent {
        #[arg(long, default_value_t = 20)]
        limit: usize,
    },
    /// Search messages by content and optional role.
    Search {
        query: String,
        #[arg(long)]
        role: Option<String>,
        #[arg(long, default_value_t = 20)]
        limit: usize,
    },
    /// Get recent messages for a role.
    Role {
        role: String,
        #[arg(long, default_value_t = 50)]
        limit: usize,
    },
    /// Get a message by ID.
    Get { message_id: String },
    /// Delete a message by ID.
    Delete { message_id: String },
    /// Remove stored tool outputs for recursive search tools.
    CleanupSearchResults {
        #[arg(long = "tool", value_delimiter = ',')]
        tools: Vec<String>,
    },
    /// Count stored messages.
    Count,
    /// Clear all stored messages.
    Clear,
    /// Build a recent context window under a character budget.
    Context {
        #[arg(long, default_value_t = 50)]
        max_messages: usize,
        #[arg(long, default_value_t = 50_000)]
        max_chars: usize,
    },
}

#[derive(Debug, Subcommand)]
enum AgentCommand {
    /// Send one message through the memory-backed agent loop.
    Chat {
        #[arg(short, long)]
        message: String,
        #[arg(long)]
        no_recall: bool,
    },
    /// Build and print the LLM messages for one agent turn without calling the model.
    Prepare {
        #[arg(short, long)]
        message: String,
        #[arg(long)]
        no_recall: bool,
    },
}

#[derive(Debug, Subcommand)]
enum HeartbeatCommand {
    /// Render the next heartbeat prompt without calling the model.
    Prompt {
        #[arg(long)]
        minimal: bool,
    },
    /// Process one heartbeat through the agent.
    Trigger {
        #[arg(long)]
        minimal: bool,
        #[arg(long)]
        summarize: bool,
        #[arg(long)]
        no_recall: bool,
    },
}

#[derive(Debug, Subcommand)]
enum TelegramCommand {
    /// Split a response into Telegram message chunks without sending it.
    Split { text: String },
    /// Poll Telegram once and process any received text messages.
    PollOnce {
        #[arg(long, default_value_t = 30)]
        timeout: u64,
        #[arg(long)]
        no_recall: bool,
    },
    /// Run Telegram long polling until interrupted.
    Run {
        #[arg(long, default_value_t = 30)]
        timeout: u64,
        #[arg(long)]
        no_recall: bool,
    },
}

#[derive(Clone, Debug, Eq, PartialEq)]
enum TelegramRuntimeCommand {
    Start,
    Help,
    Status,
    Stop,
    Heartbeat,
    Model(Option<String>),
    Aux(Option<String>),
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum TelegramModelSlot {
    Main,
    Aux,
}

#[derive(Debug)]
struct TelegramTurnInput {
    chat_id: i64,
    user_id: i64,
    message_id: i64,
    content: String,
    metadata: Option<serde_json::Value>,
    attachments: Vec<LlmAttachment>,
}

const TELEGRAM_TYPING_REFRESH_SECONDS: u64 = 3;
const TELEGRAM_ACTOR_UPDATE_POLL_SECONDS: u64 = 1;
const TELEGRAM_ACTOR_UPDATE_QUERY_LIMIT: usize = 50;

#[tokio::main]
async fn main() -> Result<()> {
    let settings = Settings::from_env();
    if let Some(log_path) = init_logging(&settings) {
        tracing::info!(path = %log_path.display(), "logging initialized");
    } else {
        tracing::info!("logging initialized without file output");
    }
    let cli = Cli::parse();
    let command = match cli.command {
        Some(command) => command,
        None => default_command_for_mode(&settings.mode),
    };
    match command {
        Command::Check => check(),
        Command::Prompt { name } => print_prompt(&name),
        Command::InitMemory => init_memory(),
        Command::Memory { command } => memory_command(command),
        Command::Fs { command } => fs_command(command),
        Command::Sh { command } => sh_command(command),
        Command::Web { command } => web_command(command),
        Command::Transcribe {
            file_path,
            mime_type,
        } => transcribe_command(&file_path, mime_type.as_deref()),
        Command::Todo { command } => todo_command(command),
        Command::Note { command } => note_command(command),
        Command::Archive { command } => archive_command(command),
        Command::Messages { command } => messages_command(command),
        Command::Agent { command } => agent_command(command).await,
        Command::Heartbeat { command } => heartbeat_command(command).await,
        Command::Telegram { command } => telegram_command(command).await,
        Command::Api { port } => api_command(port).await,
        Command::Chat {
            message,
            system,
            aux,
        } => chat(message, system, aux).await,
    }
}

#[derive(Clone)]
struct LogWriter {
    file: Arc<Mutex<File>>,
}

struct LogLineWriter {
    file: Arc<Mutex<File>>,
}

impl Write for LogLineWriter {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        let _ = io::stderr().write_all(buf);
        let mut file = self
            .file
            .lock()
            .map_err(|_| io::Error::other("log file lock poisoned"))?;
        file.write_all(buf)?;
        Ok(buf.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        let _ = io::stderr().flush();
        let mut file = self
            .file
            .lock()
            .map_err(|_| io::Error::other("log file lock poisoned"))?;
        file.flush()
    }
}

impl<'writer> tracing_subscriber::fmt::MakeWriter<'writer> for LogWriter {
    type Writer = LogLineWriter;

    fn make_writer(&'writer self) -> Self::Writer {
        LogLineWriter {
            file: self.file.clone(),
        }
    }
}

fn init_logging(settings: &Settings) -> Option<PathBuf> {
    let filter = || EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info"));
    if let Err(error) = std::fs::create_dir_all(&settings.logs_dir) {
        eprintln!(
            "logging_file_unavailable: cannot create {}: {error}",
            settings.logs_dir.display()
        );
        let _ = tracing_subscriber::fmt()
            .with_env_filter(filter())
            .with_target(true)
            .with_ansi(false)
            .try_init();
        return None;
    }
    let log_path = settings.logs_dir.join("lethe.log");
    let file = match OpenOptions::new().create(true).append(true).open(&log_path) {
        Ok(file) => file,
        Err(error) => {
            eprintln!(
                "logging_file_unavailable: cannot open {}: {error}",
                log_path.display()
            );
            let _ = tracing_subscriber::fmt()
                .with_env_filter(filter())
                .with_target(true)
                .with_ansi(false)
                .try_init();
            return None;
        }
    };

    if let Err(error) = tracing_subscriber::fmt()
        .with_env_filter(filter())
        .with_target(true)
        .with_ansi(false)
        .with_writer(LogWriter {
            file: Arc::new(Mutex::new(file)),
        })
        .try_init()
    {
        eprintln!("logging_setup_failed: {error}");
        return None;
    }
    Some(log_path)
}

fn default_command_for_mode(mode: &RuntimeMode) -> Command {
    match mode {
        RuntimeMode::Api => Command::Api { port: None },
        RuntimeMode::Telegram => Command::Telegram {
            command: TelegramCommand::Run {
                timeout: 30,
                no_recall: false,
            },
        },
        RuntimeMode::Cli => Command::Check,
    }
}

fn parse_telegram_runtime_command(text: &str) -> Option<TelegramRuntimeCommand> {
    let trimmed = text.trim();
    if !trimmed.starts_with('/') {
        return None;
    }

    let token = trimmed.split_whitespace().next()?;
    let command = token
        .trim_start_matches('/')
        .split('@')
        .next()
        .unwrap_or_default()
        .to_ascii_lowercase();
    let args = trimmed.get(token.len()..).unwrap_or_default().trim();
    let args = (!args.is_empty()).then(|| args.to_string());

    match command.as_str() {
        "start" => Some(TelegramRuntimeCommand::Start),
        "help" => Some(TelegramRuntimeCommand::Help),
        "status" => Some(TelegramRuntimeCommand::Status),
        "stop" => Some(TelegramRuntimeCommand::Stop),
        "heartbeat" => Some(TelegramRuntimeCommand::Heartbeat),
        "model" => Some(TelegramRuntimeCommand::Model(args)),
        "aux" => Some(TelegramRuntimeCommand::Aux(args)),
        _ => None,
    }
}

fn telegram_help_text(settings: &Settings) -> String {
    format!(
        "Hello. I'm {}.\n\nSend me any message and I'll help.\n\nCommands:\n/status - Check runtime status\n/stop - Cancel current processing when supported\n/heartbeat - Force a check-in\n/model [model-id] - Show or change the main model\n/aux [model-id] - Show or change the auxiliary model",
        settings.agent_name
    )
}

async fn telegram_status_text(agent: &Agent, settings: &Settings) -> Result<String> {
    let stats = agent.memory().stats()?;
    let router = agent.router_config()?;
    let actor_summary = if let Some(registry) = agent.actor_registry() {
        format!("enabled, active={}", registry.active_count().await)
    } else {
        "disabled".to_string()
    };

    Ok(format!(
        "Status: ready\nMemory: {} blocks, {} archival, {} messages, {} notes\nModel: {}\nAux model: {}\nHeartbeat: {}, interval={}s\nActors: {}",
        stats.memory_blocks,
        stats.archival_memories,
        stats.message_history,
        stats.notes,
        empty_marker(&router.model),
        empty_marker(&router.aux_model),
        if settings.heartbeat_enabled {
            "enabled"
        } else {
            "disabled"
        },
        settings.heartbeat_interval_seconds,
        actor_summary
    ))
}

async fn telegram_conversation_status_text(
    manager: Option<&ConversationManager>,
    chat_id: i64,
) -> String {
    let Some(manager) = manager else {
        return "Conversation: direct polling".to_string();
    };

    let pending = manager.pending_count(chat_id).await;
    let state = if manager.is_processing(chat_id).await {
        "processing"
    } else if manager.is_debouncing(chat_id).await {
        "debouncing"
    } else {
        "idle"
    };
    format!("Conversation: {state}, pending={pending}")
}

fn telegram_model_text(
    agent: &Agent,
    slot: TelegramModelSlot,
    requested_model: Option<&str>,
) -> Result<String> {
    let before = agent.router_config()?;
    let (label, current) = match slot {
        TelegramModelSlot::Main => ("Main model", before.model.as_str()),
        TelegramModelSlot::Aux => ("Aux model", before.aux_model.as_str()),
    };

    let Some(model) = requested_model
        .map(str::trim)
        .filter(|model| !model.is_empty())
    else {
        let command = match slot {
            TelegramModelSlot::Main => "/model",
            TelegramModelSlot::Aux => "/aux",
        };
        return Ok(format!(
            "{label}: {}\nUse `{command} <model-id>` to change it for this process.",
            empty_marker(current)
        ));
    };

    let changed = match slot {
        TelegramModelSlot::Main => agent.reconfigure_models(Some(model), None)?,
        TelegramModelSlot::Aux => agent.reconfigure_models(None, Some(model))?,
    };
    if changed
        .as_object()
        .is_some_and(|changes| changes.is_empty())
    {
        return Ok(format!("{label} unchanged: {}", empty_marker(model)));
    }

    let after = agent.router_config()?;
    let updated = match slot {
        TelegramModelSlot::Main => after.model.as_str(),
        TelegramModelSlot::Aux => after.aux_model.as_str(),
    };
    Ok(format!(
        "{label} updated: {} -> {}",
        empty_marker(current),
        empty_marker(updated)
    ))
}

fn telegram_text_metadata(
    incoming: &IncomingTelegramText,
) -> serde_json::Map<String, serde_json::Value> {
    let mut metadata = serde_json::Map::from_iter([
        ("source".to_string(), json!("telegram_text")),
        ("chat_id".to_string(), json!(incoming.chat_id)),
        ("user_id".to_string(), json!(incoming.user_id)),
        ("message_id".to_string(), json!(incoming.message_id)),
        ("update_id".to_string(), json!(incoming.update_id)),
    ]);
    annotate_map(
        &mut metadata,
        MessageVisibility::UserVisible,
        MessageKind::Chat,
        "telegram",
    );
    metadata
}

fn metadata_i64(metadata: &serde_json::Map<String, serde_json::Value>, key: &str) -> Option<i64> {
    metadata.get(key).and_then(serde_json::Value::as_i64)
}

fn metadata_value_from_map(
    metadata: &serde_json::Map<String, serde_json::Value>,
) -> Option<serde_json::Value> {
    (!metadata.is_empty()).then(|| serde_json::Value::Object(metadata.clone()))
}

fn metadata_map_from_value(
    metadata: Option<&serde_json::Value>,
) -> Option<serde_json::Map<String, serde_json::Value>> {
    match metadata {
        Some(serde_json::Value::Object(map)) => Some(map.clone()),
        Some(value) => Some(serde_json::Map::from_iter([(
            "metadata".to_string(),
            value.clone(),
        )])),
        None => None,
    }
}

async fn api_command(port: Option<u16>) -> Result<()> {
    let settings = Settings::from_env();
    let port = port.unwrap_or(settings.lethe_api_port);
    lethe::interfaces::api::serve(settings, port).await
}

fn prompt_store(settings: &Settings) -> PromptStore {
    PromptStore::new(&settings.workspace_dir, &settings.config_dir)
}

fn check() -> Result<()> {
    let settings = Settings::from_env();
    let store = prompt_store(&settings);
    let prompt = store.load("agent_instructions", "");

    println!("Lethe Rust runtime");
    println!("mode: {:?}", settings.mode);
    println!("home: {}", settings.lethe_home.display());
    println!("workspace: {}", settings.workspace_dir.display());
    println!("config: {}", settings.config_dir.display());
    println!("llm_model: {}", empty_marker(&settings.llm_model));
    println!(
        "llm_model_aux: {}",
        empty_marker(settings.effective_aux_model())
    );
    println!("llm_auth: {}", llm_auth_mode_for_settings(&settings));
    println!(
        "agent_instructions_source: {}",
        source_label(&prompt.source)
    );
    println!(
        "single_binary_prompt_fallback: {}",
        matches!(prompt.source, PromptSource::Embedded)
    );
    Ok(())
}

fn print_prompt(name: &str) -> Result<()> {
    let settings = Settings::from_env();
    let prompt = prompt_store(&settings).load(name, "");
    println!("{}", prompt.text);
    Ok(())
}

fn init_memory() -> Result<()> {
    let settings = Settings::from_env();
    let blocks_dir = settings.workspace_dir.join("memory");
    let manager = BlockManager::new(&blocks_dir)?;
    manager.init_embedded_defaults()?;
    let block_count = manager.list_blocks(true)?.len();
    println!("seeded_core_memory_blocks: {block_count}");
    println!("blocks_dir: {}", blocks_dir.display());
    Ok(())
}

fn fs_command(command: FsCommand) -> Result<()> {
    let settings = Settings::from_env();
    let tools = FileTools::new(settings.workspace_dir);
    let output = match command {
        FsCommand::Read {
            file_path,
            offset,
            limit,
        } => tools.read_file(&file_path, offset, limit),
        FsCommand::Write { file_path, content } => tools.write_file(&file_path, &content),
        FsCommand::Edit {
            file_path,
            old_string,
            new_string,
            replace_all,
        } => tools.edit_file(&file_path, &old_string, &new_string, replace_all),
        FsCommand::List { path, show_hidden } => tools.list_directory(&path, show_hidden),
        FsCommand::Glob { pattern, path } => tools.glob_search(&pattern, &path),
        FsCommand::Grep {
            pattern,
            path,
            file_pattern,
        } => tools.grep_search(&pattern, &path, &file_pattern),
    };
    println!("{output}");
    Ok(())
}

fn sh_command(command: ShCommand) -> Result<()> {
    let shell = ShellTools::from_env();
    let output = match command {
        ShCommand::Run {
            command,
            timeout,
            background,
            pty,
        } => shell.bash(&command, timeout, background, pty),
        ShCommand::Env => shell.get_environment_info(),
        ShCommand::Which { command_name } => shell.check_command_exists(&command_name),
    };
    println!("{output}");
    Ok(())
}

fn web_command(command: WebCommand) -> Result<()> {
    let settings = Settings::from_env();
    let tools = WebTools::new(settings.cache_dir);
    let output = match command {
        WebCommand::Available => WebTools::is_available().to_string(),
        WebCommand::Search {
            query,
            num_results,
            include_text,
            category,
        } => tools.web_search(&query, num_results, include_text, &category),
        WebCommand::Fetch { url, max_chars } => tools.fetch_webpage(&url, max_chars),
    };
    println!("{output}");
    Ok(())
}

fn transcribe_command(file_path: &str, mime_type: Option<&str>) -> Result<()> {
    let settings = Settings::from_env();
    let audio = std::fs::read(file_path)?;
    let audio_format = infer_audio_format(file_path, mime_type);
    let provider = choose_transcription_provider(&settings)?;
    let text = transcribe_audio(&audio, file_path, mime_type, &settings)?;
    println!("provider: {}", provider.as_str());
    println!("format: {audio_format}");
    println!();
    println!("{text}");
    Ok(())
}

fn todo_command(command: TodoCommand) -> Result<()> {
    let settings = Settings::from_env();
    let manager = TodoManager::open(settings.db_path)?;
    let output = match command {
        TodoCommand::Create {
            title,
            description,
            priority,
            due_date,
            tags,
            source,
        } => {
            let todo = NewTodo {
                title: title.clone(),
                description,
                priority: parse_priority(&priority)?,
                due_date,
                tags,
                source,
            };
            let todo_id = manager.create(todo)?;
            format!("Created todo #{todo_id}: {title}")
        }
        TodoCommand::List {
            status,
            priority,
            include_completed,
            limit,
        } => {
            let todos = manager.list(TodoFilter {
                status: parse_optional_status(status.as_deref())?,
                priority: parse_optional_priority(priority.as_deref())?,
                include_completed,
                limit,
            })?;
            TodoManager::format_list(&todos)
        }
        TodoCommand::Update {
            todo_id,
            title,
            description,
            status,
            priority,
            due_date,
        } => {
            let updated = manager.update(
                todo_id,
                TodoUpdate {
                    title,
                    description,
                    status: parse_optional_status(status.as_deref())?,
                    priority: parse_optional_priority(priority.as_deref())?,
                    due_date,
                },
            )?;
            if updated {
                format!("Updated todo #{todo_id}")
            } else {
                format!("Todo #{todo_id} not found")
            }
        }
        TodoCommand::Complete { todo_id } => {
            if manager.complete(todo_id)? {
                format!("Completed todo #{todo_id}")
            } else {
                format!("Todo #{todo_id} not found")
            }
        }
        TodoCommand::Search { query, limit } => {
            let todos = manager.search(&query, limit)?;
            TodoManager::format_search(&query, &todos)
        }
        TodoCommand::RemindCheck => {
            let todos = manager.due_reminders()?;
            TodoManager::format_due_reminders(&todos)
        }
        TodoCommand::Reminded { todo_id } => {
            if manager.mark_reminded(todo_id)? {
                format!("Marked todo #{todo_id} as reminded")
            } else {
                format!("Todo #{todo_id} not found")
            }
        }
        TodoCommand::Delete { todo_id } => {
            if manager.delete(todo_id)? {
                format!("Deleted todo #{todo_id}")
            } else {
                format!("Todo #{todo_id} not found")
            }
        }
    };
    println!("{output}");
    Ok(())
}

async fn telegram_command(command: TelegramCommand) -> Result<()> {
    match command {
        TelegramCommand::Split { text } => {
            println!(
                "{}",
                serde_json::to_string_pretty(&split_telegram_messages(&text))?
            );
            Ok(())
        }
        TelegramCommand::PollOnce { timeout, no_recall } => {
            let settings = Settings::from_env();
            let client = TelegramClient::new(
                settings.telegram_bot_token.clone(),
                settings.telegram_allowed_user_ids.clone(),
            )?;
            let agent = Agent::from_settings(settings.clone())?;
            let options = AgentOptions {
                use_hippocampus: !no_recall,
                ..Default::default()
            };
            let processed = process_telegram_once(
                TelegramPollContext {
                    client: &client,
                    agent: &agent,
                    settings: &settings,
                    options: &options,
                    conversation_manager: None,
                    process_callback: None,
                },
                None,
                timeout,
            )
            .await?;
            println!("processed_updates: {}", processed.1);
            Ok(())
        }
        TelegramCommand::Run { timeout, no_recall } => {
            let settings = Settings::from_env();
            let client = TelegramClient::new(
                settings.telegram_bot_token.clone(),
                settings.telegram_allowed_user_ids.clone(),
            )?;
            let agent = Agent::from_settings(settings.clone())?;
            let options = AgentOptions {
                use_hippocampus: !no_recall,
                ..Default::default()
            };
            let agent = Arc::new(agent);
            let conversation_manager = ConversationManager::new(
                std::time::Duration::from_secs_f64(settings.debounce_seconds.max(0.0)),
            );
            let process_callback = telegram_process_callback(
                client.clone(),
                agent.clone(),
                settings.clone(),
                options.clone(),
            );
            let mut offset = None;
            let mut target_chat_id = settings.telegram_allowed_user_ids.first().copied();
            let target_chat_id_state = Arc::new(AtomicI64::new(target_chat_id.unwrap_or(0)));
            let actor_update_monitor = spawn_telegram_actor_update_monitor(
                client.clone(),
                agent.clone(),
                settings.clone(),
                options.clone(),
                target_chat_id_state.clone(),
            );
            let mut heartbeat = Heartbeat::new(HeartbeatConfig::from_settings(&settings));
            let mut proactive_limiter = ProactiveRateLimiter::from_settings(&settings);
            let mut heartbeat_interval = tokio::time::interval(std::time::Duration::from_secs(
                heartbeat.config().interval_seconds.max(1),
            ));
            heartbeat_interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
            loop {
                tokio::select! {
                    _ = tokio::signal::ctrl_c() => {
                        println!("telegram_runner_stopped: interrupt");
                        break;
                    }
                    _ = heartbeat_interval.tick(), if heartbeat.config().enabled => {
                        if let Some(chat_id) = target_chat_id
                            && let Err(error) = process_heartbeat_once(
                                &client,
                                &agent,
                                &settings,
                                &mut heartbeat,
                                &mut proactive_limiter,
                                chat_id,
                                &options,
                            ).await {
                                tracing::warn!(error = %error, "heartbeat failed");
                                eprintln!("heartbeat_error: {error}");
                            }
                    }
                    result = process_telegram_once(
                        TelegramPollContext {
                            client: &client,
                            agent: &agent,
                            settings: &settings,
                            options: &options,
                            conversation_manager: Some(&conversation_manager),
                            process_callback: Some(process_callback.clone()),
                        },
                        offset,
                        timeout,
                    ) => {
                        let (next_offset, processed, last_chat_id) = match result {
                            Ok(value) => value,
                            Err(error) => {
                                tracing::error!(error = %error, "telegram polling failed");
                                return Err(error);
                            }
                        };
                        offset = next_offset;
                        if let Some(chat_id) = last_chat_id {
                            target_chat_id = Some(chat_id);
                            target_chat_id_state.store(chat_id, Ordering::SeqCst);
                            heartbeat.reset_idle_timer();
                        }
                        if processed > 0 {
                            println!("processed_updates: {processed}");
                        }
                    }
                }
            }
            if let Some(task) = actor_update_monitor {
                task.abort();
                let _ = task.await;
            }
            Ok(())
        }
    }
}

fn telegram_process_callback(
    client: TelegramClient,
    agent: Arc<Agent>,
    settings: Settings,
    options: AgentOptions,
) -> ProcessCallback {
    Arc::new(move |context: ProcessContext| {
        let client = client.clone();
        let agent = agent.clone();
        let settings = settings.clone();
        let options = options.clone();
        Box::pin(async move {
            if context.interrupt.is_interrupted() {
                return Ok(());
            }
            tracing::info!(
                chat_id = context.chat_id,
                user_id = context.user_id,
                attachments = context.attachments.len(),
                message_chars = context.message.chars().count(),
                "telegram conversation turn started"
            );

            let guard = Arc::new(Mutex::new(TelegramTurnGuard::new()));
            let runtime = ToolRuntime {
                telegram: Some(TelegramToolContext {
                    token: settings.telegram_bot_token.clone(),
                    chat_id: context.chat_id,
                    last_message_id: metadata_i64(&context.metadata, "message_id"),
                    guard: Some(guard.clone()),
                    dry_run: false,
                }),
                ..ToolRuntime::default()
            };
            let response = with_telegram_typing(
                &client,
                context.chat_id,
                agent.chat_once_with_attachments_metadata_runtime(
                    &context.message,
                    context.attachments.clone(),
                    metadata_value_from_map(&context.metadata),
                    &options,
                    runtime,
                ),
            )
            .await?;
            if !context.interrupt.is_interrupted() {
                tracing::info!(
                    chat_id = context.chat_id,
                    response_chars = response.chars().count(),
                    "telegram conversation turn completed"
                );
                send_guarded_telegram_final_response(&client, context.chat_id, &response, guard)
                    .await?;
            }
            Ok(())
        })
    })
}

async fn handle_telegram_runtime_command(
    client: &TelegramClient,
    agent: &Agent,
    settings: &Settings,
    incoming: &IncomingTelegramText,
    options: &AgentOptions,
    conversation_manager: Option<&ConversationManager>,
) -> Result<bool> {
    let Some(command) = parse_telegram_runtime_command(&incoming.text) else {
        return Ok(false);
    };

    match command {
        TelegramRuntimeCommand::Start | TelegramRuntimeCommand::Help => {
            client
                .send_message(incoming.chat_id, &telegram_help_text(settings))
                .await?;
        }
        TelegramRuntimeCommand::Status => {
            let mut status = telegram_status_text(agent, settings).await?;
            status.push('\n');
            status.push_str(
                &telegram_conversation_status_text(conversation_manager, incoming.chat_id).await,
            );
            client.send_message(incoming.chat_id, &status).await?;
        }
        TelegramRuntimeCommand::Stop => {
            let message = if let Some(manager) = conversation_manager {
                if manager.cancel(incoming.chat_id).await {
                    "Processing cancelled."
                } else {
                    "Nothing to cancel."
                }
            } else {
                "Nothing to cancel in this Rust polling mode."
            };
            client.send_message(incoming.chat_id, message).await?;
        }
        TelegramRuntimeCommand::Heartbeat => {
            client
                .send_message(incoming.chat_id, "Triggering heartbeat...")
                .await?;
            let mut heartbeat = Heartbeat::new(HeartbeatConfig::from_settings(settings));
            let mut proactive_limiter = ProactiveRateLimiter::from_settings(settings);
            if let Err(error) = process_heartbeat_once(
                client,
                agent,
                settings,
                &mut heartbeat,
                &mut proactive_limiter,
                incoming.chat_id,
                options,
            )
            .await
            {
                client
                    .send_message(incoming.chat_id, &format!("Heartbeat failed: {error}"))
                    .await?;
            }
        }
        TelegramRuntimeCommand::Model(model) => {
            let response = telegram_model_text(agent, TelegramModelSlot::Main, model.as_deref())?;
            client.send_message(incoming.chat_id, &response).await?;
        }
        TelegramRuntimeCommand::Aux(model) => {
            let response = telegram_model_text(agent, TelegramModelSlot::Aux, model.as_deref())?;
            client.send_message(incoming.chat_id, &response).await?;
        }
    }

    Ok(true)
}

async fn handle_telegram_turn(
    client: &TelegramClient,
    agent: &Agent,
    settings: &Settings,
    options: &AgentOptions,
    conversation_manager: Option<&ConversationManager>,
    process_callback: Option<ProcessCallback>,
    turn: TelegramTurnInput,
) -> Result<()> {
    tracing::info!(
        chat_id = turn.chat_id,
        user_id = turn.user_id,
        message_id = turn.message_id,
        attachments = turn.attachments.len(),
        message_chars = turn.content.chars().count(),
        "telegram turn started"
    );
    if let (Some(manager), Some(callback)) = (conversation_manager, process_callback) {
        manager
            .add_message_with_attachments(
                turn.chat_id,
                turn.user_id,
                turn.content,
                metadata_map_from_value(turn.metadata.as_ref()),
                turn.attachments,
                Some(callback),
            )
            .await;
        return Ok(());
    }

    let TelegramTurnInput {
        chat_id,
        user_id: _,
        message_id,
        content,
        metadata,
        attachments,
    } = turn;
    let guard = Arc::new(Mutex::new(TelegramTurnGuard::new()));
    let runtime = ToolRuntime {
        telegram: Some(TelegramToolContext {
            token: settings.telegram_bot_token.clone(),
            chat_id,
            last_message_id: Some(message_id),
            guard: Some(guard.clone()),
            dry_run: false,
        }),
        ..ToolRuntime::default()
    };
    let response = with_telegram_typing(
        client,
        chat_id,
        agent.chat_once_with_attachments_metadata_runtime(
            &content,
            attachments,
            metadata,
            options,
            runtime,
        ),
    )
    .await?;
    tracing::info!(
        chat_id,
        response_chars = response.chars().count(),
        "telegram turn completed"
    );
    send_guarded_telegram_final_response(client, chat_id, &response, guard).await?;
    Ok(())
}

fn start_telegram_typing(client: TelegramClient, chat_id: i64) -> tokio::task::JoinHandle<()> {
    tokio::spawn(async move {
        loop {
            match client.send_chat_action(chat_id, "typing").await {
                Ok(_) => {
                    tokio::time::sleep(std::time::Duration::from_secs(
                        TELEGRAM_TYPING_REFRESH_SECONDS,
                    ))
                    .await;
                }
                Err(error) => {
                    tracing::debug!(chat_id, error = %error, "telegram typing action failed");
                    break;
                }
            }
        }
    })
}

async fn with_telegram_typing<F, T>(client: &TelegramClient, chat_id: i64, future: F) -> T
where
    F: Future<Output = T>,
{
    let typing_task = start_telegram_typing(client.clone(), chat_id);
    let output = future.await;
    typing_task.abort();
    let _ = typing_task.await;
    output
}

fn spawn_telegram_actor_update_monitor(
    client: TelegramClient,
    agent: Arc<Agent>,
    settings: Settings,
    options: AgentOptions,
    target_chat_id: Arc<AtomicI64>,
) -> Option<tokio::task::JoinHandle<()>> {
    let actor_runtime = agent.actor_registry()?;
    let principal_actor_id = agent.principal_actor_id()?.to_string();
    Some(tokio::spawn(async move {
        let mut processed_event_ids = HashSet::<String>::new();
        let mut interval = tokio::time::interval(std::time::Duration::from_secs(
            TELEGRAM_ACTOR_UPDATE_POLL_SECONDS,
        ));
        interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        loop {
            interval.tick().await;
            let chat_id = target_chat_id.load(Ordering::SeqCst);
            if chat_id == 0 {
                continue;
            }
            let events = match actor_runtime
                .principal_task_update_events(
                    &principal_actor_id,
                    TELEGRAM_ACTOR_UPDATE_QUERY_LIMIT,
                )
                .await
            {
                Ok(events) => events,
                Err(error) => {
                    tracing::warn!(error = %error, "actor update query failed");
                    continue;
                }
            };
            let fresh_events = events
                .into_iter()
                .filter(|event| processed_event_ids.insert(event.event.id.clone()))
                .collect::<Vec<_>>();
            if fresh_events.is_empty() {
                continue;
            }
            if let Err(error) = process_telegram_actor_updates(
                &client,
                &agent,
                &settings,
                &options,
                chat_id,
                &fresh_events,
            )
            .await
            {
                tracing::warn!(error = %error, "actor update cortex turn failed");
            }
        }
    }))
}

async fn process_telegram_actor_updates(
    client: &TelegramClient,
    agent: &Agent,
    settings: &Settings,
    options: &AgentOptions,
    chat_id: i64,
    updates: &[ActorNamedEvent],
) -> Result<()> {
    let guard = Arc::new(Mutex::new(TelegramTurnGuard::new()));
    let runtime = ToolRuntime {
        telegram: Some(TelegramToolContext {
            token: settings.telegram_bot_token.clone(),
            chat_id,
            last_message_id: None,
            guard: Some(guard.clone()),
            dry_run: false,
        }),
        ..ToolRuntime::default()
    };
    let synthetic_message = actor_update_synthetic_message(updates);
    let response = with_telegram_typing(
        client,
        chat_id,
        agent.chat_once_with_attachments_metadata_runtime(
            &synthetic_message,
            Vec::new(),
            Some(message_metadata_value(
                MessageVisibility::Internal,
                MessageKind::ActorUpdate,
                "actor_update",
            )),
            options,
            runtime,
        ),
    )
    .await?;
    send_guarded_telegram_final_response(client, chat_id, &response, guard).await?;
    if !response.trim().is_empty() {
        agent.memory().messages.add(
            "assistant",
            &response,
            Some(json!({
                "source": "actor_update",
                "actor_event_ids": updates
                    .iter()
                    .map(|update| update.event.id.clone())
                    .collect::<Vec<_>>(),
            })),
        )?;
    }
    Ok(())
}

fn actor_update_synthetic_message(updates: &[ActorNamedEvent]) -> String {
    let terminal = updates
        .iter()
        .any(|update| actor_update_is_terminal(actor_update_kind(update)));
    let mut lines = vec![
        "[System: actor update]".to_string(),
        "One or more subagents sent task updates to your cortex inbox.".to_string(),
        "The authoritative details are in your actor inbox; review them before responding."
            .to_string(),
        String::new(),
        "<updates>".to_string(),
    ];
    for update in updates {
        let kind = actor_update_kind(update);
        let preview = update
            .event
            .payload
            .get("content_preview")
            .and_then(serde_json::Value::as_str)
            .unwrap_or_default()
            .trim();
        lines.push(format!(
            "- {} ({kind}): {}",
            update.actor_name,
            truncate_context_text(preview, 240)
        ));
    }
    lines.push("</updates>".to_string());
    lines.push(String::new());
    if terminal {
        lines.push(
            "At least one subagent finished or failed. Send the user a concise update with the result, blocker, or next action."
                .to_string(),
        );
    } else {
        lines.push(
            "These are progress updates. Send a brief user-visible status only if it adds value; otherwise return no text."
                .to_string(),
        );
    }
    lines.join("\n")
}

fn actor_update_kind(update: &ActorNamedEvent) -> &str {
    update
        .event
        .payload
        .get("intent")
        .or_else(|| update.event.payload.get("kind"))
        .and_then(serde_json::Value::as_str)
        .unwrap_or("progress")
}

fn actor_update_is_terminal(kind: &str) -> bool {
    matches!(kind, "done" | "failed" | "error" | "max_turns")
}

fn truncate_context_text(value: &str, limit: usize) -> String {
    let mut truncated = value.chars().take(limit).collect::<String>();
    if value.chars().count() > limit {
        truncated.push_str("...[truncated]");
    }
    truncated
}

struct TelegramPollContext<'a> {
    client: &'a TelegramClient,
    agent: &'a Agent,
    settings: &'a Settings,
    options: &'a AgentOptions,
    conversation_manager: Option<&'a ConversationManager>,
    process_callback: Option<ProcessCallback>,
}

async fn process_telegram_once(
    context: TelegramPollContext<'_>,
    offset: Option<i64>,
    timeout: u64,
) -> Result<(Option<i64>, usize, Option<i64>)> {
    let client = context.client;
    let agent = context.agent;
    let settings = context.settings;
    let options = context.options;
    let conversation_manager = context.conversation_manager;
    let process_callback = context.process_callback;
    let updates = client.get_updates(offset, timeout).await?;
    let mut next_offset = offset;
    let mut processed = 0;
    let mut last_chat_id = None;
    for update in updates {
        next_offset = Some(update.update_id + 1);

        if let Some(incoming) = update.incoming_text() {
            if !client.user_allowed(incoming.user_id) {
                continue;
            }
            last_chat_id = Some(incoming.chat_id);
            if handle_telegram_runtime_command(
                client,
                agent,
                settings,
                &incoming,
                options,
                conversation_manager,
            )
            .await?
            {
                processed += 1;
                continue;
            }
            let metadata = telegram_text_metadata(&incoming);
            handle_telegram_turn(
                client,
                agent,
                settings,
                options,
                conversation_manager,
                process_callback.clone(),
                TelegramTurnInput {
                    chat_id: incoming.chat_id,
                    user_id: incoming.user_id,
                    message_id: incoming.message_id,
                    content: incoming.text,
                    metadata: Some(serde_json::Value::Object(metadata)),
                    attachments: Vec::new(),
                },
            )
            .await?;
            processed += 1;
            continue;
        }

        if let Some(incoming) = update.incoming_photo() {
            if !client.user_allowed(incoming.user_id) {
                continue;
            }
            last_chat_id = Some(incoming.chat_id);
            let photo_result = async {
                let file = client.get_file(&incoming.file_id).await?;
                let image_bytes = client.download_file(&file.file_path).await?;
                let content_type = image_mime_type_from_path(&file.file_path).to_string();
                let attachment = LlmAttachment {
                    content_type: content_type.clone(),
                    base64_content: BASE64_STANDARD.encode(&image_bytes),
                    name: Some(incoming.attachment_name(&content_type)),
                };
                Ok::<(String, LlmAttachment), anyhow::Error>((content_type, attachment))
            }
            .await;

            let (content_type, attachment) = match photo_result {
                Ok(value) => value,
                Err(error) => {
                    client
                        .send_message(
                            incoming.chat_id,
                            &format!("Failed to process photo: {error}"),
                        )
                        .await?;
                    processed += 1;
                    continue;
                }
            };

            let content = incoming.content_text();
            let metadata = incoming.metadata(&content_type);
            handle_telegram_turn(
                client,
                agent,
                settings,
                options,
                conversation_manager,
                process_callback.clone(),
                TelegramTurnInput {
                    chat_id: incoming.chat_id,
                    user_id: incoming.user_id,
                    message_id: incoming.message_id,
                    content,
                    metadata: Some(metadata),
                    attachments: vec![attachment],
                },
            )
            .await?;
            processed += 1;
            continue;
        }

        if let Some(incoming) = update.incoming_audio() {
            if !client.user_allowed(incoming.user_id) {
                continue;
            }
            last_chat_id = Some(incoming.chat_id);
            if !settings.telegram_transcription_enabled {
                client
                    .send_message(incoming.chat_id, "Voice transcription is disabled.")
                    .await?;
                processed += 1;
                continue;
            }

            let transcript_result = async {
                let provider = choose_transcription_provider(settings)?;
                let model = if settings.transcription_model.trim().is_empty() {
                    default_model_for_provider(provider).to_string()
                } else {
                    settings.transcription_model.trim().to_string()
                };
                let file = client.get_file(&incoming.file_id).await?;
                let audio_bytes = client.download_file(&file.file_path).await?;
                let transcript = transcribe_audio(
                    &audio_bytes,
                    &incoming.file_name,
                    incoming.mime_type.as_deref(),
                    settings,
                )?;
                Ok::<(String, String, String), anyhow::Error>((
                    transcript,
                    provider.as_str().to_string(),
                    model,
                ))
            }
            .await;

            let (transcript, provider, model) = match transcript_result {
                Ok(value) => value,
                Err(error) => {
                    client
                        .send_message(
                            incoming.chat_id,
                            &format!("Failed to transcribe audio: {error}"),
                        )
                        .await?;
                    processed += 1;
                    continue;
                }
            };

            let content = incoming.content_with_transcript(&transcript);
            let metadata = incoming.metadata(&provider, &model);
            handle_telegram_turn(
                client,
                agent,
                settings,
                options,
                conversation_manager,
                process_callback.clone(),
                TelegramTurnInput {
                    chat_id: incoming.chat_id,
                    user_id: incoming.user_id,
                    message_id: incoming.message_id,
                    content,
                    metadata: Some(metadata),
                    attachments: Vec::new(),
                },
            )
            .await?;
            processed += 1;
            continue;
        }

        if let Some(incoming) = update.incoming_document() {
            if !client.user_allowed(incoming.user_id) {
                continue;
            }
            last_chat_id = Some(incoming.chat_id);
            let document_result = async {
                let file = client.get_file(&incoming.file_id).await?;
                let downloads_dir = settings.workspace_dir.join("Downloads");
                std::fs::create_dir_all(&downloads_dir)?;
                let file_path = downloads_dir.join(&incoming.file_name);
                let bytes = client.download_file(&file.file_path).await?;
                std::fs::write(&file_path, bytes)?;
                Ok::<std::path::PathBuf, anyhow::Error>(file_path)
            }
            .await;

            let file_path = match document_result {
                Ok(path) => path,
                Err(error) => {
                    client
                        .send_message(
                            incoming.chat_id,
                            &format!("Failed to download file: {error}"),
                        )
                        .await?;
                    processed += 1;
                    continue;
                }
            };

            let content = incoming.content_with_path(&file_path);
            let metadata = incoming.metadata(&file_path);
            handle_telegram_turn(
                client,
                agent,
                settings,
                options,
                conversation_manager,
                process_callback.clone(),
                TelegramTurnInput {
                    chat_id: incoming.chat_id,
                    user_id: incoming.user_id,
                    message_id: incoming.message_id,
                    content,
                    metadata: Some(metadata),
                    attachments: Vec::new(),
                },
            )
            .await?;
            processed += 1;
            continue;
        }

        if let Some(incoming) = update.incoming_sticker() {
            if !client.user_allowed(incoming.user_id) {
                continue;
            }
            last_chat_id = Some(incoming.chat_id);
            let content = incoming.content();
            let metadata = incoming.metadata();
            handle_telegram_turn(
                client,
                agent,
                settings,
                options,
                conversation_manager,
                process_callback.clone(),
                TelegramTurnInput {
                    chat_id: incoming.chat_id,
                    user_id: incoming.user_id,
                    message_id: incoming.message_id,
                    content,
                    metadata: Some(metadata),
                    attachments: Vec::new(),
                },
            )
            .await?;
            processed += 1;
            continue;
        }

        if let Some(reaction) = update.incoming_reaction() {
            if !client.user_allowed(reaction.user_id) {
                continue;
            }
            last_chat_id = Some(reaction.chat_id);
            agent
                .memory()
                .messages
                .add("user", &reaction.content(), Some(reaction.metadata()))?;
            processed += 1;
        }
    }
    Ok((next_offset, processed, last_chat_id))
}

async fn send_guarded_telegram_final_response(
    client: &TelegramClient,
    chat_id: i64,
    response: &str,
    guard: SharedTelegramTurnGuard,
) -> Result<()> {
    let (pending_reactions, channel) = {
        let mut guard = guard
            .lock()
            .map_err(|error| anyhow!("telegram turn guard poisoned: {error}"))?;
        (
            guard.drain_pending_reactions(),
            guard.choose_visible_channel(),
        )
    };

    if is_emoji_only_reply(response) && !pending_reactions.is_empty() {
        if channel == VisibleTelegramChannel::Reaction {
            let pending = &pending_reactions[0];
            if client
                .set_message_reaction(pending.chat_id, pending.message_id, &pending.emoji)
                .await
                .unwrap_or(false)
            {
                return Ok(());
            }
        }
        send_telegram_messages_with_delays(client, chat_id, split_telegram_messages(response))
            .await?;
        return Ok(());
    }

    for pending in pending_reactions {
        let _ = client
            .set_message_reaction(pending.chat_id, pending.message_id, &pending.emoji)
            .await
            .unwrap_or(false);
    }

    if !response.trim().is_empty() {
        send_telegram_messages_with_delays(client, chat_id, split_telegram_messages(response))
            .await?;
    }
    Ok(())
}

async fn send_telegram_messages_with_delays(
    client: &TelegramClient,
    chat_id: i64,
    chunks: Vec<String>,
) -> Result<()> {
    let total = chunks.len();
    for (index, chunk) in chunks.into_iter().enumerate() {
        client.send_message(chat_id, &chunk).await?;
        if index + 1 < total {
            let delay = telegram_inter_message_delay(&chunk);
            let _ = client.send_chat_action(chat_id, "typing").await;
            tokio::time::sleep(delay).await;
        }
    }
    Ok(())
}

fn telegram_inter_message_delay(chunk: &str) -> std::time::Duration {
    let chars = chunk.chars().count() as f64;
    let mut rng = rand::rng();
    let think = rng.random_range(0.35..=1.0);
    let typing = chars * 0.012;
    let jitter = rng.random_range(0.75..=1.15);
    let seconds = ((think + typing).min(4.0) * jitter).clamp(0.25, 4.6);
    std::time::Duration::from_secs_f64(seconds)
}

async fn process_heartbeat_once(
    client: &TelegramClient,
    agent: &Agent,
    settings: &Settings,
    heartbeat: &mut Heartbeat,
    proactive_limiter: &mut ProactiveRateLimiter,
    chat_id: i64,
    options: &AgentOptions,
) -> Result<()> {
    let prompts = prompt_store(settings);
    let reminders = active_reminders_text(settings)?;
    let prompt = heartbeat.trigger(&prompts, &reminders);
    let response = agent
        .chat_once_with_metadata(
            &prompt.message,
            message_metadata_value(
                MessageVisibility::Internal,
                MessageKind::Heartbeat,
                "heartbeat",
            ),
            options,
        )
        .await?;
    let outcome = heartbeat.finish_response(&response, None);
    let _background = agent
        .process_background_heartbeat_quiet(&prompt.message, &reminders)
        .await?;
    let mut messages = Vec::new();
    if outcome.action == HeartbeatAction::Send {
        messages.push(outcome.message);
    }
    for message in messages {
        if !proactive_limiter.allowed() {
            break;
        }
        send_telegram_messages_with_delays(client, chat_id, split_telegram_messages(&message))
            .await?;
        proactive_limiter.record();
    }
    Ok(())
}

async fn agent_command(command: AgentCommand) -> Result<()> {
    let settings = Settings::from_env();
    let agent = Agent::from_settings(settings)?;
    match command {
        AgentCommand::Chat { message, no_recall } => {
            let response = agent
                .chat_once(
                    &message,
                    &AgentOptions {
                        use_hippocampus: !no_recall,
                        ..Default::default()
                    },
                )
                .await?;
            println!("{response}");
        }
        AgentCommand::Prepare { message, no_recall } => {
            let turn = agent
                .prepare_turn_async(
                    &message,
                    &AgentOptions {
                        use_hippocampus: !no_recall,
                        ..Default::default()
                    },
                )
                .await?;
            println!("{}", serde_json::to_string_pretty(&turn)?);
        }
    }
    Ok(())
}

async fn heartbeat_command(command: HeartbeatCommand) -> Result<()> {
    let settings = Settings::from_env();
    let prompts = prompt_store(&settings);
    let reminders = active_reminders_text(&settings)?;
    match command {
        HeartbeatCommand::Prompt { minimal } => {
            let mut heartbeat = Heartbeat::new(heartbeat_config(&settings, minimal));
            let prompt = heartbeat.trigger(&prompts, &reminders);
            println!("{}", prompt.message);
        }
        HeartbeatCommand::Trigger {
            minimal,
            summarize,
            no_recall,
        } => {
            let mut heartbeat = Heartbeat::new(heartbeat_config(&settings, minimal));
            let prompt = heartbeat.trigger(&prompts, &reminders);
            let agent = Agent::from_settings(settings.clone())?;
            let response = agent
                .chat_once_with_metadata(
                    &prompt.message,
                    message_metadata_value(
                        MessageVisibility::Internal,
                        MessageKind::Heartbeat,
                        "heartbeat",
                    ),
                    &AgentOptions {
                        use_hippocampus: !no_recall,
                        ..Default::default()
                    },
                )
                .await?;
            let evaluated = if summarize && !response.trim().is_empty() {
                let router = LlmRouter::new(LlmRouterConfig::from_settings(&settings));
                Some(
                    router
                        .complete(
                            vec![LlmMessage::user(render_summary_prompt(&prompts, &response))],
                            true,
                        )
                        .await?,
                )
            } else {
                None
            };
            let outcome = heartbeat.finish_response(&response, evaluated.as_deref());
            let background = agent
                .process_background_heartbeat(&prompt.message, &reminders)
                .await?;
            println!(
                "{}",
                serde_json::to_string_pretty(&json!({
                    "prompt": {
                        "use_full_context": prompt.use_full_context,
                        "first_tick": prompt.first_tick,
                    },
                    "raw_response": response,
                    "evaluated": evaluated,
                    "outcome": outcome,
                    "background": background,
                }))?
            );
        }
    }
    Ok(())
}

fn heartbeat_config(settings: &Settings, minimal: bool) -> HeartbeatConfig {
    let mut config = HeartbeatConfig::from_settings(settings);
    if minimal {
        config.full_context_interval_seconds = 0;
    }
    config
}

fn active_reminders_text(settings: &Settings) -> Result<String> {
    let manager = TodoManager::open(settings.db_path.clone())?;
    let reminders = manager
        .due_reminders()?
        .into_iter()
        .map(|todo| ActiveReminder {
            title: todo.title,
            priority: todo.priority.as_str().to_string(),
            due: todo.due_date,
        })
        .collect::<Vec<_>>();
    Ok(format_active_reminders(&reminders, 10))
}

fn messages_command(command: MessageCommand) -> Result<()> {
    let settings = Settings::from_env();
    let store = MemoryStore::from_settings(&settings)?;
    let history = &store.messages;
    let output = match command {
        MessageCommand::Add {
            role,
            content,
            metadata,
        } => {
            let metadata = metadata.as_deref().map(serde_json::from_str).transpose()?;
            let message_id = history.add(&role, &content, metadata)?;
            format!("Stored message {message_id}")
        }
        MessageCommand::Recent { limit } => {
            let messages = history.get_recent(limit)?;
            MessageHistory::format_messages(&messages)
        }
        MessageCommand::Search { query, role, limit } => {
            let messages = store.search_messages(&query, limit, role.as_deref())?;
            MessageHistory::format_messages(&messages)
        }
        MessageCommand::Role { role, limit } => {
            let messages = history.get_by_role(&role, limit)?;
            MessageHistory::format_messages(&messages)
        }
        MessageCommand::Get { message_id } => match history.get(&message_id)? {
            Some(message) => MessageHistory::format_messages(&[message]),
            None => format!("Message {message_id} not found"),
        },
        MessageCommand::Delete { message_id } => {
            if history.delete(&message_id)? {
                format!("Deleted message {message_id}")
            } else {
                format!("Message {message_id} not found")
            }
        }
        MessageCommand::CleanupSearchResults { tools } => {
            let deleted = history.cleanup_search_results(optional_slice(&tools))?;
            format!("Cleaned up {deleted} search result message(s)")
        }
        MessageCommand::Count => format!("message_count: {}", history.count()?),
        MessageCommand::Clear => {
            let cleared = history.clear()?;
            format!("Cleared {cleared} message(s)")
        }
        MessageCommand::Context {
            max_messages,
            max_chars,
        } => {
            let messages = history.get_context_window(max_messages, max_chars)?;
            MessageHistory::format_messages(&messages)
        }
    };
    println!("{output}");
    Ok(())
}

fn memory_command(command: MemoryCommand) -> Result<()> {
    let settings = Settings::from_env();
    let store = MemoryStore::from_settings(&settings)?;
    match command {
        MemoryCommand::Init => {
            let stats = store.stats()?;
            println!("memory_initialized: true");
            println!("workspace: {}", store.workspace_dir().display());
            println!("memory_blocks: {}", stats.memory_blocks);
        }
        MemoryCommand::Stats => {
            let stats = store.stats()?;
            println!("memory_blocks: {}", stats.memory_blocks);
            println!("archival_memories: {}", stats.archival_memories);
            println!("message_history: {}", stats.message_history);
            println!("notes: {}", stats.notes);
        }
        MemoryCommand::Context => {
            println!("{}", store.get_context_for_prompt()?);
        }
        MemoryCommand::ContextSplit => {
            let (stable, volatile) = store.get_context_split()?;
            println!("<stable_context>\n{stable}\n</stable_context>");
            println!("\n<volatile_context>\n{volatile}\n</volatile_context>");
        }
        MemoryCommand::Recall { message } => {
            let recent = store.messages.get_recent(10)?;
            let recall = Hippocampus::new(HippocampusConfig {
                enabled: settings.hippocampus_enabled,
                ..Default::default()
            })
            .recall(&store, &message, &recent)?;
            match recall {
                Some(recall) => println!("{recall}"),
                None => println!("No associative memory recall."),
            }
        }
        MemoryCommand::Curate { force } => {
            let curator = MemoryCurator::new(settings.memory_dir.join("curator_state.json"));
            let stats = curator.run(&store, force)?;
            println!("{}", serde_json::to_string_pretty(&stats)?);
        }
        MemoryCommand::BlockList { include_hidden } => {
            let blocks = store.blocks.list_blocks(include_hidden)?;
            println!("{}", serde_json::to_string_pretty(&blocks)?);
        }
        MemoryCommand::BlockRead { label } => match store.blocks.get(&label)? {
            Some(block) => println!("{}", serde_json::to_string_pretty(&block)?),
            None => println!("Block '{label}' not found"),
        },
        MemoryCommand::BlockCreate {
            label,
            value,
            description,
            limit,
            read_only,
            hidden,
        } => {
            store
                .blocks
                .create(&label, &value, &description, limit, read_only, hidden)?;
            println!("Created block '{label}'");
        }
        MemoryCommand::BlockUpdate {
            label,
            value,
            description,
        } => {
            if store
                .blocks
                .update(&label, value.as_deref(), description.as_deref())?
            {
                println!("Updated block '{label}'");
            } else {
                println!("Block '{label}' not found");
            }
        }
        MemoryCommand::BlockAppend { label, text } => {
            if store.blocks.append(&label, &text)? {
                println!("Appended to block '{label}'");
            } else {
                println!("Block '{label}' not found");
            }
        }
        MemoryCommand::BlockReplace {
            label,
            old_string,
            new_string,
        } => {
            if store.blocks.str_replace(&label, &old_string, &new_string)? {
                println!("Replaced text in block '{label}'");
            } else {
                println!("Text not found in block '{label}'");
            }
        }
        MemoryCommand::BlockDelete { label } => {
            if store.blocks.delete(&label)? {
                println!("Deleted block '{label}'");
            } else {
                println!("Block '{label}' not found");
            }
        }
    }
    Ok(())
}

fn archive_command(command: ArchiveCommand) -> Result<()> {
    let settings = Settings::from_env();
    let store = MemoryStore::from_settings(&settings)?;
    let memory = &store.archival;
    let output = match command {
        ArchiveCommand::Add {
            text,
            tags,
            metadata,
        } => {
            let metadata = metadata.as_deref().map(serde_json::from_str).transpose()?;
            let memory_id = memory.add(&text, metadata, &tags)?;
            format!("Stored in archival memory (id: {memory_id})")
        }
        ArchiveCommand::Search { query, tags, limit } => {
            let results = store.search_archival(&query, limit, optional_slice(&tags))?;
            ArchivalMemory::format_entries(&results)
        }
        ArchiveCommand::Recent { limit } => {
            let entries = memory.list_recent(limit)?;
            ArchivalMemory::format_entries(&entries)
        }
        ArchiveCommand::Get { memory_id } => match memory.get(&memory_id)? {
            Some(entry) => ArchivalMemory::format_entries(&[entry]),
            None => format!("Archival memory {memory_id} not found"),
        },
        ArchiveCommand::Tag { memory_id, tags } => {
            if memory.update_tags(&memory_id, &tags)? {
                format!("Updated archival memory {memory_id} tags")
            } else {
                format!("Archival memory {memory_id} not found")
            }
        }
        ArchiveCommand::Delete { memory_id } => {
            if memory.delete(&memory_id)? {
                format!("Deleted archival memory {memory_id}")
            } else {
                format!("Archival memory {memory_id} not found")
            }
        }
    };
    println!("{output}");
    Ok(())
}

fn note_command(command: NoteCommand) -> Result<()> {
    let settings = Settings::from_env();
    let memory = MemoryStore::from_settings(&settings)?;
    let store = &memory.notes;
    let output = match command {
        NoteCommand::Create {
            title,
            content,
            tags,
            subdir,
        } => {
            let path = store.create(&title, &content, &tags, subdir.as_deref())?;
            format!("Note saved: {} (tags: {})", path.display(), tags.join(", "))
        }
        NoteCommand::List { tags } => {
            let notes = store.list_notes(optional_slice(&tags))?;
            let mut output = NoteStore::format_list(&notes);
            if notes.is_empty() && !tags.is_empty() {
                output.push_str(&format!(" (tags: {})", tags.join(",")));
            }
            output
        }
        NoteCommand::Search { query, tags, limit } => {
            let results = memory.search_notes(&query, optional_slice(&tags), limit)?;
            NoteStore::format_search(&query, &tags, &results)
        }
        NoteCommand::Tags => {
            let tags = store.all_tags()?;
            if tags.is_empty() {
                "No note tags found.".to_string()
            } else {
                tags.join("\n")
            }
        }
        NoteCommand::Reindex => {
            let count = store.reindex()?;
            format!("Reindexed {count} note(s)")
        }
    };
    println!("{output}");
    Ok(())
}

async fn chat(message: String, system: Option<String>, aux: bool) -> Result<()> {
    let settings = Settings::from_env();
    let config = LlmRouterConfig::from_settings(&settings);
    let router = LlmRouter::new(config);
    let system = system.unwrap_or_else(|| {
        prompt_store(&settings)
            .load("agent_instructions", "You are Lethe.")
            .text
    });

    let response = router
        .complete(
            vec![LlmMessage::system(system), LlmMessage::user(message)],
            aux,
        )
        .await?;
    println!("{response}");
    Ok(())
}

fn empty_marker(value: &str) -> &str {
    if value.trim().is_empty() {
        "<unset>"
    } else {
        value
    }
}

fn source_label(source: &PromptSource) -> String {
    match source {
        PromptSource::Workspace(path) => format!("workspace:{}", path.display()),
        PromptSource::Config(path) => format!("config:{}", path.display()),
        PromptSource::Embedded => "embedded".to_string(),
        PromptSource::Fallback => "fallback".to_string(),
    }
}

fn parse_optional_status(value: Option<&str>) -> Result<Option<TodoStatus>> {
    value
        .filter(|value| !value.trim().is_empty())
        .map(parse_status)
        .transpose()
}

fn parse_status(value: &str) -> Result<TodoStatus> {
    TodoStatus::parse(value).ok_or_else(|| {
        anyhow!(
            "invalid todo status '{}'; expected pending, in_progress, completed, deferred, or cancelled",
            value
        )
    })
}

fn parse_optional_priority(value: Option<&str>) -> Result<Option<TodoPriority>> {
    value
        .filter(|value| !value.trim().is_empty())
        .map(parse_priority)
        .transpose()
}

fn parse_priority(value: &str) -> Result<TodoPriority> {
    TodoPriority::parse(value).ok_or_else(|| {
        anyhow!(
            "invalid todo priority '{}'; expected low, normal, high, or urgent",
            value
        )
    })
}

fn optional_slice(values: &[String]) -> Option<&[String]> {
    if values.is_empty() {
        None
    } else {
        Some(values)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn test_settings(root: &std::path::Path) -> Settings {
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
    fn default_command_honors_runtime_mode() {
        assert!(matches!(
            default_command_for_mode(&RuntimeMode::Api),
            Command::Api { port: None }
        ));
        assert!(matches!(
            default_command_for_mode(&RuntimeMode::Telegram),
            Command::Telegram {
                command: TelegramCommand::Run {
                    timeout: 30,
                    no_recall: false
                }
            }
        ));
        assert!(matches!(
            default_command_for_mode(&RuntimeMode::Cli),
            Command::Check
        ));
    }

    #[test]
    fn telegram_runtime_command_parser_accepts_known_commands() {
        assert_eq!(
            parse_telegram_runtime_command("/status@LetheBot"),
            Some(TelegramRuntimeCommand::Status)
        );
        assert_eq!(
            parse_telegram_runtime_command("/model openai/gpt-5.1"),
            Some(TelegramRuntimeCommand::Model(Some(
                "openai/gpt-5.1".to_string()
            )))
        );
        assert_eq!(
            parse_telegram_runtime_command("/aux   anthropic/claude-sonnet-4.5"),
            Some(TelegramRuntimeCommand::Aux(Some(
                "anthropic/claude-sonnet-4.5".to_string()
            )))
        );
        assert_eq!(parse_telegram_runtime_command("hello"), None);
        assert_eq!(parse_telegram_runtime_command("/unknown"), None);
    }

    #[test]
    fn telegram_model_text_shows_and_updates_process_models() {
        let tmp = tempfile::tempdir().unwrap();
        let settings = test_settings(tmp.path());
        let agent = Agent::from_settings(settings).unwrap();

        let current = telegram_model_text(&agent, TelegramModelSlot::Main, None).unwrap();
        assert!(current.contains("openai/gpt-5"));

        let updated =
            telegram_model_text(&agent, TelegramModelSlot::Main, Some("openrouter/kimi-k2"))
                .unwrap();
        assert!(updated.contains("openai/gpt-5 -> openrouter/kimi-k2"));
        assert_eq!(agent.router_config().unwrap().model, "openrouter/kimi-k2");

        let updated_aux =
            telegram_model_text(&agent, TelegramModelSlot::Aux, Some("google/gemini-flash"))
                .unwrap();
        assert!(updated_aux.contains("openai/gpt-5-mini -> google/gemini-flash"));
        assert_eq!(
            agent.router_config().unwrap().aux_model,
            "google/gemini-flash"
        );
    }

    #[tokio::test]
    async fn telegram_status_text_includes_runtime_summary() {
        let tmp = tempfile::tempdir().unwrap();
        let settings = test_settings(tmp.path());
        let agent = Agent::from_settings(settings.clone()).unwrap();

        let status = telegram_status_text(&agent, &settings).await.unwrap();

        assert!(status.contains("Status: ready"));
        assert!(status.contains("Memory:"));
        assert!(status.contains("Model: openai/gpt-5"));
        assert!(status.contains("Heartbeat: enabled"));
        assert!(status.contains("Actors: enabled"));
    }

    #[test]
    fn telegram_text_metadata_preserves_transport_ids() {
        let incoming = IncomingTelegramText {
            update_id: 10,
            chat_id: 20,
            user_id: 30,
            message_id: 40,
            text: "hello".to_string(),
        };

        let metadata = telegram_text_metadata(&incoming);

        assert_eq!(metadata.get("source"), Some(&json!("telegram_text")));
        assert_eq!(metadata_i64(&metadata, "chat_id"), Some(20));
        assert_eq!(metadata_i64(&metadata, "user_id"), Some(30));
        assert_eq!(metadata_i64(&metadata, "message_id"), Some(40));
        assert_eq!(metadata_i64(&metadata, "update_id"), Some(10));
    }

    #[tokio::test]
    async fn telegram_conversation_status_reports_direct_and_managed_modes() {
        assert_eq!(
            telegram_conversation_status_text(None, 1).await,
            "Conversation: direct polling"
        );

        let manager = ConversationManager::new(std::time::Duration::from_millis(5));
        manager.add_message(1, 2, "queued", None, None).await;

        assert_eq!(
            telegram_conversation_status_text(Some(&manager), 1).await,
            "Conversation: idle, pending=1"
        );
    }
}
