# Lethe

[![License](https://img.shields.io/badge/license-MIT-green?style=flat-square)](LICENSE)
[![Rust](https://img.shields.io/badge/rust-1.88+-orange?style=flat-square&logo=rust)](https://www.rust-lang.org/)

Lethe is a long-running personal AI assistant with local memory, Telegram and HTTP/SSE transports, background reflection, and focused subagents.

This v1 branch is the Rust runtime. It builds as a single binary, uses `genai` as the universal LLM router, and intentionally does not include a web console.

## Architecture

```
                 Telegram / HTTP API
                        |
                        v
              Cortex: user-facing agent
        memory, tools, delegation, final replies
                        |
       +----------------+----------------+
       |                |                |
       v                v                v
 Hippocampus       Actor System     Notification Pipeline
 recall over       subagents,       scoring, gating,
 notes/archive/    registry,        and proactive
 conversations     event bus        transport output
       |                |
       v                +----------------+
 Memory Stack                            |
 markdown blocks,                       v
 notes, LanceDB archive,       DMN + heartbeat
 message history              background thought
                        |
                        v
                    Tool Registry
       files, shell/PTY, browser, web, Telegram/API transport
```

Core runtime pieces:

| Area | Rust modules | Responsibility |
|------|--------------|----------------|
| Agent/cortex | `src/agent.rs` | Prompt assembly, LLM calls, tool loop, and actor turn execution. |
| LLM routing | `src/llm.rs`, `src/models.rs` | `genai` client, OpenRouter/local API base normalization, model metadata. |
| Memory | `src/store.rs`, `src/memory.rs`, `src/notes.rs`, `src/archival.rs`, `src/messages.rs`, `src/semantic.rs` | Markdown memory blocks, compatible LanceDB recall tables, and SQLite todos. |
| Recall | `src/hippocampus.rs` | Hybrid lexical/vector recall over notes, archival memories, and conversation history. |
| Actors | `src/actor.rs`, `src/background.rs` | Resident Kameo actors, supervisor-owned state, mailbox/event routing, autonomous subagent wakeups, persistent DMN. |
| Notifications | `src/notification.rs`, `src/heartbeat.rs`, `src/runtime.rs` | Background candidate gating and proactive output limits. |
| Transports | `src/telegram.rs`, `src/api.rs`, `src/conversation.rs` | Telegram polling, HTTP/SSE API, debounce/cancel handling. |
| Tools | `src/tools/` | Filesystem, shell, PTY terminal, browser, image, web, memory, notes, todos, actors, transport tools. |

## Build

```bash
git clone https://github.com/atemerev/lethe.git
cd lethe
cp .env.example .env
cargo build --release
target/release/lethe check
```

The LanceDB dependency builds protobuf bindings, so the host needs `protoc` and protobuf headers available.

Native installer:

```bash
curl -fsSL https://lethe.gg/install | bash
~/.lethe/bin/lethe check
```

The installer downloads the latest GitHub binary release for the current platform when available. If no release asset matches, it falls back to a local Cargo build. Force source builds with `LETHE_INSTALL_FROM_SOURCE=1`.

Run tests:

```bash
cargo test
cargo build --release
```

Browser automation uses the external `agent-browser` CLI when browser tools are called.

## Running

CLI check is the default when `LETHE_MODE` is unset:

```bash
target/release/lethe check
```

Telegram long polling:

```bash
scripts/lethe-telegram-foreground
# or, without loading the foreground environment wrapper:
LETHE_MODE=telegram target/release/lethe
# or
target/release/lethe telegram run
```

HTTP API mode:

```bash
LETHE_MODE=api LETHE_API_TOKEN=change-me target/release/lethe
# or
target/release/lethe api --port 8080
```

API mode binds to `LETHE_API_HOST` (`127.0.0.1` by default). Use a reverse proxy for remote access.

## LLM Providers

Lethe routes chat through `genai`. The Rust runtime supports API-key based providers and OpenAI-compatible local servers:

| Provider | Auth | Example `LLM_MODEL` |
|----------|------|---------------------|
| Anthropic | `ANTHROPIC_API_KEY` or Claude subscription OAuth token file | `claude-opus-4-6` |
| OpenAI | `OPENAI_API_KEY` | `gpt-5.4` |
| OpenRouter | `OPENROUTER_API_KEY` | `openrouter/moonshotai/kimi-k2.6` |
| Local OpenAI-compatible | `LLM_API_BASE` + `OPENAI_API_KEY=local` | `openai/gemma-4-31B-it-Q8_0.gguf` |

`LLM_PROVIDER` is optional. It is useful when a model id does not carry a provider prefix, for example `LLM_PROVIDER=openrouter` with `LLM_MODEL=moonshotai/kimi-k2.6`.

`LLM_MODEL_AUX` defaults to the main model and is used for lightweight/background calls.

For Anthropic subscription/OAuth mode, Lethe reads `ANTHROPIC_AUTH_TOKEN` directly or a Claude token file from `LETHE_ANTHROPIC_OAUTH_TOKENS`. When that variable is unset, it falls back to `$CREDENTIALS_DIR/anthropic_oauth_tokens.json`.

## Configuration

Configuration is read from process environment, a local `.env`, and `$LETHE_HOME/config/.env`.

| Variable | Description | Default |
|----------|-------------|---------|
| `LETHE_MODE` | `cli`, `telegram`, or `api` | `cli` |
| `LETHE_HOME` | Runtime root | `~/.lethe` |
| `WORKSPACE_DIR` | Workspace directory | `$LETHE_HOME/workspace` |
| `MEMORY_DIR` | Memory data directory | `$LETHE_HOME/data/memory` |
| `DB_PATH` | SQLite todo database path | `$LETHE_HOME/data/lethe.db` |
| `LOGS_DIR` | Runtime log directory | `$LETHE_HOME/logs` |
| `TELEGRAM_BOT_TOKEN` | Bot token from BotFather | required for Telegram |
| `TELEGRAM_ALLOWED_USER_IDS` | Comma-separated allowlist | all users |
| `TELEGRAM_TRANSCRIPTION_ENABLED` | Transcribe Telegram audio/voice | `true` |
| `LETHE_API_TOKEN` | Bearer or `x-lethe-token` auth for API mode | required for API |
| `LETHE_API_HOST` | API bind address | `127.0.0.1` |
| `LETHE_API_PORT` | API port | `8080` |
| `LLM_PROVIDER` | Optional provider hint | auto |
| `LLM_MODEL` | Main model | required for chat |
| `LLM_MODEL_AUX` | Auxiliary model | main model |
| `LLM_API_BASE` | Custom OpenAI-compatible base URL | unset |
| `LLM_CONTEXT_LIMIT` | Context size hint | `100000` |
| `OPENROUTER_API_KEY` | OpenRouter key | unset |
| `ANTHROPIC_API_KEY` | Anthropic key | unset |
| `ANTHROPIC_AUTH_TOKEN` | Optional Anthropic OAuth access token | unset |
| `LETHE_ANTHROPIC_OAUTH_TOKENS` | Optional Anthropic OAuth token file or directory | `$CREDENTIALS_DIR/anthropic_oauth_tokens.json` |
| `OPENAI_API_KEY` | OpenAI/local-compatible key | unset |
| `EXA_API_KEY` | Exa search/fetch tools | unset |
| `LETHE_SEMANTIC_SEARCH_ENABLED` | Enable LanceDB vector search | `true` |
| `LETHE_EMBEDDING_PROVIDER` | `fastembed` or `hash` | `fastembed` |
| `LETHE_EMBEDDING_MODEL` | FastEmbed model id | `Snowflake/snowflake-arctic-embed-m-v2.0` |
| `ACTORS_ENABLED` | Enable actor/subagent system | `true` |
| `HIPPOCAMPUS_ENABLED` | Enable associative recall | `true` |
| `CURATOR_ENABLED` | Enable memory curator | `true` |
| `HEARTBEAT_ENABLED` | Enable proactive heartbeat loop | `true` |
| `HEARTBEAT_INTERVAL` | Heartbeat interval seconds | `3600` |
| `PROACTIVE_MAX_PER_DAY` | Proactive message daily limit | `4` |
| `PROACTIVE_COOLDOWN_MINUTES` | Minimum spacing for proactive messages | `60` |
| `TRANSCRIPTION_PROVIDER` | `auto`, `openrouter`, `openai`, or `local` | `auto` |
| `TRANSCRIPTION_MODEL` | STT model override | provider default |
| `TRANSCRIPTION_LANGUAGE` | Optional language hint | auto |
| `TRANSCRIPTION_LOCAL_COMMAND` | Local Whisper command | `whisper` |

## Memory

Lethe stores runtime state under the workspace and data directories:

- `workspace/memory/identity.md` -- persona and identity, user-editable.
- `workspace/memory/human.md` -- facts about the user.
- `workspace/memory/project.md` -- current project/context.
- `workspace/notes/` -- tagged markdown notes.
- `$MEMORY_DIR/lancedb/` -- LanceDB tables `archival_memory`, `message_history`, and `notes` using the existing compatible schema.
- SQLite database at `$DB_PATH` -- todos.

Core memory block defaults and prompt templates are embedded into the binary, so `lethe check` and first startup work without copying prompt files into the workspace.

## Logging

Lethe writes structured runtime logs to `$LOGS_DIR/lethe.log` and mirrors them to stderr. The default level is `info`; override it with `RUST_LOG`, for example:

```bash
RUST_LOG=debug scripts/lethe-telegram-foreground
tail -f ~/.lethe/logs/lethe.log
```

Telegram turns, LLM responses, tool calls, tool results, heartbeat failures, and background actor update relay failures are logged for post-mortem debugging.

Full LLM request/response dumps are opt-in because they contain prompts, memory, tool schemas, tool results, and attachments:

```bash
LLM_DEBUG=true scripts/lethe-telegram-foreground
ls ~/.lethe/logs/llm/
```

Override the dump directory with `LLM_DEBUG_DIR`.

## API

All API routes require `Authorization: Bearer <LETHE_API_TOKEN>` or `x-lethe-token`.

| Route | Method | Purpose |
|-------|--------|---------|
| `/health` | `GET` | Readiness check. |
| `/chat` | `POST` | Send a user message and receive SSE response events. |
| `/events` | `GET` | Subscribe to proactive SSE events. |
| `/cancel` | `POST` | Cancel active work for a chat. |
| `/configure` | `POST` | Store user metadata in memory. |
| `/model` | `GET`/`POST` | Inspect or update main/aux model ids. |
| `/file?path=...` | `GET` | Serve a workspace file. |

## Local llama.cpp Example

Start an OpenAI-compatible server:

```bash
./build/bin/llama-server \
  --model /path/to/gemma-4-31B-it-Q8_0.gguf \
  --host 0.0.0.0 --port 8090 \
  --ctx-size 98304 \
  --jinja
```

Configure Lethe:

```bash
LLM_PROVIDER=openai
LLM_MODEL=openai/gemma-4-31B-it-Q8_0.gguf
LLM_API_BASE=http://localhost:8090/v1
OPENAI_API_KEY=local
LLM_CONTEXT_LIMIT=96000
```

## Development

```bash
cargo fmt --check
cargo test
cargo build --release
```

Build a local release archive:

```bash
cargo build --release
scripts/package-release
ls dist/
```

Tagged pushes (`v*`) build GitHub release assets named `lethe-<target>.tar.gz`; `install.sh` and `update.sh` consume those assets from the latest release.

Useful smoke checks:

```bash
target/release/lethe check
target/release/lethe telegram split "hello from lethe"
```

## License

MIT
