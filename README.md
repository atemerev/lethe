# Lethe

Autonomous executive assistant with Letta memory layer.

Lethe is a 24/7 AI assistant that you communicate with via Telegram. It processes tasks asynchronously, maintains persistent memory across conversations, and has full access to your machine.

## Architecture

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│  Telegram   │────▶│  Task Queue │────▶│   Worker    │
│    Bot      │     │  (SQLite)   │     │             │
└─────────────┘     └─────────────┘     └──────┬──────┘
                                               │
                                               ▼
                                        ┌─────────────┐
                                        │   Letta     │
                                        │   Agent     │
                                        │  (memory +  │
                                        │  reasoning) │
                                        └──────┬──────┘
                                               │
                         ┌─────────────────────┼─────────────────────┐
                         │                     │                     │
                         ▼                     ▼                     ▼
                  ┌─────────────┐       ┌─────────────┐       ┌─────────────┐
                  │ Filesystem  │       │    CLI      │       │   Browser   │
                  │   Tools     │       │   Tools     │       │   Tools     │
                  └─────────────┘       └─────────────┘       └─────────────┘
```

## Quick Start

### 1. Prerequisites

- Python 3.11+
- [uv](https://github.com/astral-sh/uv) for dependency management
- Local [Letta server](https://github.com/letta-ai/letta) running
- Telegram bot token from [@BotFather](https://t.me/BotFather)

### 2. Install

```bash
cd lethe
uv sync
```

### 3. Configure

```bash
cp .env.example .env
# Edit .env with your settings:
# - TELEGRAM_BOT_TOKEN (required)
# - TELEGRAM_ALLOWED_USER_IDS (your Telegram user ID)
# - LETTA_BASE_URL (default: http://localhost:8283)
```

### 4. Start Letta Server

```bash
# Option 1: Docker
docker run -d -p 8283:8283 -v letta-data:/root/.letta letta/letta:latest

# Option 2: pip
pip install letta
letta server
```

### 5. Run Lethe

```bash
uv run lethe
# or
uv run python -m lethe.main
```

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `TELEGRAM_BOT_TOKEN` | Bot token from BotFather | (required) |
| `TELEGRAM_ALLOWED_USER_IDS` | Comma-separated user IDs | (empty = all) |
| `LETTA_BASE_URL` | Letta server URL | `http://localhost:8283` |
| `LETTA_API_KEY` | API key (if using cloud) | (empty) |
| `LETHE_AGENT_NAME` | Agent name in Letta | `lethe` |
| `LETHE_CONFIG_DIR` | Path to config files | `./config` |
| `DB_PATH` | SQLite database path | `./data/lethe.db` |

### Config Files

- `config/identity.md` - Agent persona and capabilities
- `config/project.md` - Current project context

## Tools

The agent has access to:

### Filesystem
- `read_file` - Read files with line numbers
- `write_file` - Create/overwrite files
- `edit_file` - Replace text in files
- `list_directory` - List directory contents
- `glob_search` - Find files by pattern
- `grep_search` - Search file contents

### CLI
- `run_command` - Execute shell commands
- `run_command_background` - Start background processes
- `run_gog` - Gmail operations via gog CLI
- `get_environment_info` - System information
- `check_command_exists` - Check if command is available

## Development

```bash
# Install with dev dependencies
uv sync --extra dev

# Run tests
uv run pytest

# Format/lint
uv run ruff check --fix
```

## Adding Custom Tools

Create a new file in `src/lethe/tools/` and add tools using the `@_is_tool` decorator:

```python
def _is_tool(func):
    func._is_tool = True
    return func

@_is_tool
def my_custom_tool(arg1: str, arg2: int = 10) -> str:
    """Description of what the tool does.
    
    Args:
        arg1: Description of arg1
        arg2: Description of arg2
    
    Returns:
        What the tool returns
    """
    # Implementation
    return "result"
```

Then import the module in `src/lethe/tools/__init__.py`.

## License

MIT
