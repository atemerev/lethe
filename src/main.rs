use anyhow::Result;
use clap::{Parser, Subcommand};
use lethe::config::{RuntimeMode, Settings};
use lethe::tools::shell::DEFAULT_TIMEOUT_SECONDS;
use std::fs::{File, OpenOptions};
use std::io::{self, Write};
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use tracing_subscriber::EnvFilter;

mod cli;

#[derive(Debug, Parser)]
#[command(author, version, about)]
struct Cli {
    #[command(subcommand)]
    command: Option<Command>,
}

#[derive(Debug, Subcommand)]
enum Command {
    /// Interactive setup: pick provider + model + API key, write
    /// ~/.lethe/config/.env, seed the workspace, run a smoke test.
    Init,
    /// Sign in to an LLM provider. Subscription paths (ChatGPT
    /// Plus/Pro, Claude Pro/Max) store OAuth tokens under
    /// `~/.lethe/credentials/`; API-key paths (OpenRouter, plus the
    /// API-key alternative on the OAuth-capable providers) write the
    /// key to `~/.lethe/config/.env`. Either way, `LLM_PROVIDER` and
    /// the model defaults are aligned to the chosen provider.
    Login {
        #[command(subcommand)]
        command: LoginCommand,
    },
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
        command: cli::telegram_loop::TelegramCommand,
    },
    /// Run the authenticated HTTP API server.
    Api {
        #[arg(long)]
        port: Option<u16>,
    },
    /// Launch the terminal UI. Defaults to the local `lethe api` instance
    /// (port from settings) using `LETHE_API_TOKEN` for auth.
    Tui {
        /// Base URL of the lethe API. Defaults to `http://{api.host}:{api.port}`.
        #[arg(long)]
        url: Option<String>,
        /// Bearer token. Falls back to settings (which loads `LETHE_API_TOKEN`).
        #[arg(long, env = "LETHE_API_TOKEN")]
        token: Option<String>,
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
    /// Pack workspace, agent state (memory + history), and the `.env`
    /// file into a single tar.gz archive.
    Backup {
        #[arg(short, long)]
        output: Option<String>,
    },
    /// Restore a `lethe backup` archive. Asks before overwriting an
    /// existing workspace and before overwriting an existing `.env`.
    Restore {
        archive: String,
        #[arg(long)]
        yes: bool,
    },
}

#[derive(Debug, Subcommand)]
pub enum LoginCommand {
    /// Sign in to OpenAI. Prompts for subscription (ChatGPT Plus/Pro
    /// device-code OAuth, default) or API key.
    Openai,
    /// Sign in to Anthropic. Prompts for subscription (Claude Pro/Max
    /// browser-PKCE OAuth, default) or API key.
    Anthropic,
    /// Sign in to OpenRouter (API key only — no subscription path).
    Openrouter,
}

#[derive(Debug, Subcommand)]
pub enum FsCommand {
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
pub enum MemoryCommand {
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
pub enum ShCommand {
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
pub enum WebCommand {
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
pub enum TodoCommand {
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
pub enum NoteCommand {
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
pub enum ArchiveCommand {
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
pub enum MessageCommand {
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
pub enum AgentCommand {
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
pub enum HeartbeatCommand {
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
    use cli::handlers as h;
    match command {
        Command::Init => cli::init::run().await,
        Command::Login { command } => match command {
            LoginCommand::Openai => {
                if lethe::llm::oauth_env::prompt_subscription_or_api(
                    "OpenAI",
                    "ChatGPT Plus/Pro",
                )? {
                    lethe::llm::openai_oauth::run_device_login().await?;
                    let (main, aux) =
                        lethe::llm::oauth_env::prompt_provider_models("openai")?;
                    lethe::llm::oauth_env::update_env_after_oauth_login(
                        "openai",
                        main.as_deref(),
                        aux.as_deref(),
                        Some("OPENAI_API_KEY"),
                    )
                } else {
                    lethe::llm::oauth_env::run_api_key_login(
                        "openai",
                        "OPENAI_API_KEY",
                        "Paste OPENAI_API_KEY (https://platform.openai.com/api-keys): ",
                    )
                }
            }
            LoginCommand::Anthropic => {
                if lethe::llm::oauth_env::prompt_subscription_or_api(
                    "Anthropic",
                    "Claude Pro/Max",
                )? {
                    lethe::llm::anthropic_oauth::run_device_login().await?;
                    let (main, aux) =
                        lethe::llm::oauth_env::prompt_provider_models("anthropic")?;
                    lethe::llm::oauth_env::update_env_after_oauth_login(
                        "anthropic",
                        main.as_deref(),
                        aux.as_deref(),
                        Some("ANTHROPIC_API_KEY"),
                    )
                } else {
                    lethe::llm::oauth_env::run_api_key_login(
                        "anthropic",
                        "ANTHROPIC_API_KEY",
                        "Paste ANTHROPIC_API_KEY (https://console.anthropic.com/settings/keys): ",
                    )
                }
            }
            LoginCommand::Openrouter => lethe::llm::oauth_env::run_api_key_login(
                "openrouter",
                "OPENROUTER_API_KEY",
                "Paste OPENROUTER_API_KEY (https://openrouter.ai/keys): ",
            ),
        },
        Command::Check => h::check().await,
        Command::Prompt { name } => h::print_prompt(&name),
        Command::InitMemory => h::init_memory(),
        Command::Memory { command } => h::memory_command(command).await,
        Command::Fs { command } => h::fs_command(command),
        Command::Sh { command } => h::sh_command(command),
        Command::Web { command } => h::web_command(command),
        Command::Transcribe {
            file_path,
            mime_type,
        } => h::transcribe_command(&file_path, mime_type.as_deref()),
        Command::Todo { command } => h::todo_command(command),
        Command::Note { command } => h::note_command(command),
        Command::Archive { command } => h::archive_command(command),
        Command::Messages { command } => h::messages_command(command),
        Command::Agent { command } => h::agent_command(command).await,
        Command::Heartbeat { command } => h::heartbeat_command(command).await,
        Command::Telegram { command } => cli::telegram_loop::telegram_command(command).await,
        Command::Api { port } => h::api_command(port).await,
        Command::Tui { url, token } => h::tui_command(url, token).await,
        Command::Chat {
            message,
            system,
            aux,
        } => h::chat(message, system, aux).await,
        Command::Backup { output } => cli::backup::backup(output),
        Command::Restore { archive, yes } => cli::backup::restore(archive, yes),
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
    if let Err(error) = std::fs::create_dir_all(&settings.paths.logs_dir) {
        eprintln!(
            "logging_file_unavailable: cannot create {}: {error}",
            settings.paths.logs_dir.display()
        );
        let _ = tracing_subscriber::fmt()
            .with_env_filter(filter())
            .with_target(true)
            .with_ansi(false)
            .try_init();
        return None;
    }
    let log_path = settings.paths.logs_dir.join("lethe.log");
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
            command: cli::telegram_loop::TelegramCommand::Run {
                timeout: 30,
                no_recall: false,
            },
        },
        RuntimeMode::Cli => Command::Check,
    }
}



#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_command_honors_runtime_mode() {
        assert!(matches!(
            default_command_for_mode(&RuntimeMode::Api),
            Command::Api { port: None }
        ));
        assert!(matches!(
            default_command_for_mode(&RuntimeMode::Telegram),
            Command::Telegram {
                command: cli::telegram_loop::TelegramCommand::Run {
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
}
