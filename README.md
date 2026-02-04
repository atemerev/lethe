# Lethe

[![Release](https://img.shields.io/badge/release-v0.3.6-blue?style=flat-square)](https://github.com/atemerev/lethe/releases/tag/v0.3.6)
[![License](https://img.shields.io/badge/license-MIT-green?style=flat-square)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11+-blue?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![Telegram](https://img.shields.io/badge/Telegram-bot-blue?style=flat-square&logo=telegram)](https://telegram.org)

Autonomous executive assistant with persistent memory.

Lethe is a 24/7 AI assistant that you communicate with via Telegram. It remembers everything - your preferences, your projects, conversations from months ago. The more you use it, the more useful it becomes.

**Local-first architecture** - no cloud dependencies except the LLM API.

## Architecture

```
Telegram Bot → Conversation Manager → Agent → LLM (via litellm)
     ↑              (debounce,           ↓
     │              interrupts)       Tools (bash, files, browser, web search)
     │                                   ↓
     └─────────────────────────────── Memory (LanceDB)
                                        ├── blocks (files in workspace/)
                                        ├── archival (vector + FTS)
                                        └── messages (conversation history)
```

## Core Dependencies

| Component | Library | Purpose |
|-----------|---------|---------|
| **LLM** | [litellm](https://github.com/BerriAI/litellm) | Multi-provider LLM API (OpenRouter, Anthropic, OpenAI) |
| **Vector DB** | [LanceDB](https://lancedb.com/) | Local vector + full-text search for memory |
| **Embeddings** | [sentence-transformers](https://sbert.net/) | Local embeddings (all-MiniLM-L6-v2, CPU-only) |
| **Telegram** | [aiogram](https://aiogram.dev/) | Async Telegram bot framework |

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

This automatically detects your install mode (container or native) and updates accordingly.

### LLM Providers

| Provider | Env Variable | Default Model |
|----------|--------------|---------------|
| OpenRouter | `OPENROUTER_API_KEY` | `moonshotai/kimi-k2.5-0127` |
| Anthropic | `ANTHROPIC_API_KEY` | `claude-opus-4-5-20251101` |
| OpenAI | `OPENAI_API_KEY` | `gpt-5.2` |

Set `LLM_PROVIDER` to force a specific provider, or let it auto-detect from available API keys.

**Multi-model support**: Set `LLM_MODEL_AUX` for a cheaper model used in heartbeats/summarization (e.g., `claude-haiku-4-5-20251001`).

### 5. (Optional) Run as Service

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

Always in context. Stored as editable files in `workspace/memory/`:

```
workspace/memory/
├── persona.md      # Who the agent is
├── human.md        # What it knows about you
├── project.md      # Current project context
├── tools.md        # Available CLI tools
└── tasks.md        # Active tasks/reminders
```

Edit these files directly - changes are picked up on next message.

### Archival Memory

Long-term semantic storage with hybrid search (vector + full-text). Used for:
- Facts and learnings
- Detailed information that doesn't fit in blocks
- Searchable via `archival_search` tool

### Message History

Conversation history stored locally. Searchable via `conversation_search` tool.

## Tools

### Filesystem
- `read_file` - Read with smart truncation (2000 lines / 50KB)
- `write_file` - Create/overwrite files
- `edit_file` - Replace text in files
- `list_directory` - List directory contents
- `glob_search` - Find files by pattern
- `grep_search` - Search file contents (500 char line limit)

### CLI
- `bash` - Execute commands with tail truncation (keeps errors visible)
- `bash_output` - Get output from background processes
- `kill_bash` - Terminate background processes

### Browser (via agent-browser)
- `browser_open` - Open URL
- `browser_snapshot` - Get accessibility tree with refs (@e1, @e2...)
- `browser_click` - Click element by ref
- `browser_fill` - Fill input field
- `send_image` - Send image to user (maintains message order)

### Web Search (optional, requires EXA_API_KEY)
- `web_search` - AI-powered semantic search
- `fetch_webpage` - Extract clean text from URL

### Memory
- `memory_read` - Read a memory block
- `memory_update` - Update a memory block
- `memory_append` - Append to a memory block
- `archival_search` - Search long-term memory
- `archival_insert` - Store in long-term memory
- `conversation_search` - Search message history

## Hippocampus (Autoassociative Memory)

On each message, the hippocampus automatically searches for relevant context:

```
User message → Hippocampus → Augmented message → LLM
                   ↓
            Search archival memory
            Search conversation history
                   ↓
            [Associative memory recall]
            **From long-term memory:**
            - relevant facts...
            **From past conversations:**
            - related discussions...
            [End of recall]
```

- Searches archival memory (semantic + keyword hybrid)
- Searches past conversations (excludes recent 5 messages)
- Filters by relevance score (threshold: 0.3)
- Max 50 lines of context added
- Disable with `HIPPOCAMPUS_ENABLED=false`

## Conversation Manager

Handles async message processing with:

- **No debounce on first message** - Responds immediately
- **Debounce on interrupt** - Waits 5s for follow-up messages
- **Message batching** - Combines rapid messages into one
- **Interrupt handling** - New messages during processing are queued

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `TELEGRAM_BOT_TOKEN` | Bot token from BotFather | (required) |
| `TELEGRAM_ALLOWED_USER_IDS` | Comma-separated user IDs | (required) |
| `LLM_PROVIDER` | Force provider (`openrouter`, `anthropic`, `openai`) | (auto-detect) |
| `OPENROUTER_API_KEY` | OpenRouter API key | (one required) |
| `ANTHROPIC_API_KEY` | Anthropic API key | (one required) |
| `OPENAI_API_KEY` | OpenAI API key | (one required) |
| `LLM_MODEL` | Main model | (provider default) |
| `LLM_MODEL_AUX` | Aux model for heartbeats | (provider default) |
| `LLM_CONTEXT_LIMIT` | Context window size | `128000` |
| `EXA_API_KEY` | Exa web search API key | (optional) |
| `HIPPOCAMPUS_ENABLED` | Enable memory recall | `true` |
| `WORKSPACE_DIR` | Agent workspace | `./workspace` |
| `MEMORY_DIR` | Memory data storage | `./data/memory` |

Note: `.env` file takes precedence over shell environment variables.

### Identity Configuration

Edit files in `config/blocks/` to customize the agent:
- `persona.md` - Agent's personality and behavior
- `human.md` - What the agent knows about you
- `project.md` - Current project context

## Development

```bash
# Run tests
uv run pytest

# Run specific test file
uv run pytest tests/test_tools.py -v

# Run with coverage
uv run pytest --cov=lethe
```

### Test Coverage

- `test_tools.py` - 51 tests (filesystem, CLI, browser, web search)
- `test_blocks.py` - 15 tests (file-based memory blocks)
- `test_truncate.py` - 20 tests (smart truncation utilities)
- `test_conversation.py` - 16 tests (conversation manager)
- `test_hippocampus.py` - 10 tests (autoassociative memory recall)

## Adding Custom Tools

Create a function with the `@_is_tool` decorator:

```python
def _is_tool(func):
    func._is_tool = True
    return func

@_is_tool
def my_tool(arg1: str, arg2: int = 10) -> str:
    """Description of what the tool does.
    
    Args:
        arg1: Description of arg1
        arg2: Description of arg2
    
    Returns:
        What the tool returns
    """
    return "result"
```

Add to `src/lethe/tools/__init__.py`.

## License

MIT
