# Lethe

[![Release](https://img.shields.io/github/v/release/atemerev/lethe?style=flat-square&color=blue)](https://github.com/atemerev/lethe/releases/latest)
[![License](https://img.shields.io/badge/license-MIT-green?style=flat-square)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11+-blue?style=flat-square&logo=python&logoColor=white)](https://python.org)

Autonomous AI assistant with persistent memory, background cognition, and a multi-agent architecture.

Lethe runs 24/7 as a systemd/launchd service, communicates via Telegram, remembers your preferences and projects across sessions, and thinks in the background even when you're not talking to it. It uses tools, browses files, searches the web, and delegates focused work to subagents.

## Install

```bash
curl -fsSL https://lethe.gg/install | bash
```

The installer sets up `~/.lethe` as the runtime root, creates a systemd (Linux) or launchd (macOS) service, and walks you through provider selection and Telegram bot setup.

**Prerequisites:** Python 3.11+, a Telegram bot token, and an LLM provider (Anthropic subscription, OpenRouter API key, OpenAI, or a local server).

### Manual install

```bash
git clone https://github.com/atemerev/lethe.git
cd lethe
uv sync
cp .env.example .env   # edit with your credentials
uv run lethe
```

### Update / Uninstall

```bash
curl -fsSL https://lethe.gg/update | bash
curl -fsSL https://lethe.gg/uninstall | bash
```

## Architecture

```
Telegram
    |
    v
  Cortex  (principal actor, user-facing)
    |
  Brainstem  (supervision, health checks)
    |
    +--- DMN  (background cognition, hourly)
    +--- Hippocampus  (recall + salience tagging)
    +--- Subagents  (spawned on demand)
    +--- Tools  (CLI, files, web, browser)
    |
  Actor Registry + Event Bus
    |
  Memory
    +-- workspace/memory/*.md  (identity, human, project)
    +-- notes/  (skills, conventions, persistent knowledge)
    +-- LanceDB  (archival memory + message history)
```

### Actors

| Actor | Role |
|-------|------|
| **Cortex** | Principal actor. Only actor that talks to the user. Handles quick tasks directly, delegates complex work to subagents. |
| **Brainstem** | Boot supervisor. Checks resources, releases, reports findings to cortex. |
| **DMN** | Background cognition on an hourly cadence. Scans goals, updates state, writes reflections, escalates insights. |
| **Hippocampus** | Autoassociative recall and salience tagging. Searches notes, archival memory, and conversation history on each message. |
| **Subagents** | Spawned on demand for focused tasks. Report to parent actors. No direct user channel. |

### Prompt architecture

System prompt content is split by update lifecycle:

| Content | Location | Updates |
|---------|----------|---------|
| Persona (identity, character) | `workspace/memory/identity.md` | User-editable. Never overwritten. |
| System instructions | `config/prompts/agent_instructions.md` | Current after `git pull`. |
| Tools documentation | `config/prompts/agent_tools.md` | Current after `git pull`. |
| Actor rules | `config/prompts/actor_*.md` | Current after `git pull`. |

## Security

Lethe enforces an OS-level write sandbox at process start:

- **Linux**: [Landlock](https://landlock.io/) restricts writes to `~/.lethe` and `/tmp`.
- **macOS**: Seatbelt sandbox profile with equivalent restrictions.

The API server binds to `127.0.0.1` by default. Use a reverse proxy for remote access.

## LLM Providers

| Provider | Auth | Example model |
|----------|------|---------------|
| **Anthropic (subscription)** | `ANTHROPIC_AUTH_TOKEN` | `claude-opus-4-6` |
| **Anthropic (API key)** | `ANTHROPIC_API_KEY` | `claude-opus-4-6` |
| **OpenRouter** | `OPENROUTER_API_KEY` | `openrouter/moonshotai/kimi-k2.6` |
| **OpenAI (API key)** | `OPENAI_API_KEY` | `gpt-5.4` |
| **OpenAI (subscription)** | `OPENAI_AUTH_TOKEN` | `gpt-5.4` |
| **Local (llama.cpp)** | `LLM_API_BASE` + `OPENAI_API_KEY=local` | `openai/gemma-4-31B-it-Q8_0.gguf` |

Set `LLM_MODEL` explicitly. The installer writes a default for the chosen provider; manual installs must set it in `.env`.

**Multi-model support:**
- `LLM_MODEL_AUX` -- summarization, hippocampus, lightweight background work
- `LLM_MODEL_DMN` -- DMN model override (defaults to main model)

## Memory

### Notes (persistent knowledge)

Tagged markdown files in `~/.lethe/workspace/notes/`:

```
notes/
  unige_email_via_graph_api.md   # tags: [skill, email, graph-api]
  use_uv_not_pip.md              # tags: [convention, python]
  phd_defense_requirements.md    # tags: [education, PhD]
```

Skills, conventions, and durable procedures. Searched by hippocampus on each message. Auto-extracted from archival memory by the curator.

### Memory blocks (core memory)

Always in context. Stored in `workspace/memory/`:

- `identity.md` -- agent persona (user-customizable, never overwritten)
- `human.md` -- what the agent knows about you
- `project.md` -- current project context (agent-maintained)

### Archival memory

Long-term semantic storage with hybrid search (vector + full-text). The curator runs on startup to extract valuable entries into notes.

### Message history

Full conversation history, stored locally in LanceDB. Searchable via `conversation_search` tool.

## Running locally with Gemma 4

Lethe runs well with **Gemma 4 31B** on consumer GPUs via [llama.cpp](https://github.com/ggml-org/llama.cpp).

```bash
# Build llama.cpp with CUDA
git clone https://github.com/ggml-org/llama.cpp.git && cd llama.cpp
cmake -B build -DGGML_CUDA=ON -DGGML_CUDA_FA_ALL_QUANTS=ON
cmake --build build --target llama-server -j$(nproc)

# Start the server (4x RTX 4090 example)
./build/bin/llama-server \
    --model /path/to/gemma-4-31B-it-Q8_0.gguf \
    --host 0.0.0.0 --port 8090 \
    --n-gpu-layers 999 --split-mode tensor \
    --ctx-size 98304 --flash-attn on \
    --parallel 2 --cache-ram 32768 \
    --jinja --reasoning-budget 4096 \
    --spec-type ngram-mod --spec-ngram-size-n 24 --draft-min 48 --draft-max 64 \
    -fit off
```

Configure Lethe:

```bash
# .env
LLM_PROVIDER=openai
LLM_MODEL=openai/gemma-4-31B-it-Q8_0.gguf
LLM_API_BASE=http://localhost:8090/v1
LLM_CONTEXT_LIMIT=96000
OPENAI_API_KEY=local
```

Key flags: `--split-mode tensor` for true tensor parallelism across GPUs, `--jinja` for native tool calling, `--reasoning-budget 4096` for thinking mode.

## Configuration

### Environment variables

| Variable | Description | Default |
|----------|-------------|---------|
| `TELEGRAM_BOT_TOKEN` | Bot token from BotFather | required |
| `TELEGRAM_ALLOWED_USER_IDS` | Comma-separated user IDs | all |
| `LLM_PROVIDER` | Force provider | auto-detect |
| `LLM_MODEL` | Main model | required |
| `LLM_MODEL_AUX` | Aux model | same as main |
| `LLM_MODEL_DMN` | DMN model override | same as main |
| `LLM_API_BASE` | Custom API URL | -- |
| `LLM_CONTEXT_LIMIT` | Context window size | `100000` |
| `EXA_API_KEY` | Exa web search | optional |
| `ACTORS_ENABLED` | Enable actor model | `true` |
| `HIPPOCAMPUS_ENABLED` | Enable recall + salience | `true` |
| `HEARTBEAT_INTERVAL` | Heartbeat interval (seconds) | `3600` |
| `HEARTBEAT_ENABLED` | Enable heartbeat loop | `true` |
| `PROACTIVE_MAX_PER_DAY` | Proactive message limit | `4` |
| `PROACTIVE_COOLDOWN_MINUTES` | Min spacing between proactive msgs | `60` |
| `LETHE_HOME` | Runtime root directory | `~/.lethe` |
| `LETHE_API_HOST` | API server bind address | `127.0.0.1` |

### Persona

Edit `workspace/memory/identity.md` to customize personality, purpose, and background. This file is never overwritten by updates.

System instructions (communication style, output format) are in `config/prompts/agent_instructions.md`.

### Run as service

The installer creates the service automatically. To do it manually:

```bash
# Linux (systemd user service)
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/lethe.service << EOF
[Unit]
Description=Lethe Autonomous AI Agent
After=network.target

[Service]
Type=simple
WorkingDirectory=/path/to/lethe
ExecStart=/path/to/uv run lethe
Restart=always
RestartSec=10
Environment="LETHE_HOME=$HOME/.lethe"

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now lethe
```

## Development

```bash
uv run pytest
uv run pytest tests/test_notes.py -v
```

### Project structure

```
src/lethe/
  actor/       -- actor model (cortex, dmn, brainstem, subagents)
  agent/       -- runtime orchestration
  memory/      -- blocks, LanceDB, notes, hippocampus, curator, LLM client
  tools/       -- CLI, files, web, browser, Telegram
  telegram/    -- Telegram bot interface
  conversation/ -- conversation management
  paths.py     -- centralized path derivation from LETHE_HOME
  sandbox.py   -- Landlock/Seatbelt write sandbox
  api.py       -- HTTP API server
  main.py      -- entry point

config/
  blocks/      -- seed memory blocks
  prompts/     -- system, actor, and heartbeat prompts
  workspace/   -- workspace seed files (copied once on first run)
```

## License

MIT
