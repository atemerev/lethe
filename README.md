# Lethe

[![Release](https://img.shields.io/badge/release-v0.6.1-blue?style=flat-square)](https://github.com/atemerev/lethe/releases/tag/v0.6.1)
[![License](https://img.shields.io/badge/license-MIT-green?style=flat-square)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11+-blue?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![Telegram](https://img.shields.io/badge/Telegram-bot-blue?style=flat-square&logo=telegram)](https://telegram.org)

Autonomous executive assistant with persistent memory and a multi-agent architecture.

Lethe is a 24/7 AI assistant that you communicate with via Telegram. It remembers everything — your preferences, your projects, conversations from months ago. The more you use it, the more useful it becomes.

**Local-first architecture** — no cloud dependencies except the LLM API.

## Architecture

```
User (Telegram) ─── Cortex (conscious executive, coordinator)
                        │
              ┌─────────┼──────────┐
              ↓         ↓          ↓
            DMN      Worker     Worker      (subagents)
        (background)  (task)    (task)
              │
              ↓
         Memory (LanceDB)
         ├── blocks (config/blocks/)
         ├── archival (vector + FTS)
         └── messages (conversation history)
```

### Actor Model

Lethe uses a neuroscience-inspired multi-agent system:

| Actor | Role | Tools |
|-------|------|-------|
| **Cortex** | Conscious executive layer. The ONLY agent that talks to the user. Pure coordinator — delegates ALL work to subagents. | Actor management, memory, Telegram |
| **DMN** (Default Mode Network) | Persistent background thinker. Runs every 15 min. Scans goals, reorganizes memory, writes reflections, surfaces urgent items. | File I/O, memory, search |
| **Subagents** | Spawned on demand for specific tasks. Report results back to cortex. Cannot access Telegram — only actor messaging. | Bash, file I/O, web search, browser |

### Prompt Caching

Aggressive prompt caching minimizes costs across all providers:

| Provider | Writes | Reads | Setup |
|----------|--------|-------|-------|
| Kimi K2.5 (Moonshot) | FREE | FREE | Automatic |
| DeepSeek | 1x | 0.1x | Automatic |
| Gemini 2.5 | ~free | 0.25x | Implicit |
| Anthropic Claude | 1.25x (5m) / 2x (1h) | 0.1x | Explicit `cache_control` |

For Anthropic, the cache layout is: tools (1h) → system prompt (1h) → memory blocks (5m) → messages (5m) → summary (uncached).

## Core Dependencies

| Component | Library | Purpose |
|-----------|---------|---------|
| **LLM** | [litellm](https://github.com/BerriAI/litellm) | Multi-provider LLM API (OpenRouter, Anthropic, OpenAI) |
| **Vector DB** | [LanceDB](https://lancedb.com/) | Local vector + full-text search for memory |
| **Embeddings** | [sentence-transformers](https://sbert.net/) | Local embeddings (all-MiniLM-L6-v2, CPU-only) |
| **Telegram** | [aiogram](https://aiogram.dev/) | Async Telegram bot framework |
| **Console** | [NiceGUI](https://nicegui.io/) | Mind state visualization dashboard |

All data stays local. Only LLM API calls leave your machine.

## Quick Start

### 1. One-Line Install

```bash
curl -fsSL https://lethe.gg/install | bash
```

The installer will prompt for:
- LLM provider (OpenRouter, Anthropic, or OpenAI)
- API key
- Telegram bot token

### 2. Manual Install

```bash
git clone https://github.com/atemerev/lethe.git
cd lethe
uv sync
cp .env.example .env
# Edit .env with your credentials
uv run lethe
```

### 3. Update

```bash
curl -fsSL https://lethe.gg/update | bash
```

### LLM Providers

| Provider | Env Variable | Default Model |
|----------|--------------|---------------|
| OpenRouter | `OPENROUTER_API_KEY` | `moonshotai/kimi-k2.5-0127` |
| Anthropic (API key) | `ANTHROPIC_API_KEY` | `claude-opus-4-6` |
| Anthropic (subscription) | `ANTHROPIC_AUTH_TOKEN` | `claude-opus-4-6` |
| OpenAI | `OPENAI_API_KEY` | `gpt-5.2` |

Set `LLM_PROVIDER` to force a specific provider, or let it auto-detect from available API keys.

**Multi-model support**: Set `LLM_MODEL_AUX` for a cheaper model used in summarization (e.g., `claude-haiku-4-5-20251001`).

### Anthropic OAuth (Claude Pro/Max Subscription)

Use your Claude Pro ($20/mo) or Max ($100-200/mo) subscription instead of pay-per-token API credits. This bypasses litellm and makes direct API calls with Claude Code-compatible request format.

**Option A: Interactive login (recommended)**

```bash
uv run lethe oauth-login
```

Opens your browser to sign in with your Anthropic account. Tokens are saved to `~/.lethe/oauth_tokens.json` with automatic refresh.

**Option B: Manual token**

If you already have an OAuth access token (e.g. from `claude setup-token`):

```bash
# In your .env
ANTHROPIC_AUTH_TOKEN=sk-ant-oat01-...
```

Note: tokens from `ANTHROPIC_AUTH_TOKEN` cannot be refreshed automatically. Use `oauth-login` for persistent sessions.

**Priority**: If both `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_API_KEY` are set, OAuth takes priority (logged on startup).

### Run as Service

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/lethe.service << EOF
[Unit]
Description=Lethe Autonomous AI Agent
After=network.target

[Service]
Type=simple
WorkingDirectory=$(pwd)
ExecStart=$(which uv) run lethe
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now lethe
```

## Memory System

### Memory Blocks (Core Memory)

Always in context. Stored as files in `config/blocks/`:

```
config/blocks/
├── identity.md     # Who the agent is (persona, purpose, actor model instructions)
├── human.md        # What it knows about you
├── project.md      # Current project context (agent updates this)
└── tools.md        # Available tools documentation
```

Edit these files directly — changes are picked up on next message.

### Archival Memory

Long-term semantic storage with hybrid search (vector + full-text). Used for:
- Facts and learnings
- Detailed information that doesn't fit in blocks
- Searchable via `archival_search` tool

### Message History

Conversation history stored locally. Searchable via `conversation_search` tool.

## Tools

### Cortex Tools (coordinator only)

| Tool | Purpose |
|------|---------|
| `spawn_actor` | Spawn a subagent with specific goals and tools |
| `kill_actor` | Terminate a stuck subagent |
| `ping_actor` | Check a subagent's status and progress |
| `send_message` | Send a message to another actor |
| `discover_actors` | See all actors in a group |
| `wait_for_response` | Block until a reply arrives |
| `memory_read/update/append` | Core memory block management |
| `archival_search/insert` | Long-term memory |
| `conversation_search` | Search message history |
| `telegram_send_message/file` | Send messages/files to user |

### Subagent Tools (workers)

**Always available**: `bash`, `read_file`, `write_file`, `edit_file`, `list_directory`, `grep_search`

**On request** (via `spawn_actor(tools=...)`): `web_search`, `fetch_webpage`, `browser_open`, `browser_click`, `browser_fill`, `browser_snapshot`

### Browser (via agent-browser)

Subagents can use browser automation:
- Uses accessibility tree refs (`@e1`, `@e2`) — deterministic, no AI guessing
- Persistent sessions with profiles
- Headed mode for manual login

## Hippocampus (Autoassociative Memory)

On each message, the hippocampus automatically searches for relevant context:

- LLM decides whether to recall (skips greetings, simple questions)
- Generates concise 2-5 word search queries
- Searches archival memory (semantic + keyword hybrid)
- Searches past conversations
- Max 50 lines of context added
- Disable with `HIPPOCAMPUS_ENABLED=false`

## Conversation Manager

- **No debounce on first message** — responds immediately
- **Debounce on interrupt** — waits 5s for follow-up messages
- **Message batching** — combines rapid messages into one

## Console (Mind State Visualization)

Enable with `LETHE_CONSOLE=true`. Web dashboard on port 8777.

- 3-column layout: Messages | Memory | Context
- Memory blocks show cache TTL badges (1h/5m/uncached)
- Live cache hit%, token counts, API call stats
- CPU/MEM/GPU system metrics
- Dark theme (Westworld Delos inspired)

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `TELEGRAM_BOT_TOKEN` | Bot token from BotFather | (required) |
| `TELEGRAM_ALLOWED_USER_IDS` | Comma-separated user IDs | (required) |
| `LLM_PROVIDER` | Force provider (`openrouter`, `anthropic`, `openai`) | (auto-detect) |
| `OPENROUTER_API_KEY` | OpenRouter API key | (one required) |
| `ANTHROPIC_API_KEY` | Anthropic API key | (one required) |
| `ANTHROPIC_AUTH_TOKEN` | Anthropic OAuth token (subscription) | (alternative) |
| `OPENAI_API_KEY` | OpenAI API key | (one required) |
| `LLM_MODEL` | Main model | (provider default) |
| `LLM_MODEL_AUX` | Aux model for summarization | (provider default) |
| `LLM_CONTEXT_LIMIT` | Context window size | `128000` |
| `EXA_API_KEY` | Exa web search API key | (optional) |
| `HIPPOCAMPUS_ENABLED` | Enable memory recall | `true` |
| `ACTORS_ENABLED` | Enable actor model | `true` |
| `WORKSPACE_DIR` | Agent workspace | `./workspace` |
| `MEMORY_DIR` | Memory data storage | `./data/memory` |
| `LETHE_CONSOLE` | Enable web console | `false` |
| `HEARTBEAT_INTERVAL` | DMN round interval (seconds) | `900` |

Note: `.env` file takes precedence over shell environment variables.

### Identity Configuration

Edit files in `config/blocks/` to customize the agent:
- `identity.md` — Agent's personality, purpose, and actor model instructions
- `human.md` — What the agent knows about you
- `project.md` — Current project context (agent updates this itself)
- `tools.md` — Tool documentation for the cortex

### Migrating to Actor Model

If upgrading from a pre-actor install:

```bash
python scripts/migrate_to_actors.py
```

This rewrites `identity.md` and `tools.md` for the actor model. Uses LLM (Haiku via OpenRouter) for intelligent rewriting that preserves your persona; falls back to templates if no API key. Creates `.bak` backups.

## Development

```bash
# Run tests
uv run pytest

# Run specific test file
uv run pytest tests/test_actor.py -v
```

### Test Coverage (180 tests)

- `test_actor.py` — 60 tests (actor model, registry, tools, lifecycle)
- `test_dmn.py` — 6 tests (default mode network)
- `test_tools.py` — 51 tests (filesystem, CLI, browser, web search)
- `test_blocks.py` — 15 tests (file-based memory blocks)
- `test_truncate.py` — 20 tests (smart truncation utilities)
- `test_conversation.py` — 16 tests (conversation manager)
- `test_hippocampus.py` — 10 tests (autoassociative memory recall)

## Project Structure

```
src/lethe/
├── actor/          # Actor model (cortex, DMN, subagents)
│   ├── __init__.py # Actor, ActorRegistry, ActorConfig
│   ├── tools.py    # Actor tools (spawn, kill, ping, send, discover)
│   ├── runner.py   # Subagent LLM loop runner
│   ├── dmn.py      # Default Mode Network (background thinker)
│   └── integration.py  # Wires actors into Agent/main.py
├── agent/          # Agent initialization, tool registration
├── config/         # Settings (pydantic-settings)
├── console/        # NiceGUI web dashboard
├── memory/         # LanceDB-based memory backend
│   ├── llm.py      # LLM client with context budget management
│   ├── anthropic_oauth.py  # Direct Anthropic API for OAuth (subscription auth)
│   ├── store.py    # Unified memory coordinator
│   ├── blocks.py   # Core memory blocks
│   └── context.py  # Context assembly and caching
├── telegram/       # aiogram bot
├── tools/          # Tool implementations (filesystem, CLI, browser, web)
├── heartbeat.py    # Periodic timer (triggers DMN rounds)
└── main.py         # Entry point
config/
├── blocks/
│   ├── identity.md # Agent persona + actor model instructions
│   ├── human.md    # User context
│   ├── project.md  # Project context (agent updates)
│   └── tools.md    # Tool documentation
```

## License

MIT
