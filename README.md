# Lethe

[![Release](https://img.shields.io/github/v/release/atemerev/lethe?style=flat-square&color=blue)](https://github.com/atemerev/lethe/releases/latest)
[![License](https://img.shields.io/badge/license-MIT-green?style=flat-square)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11+-blue?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![Telegram](https://img.shields.io/badge/Telegram-bot-blue?style=flat-square&logo=telegram)](https://telegram.org)

Autonomous executive assistant with persistent memory and a multi-agent architecture.

Lethe is a 24/7 AI assistant with a Telegram-first interface, optional gateway/API deployment, long-term memory, and background cognition. It remembers your preferences, projects, and past conversations, and it can use tools, browse files, search notes, and delegate focused work to subagents.

**Local-first architecture** — run it on your own hardware with a local OpenAI-compatible server such as `llama.cpp`, or point it at cloud providers such as OpenRouter, Anthropic, or OpenAI.

See [`CHANGELOG.md`](CHANGELOG.md) for release notes.

## Architecture

```
Telegram / Gateway
        │
        ↓
  Cortex (principal actor, user-facing)
        │
   Brainstem (supervision)
        │
   ┌────┼───────────────┬──────────────┐
   ↓    ↓               ↓              ↓
  DMN  Hippocampus   Subagents      Tools
 (background) (recall+salience)   (CLI/files/web/browser/Telegram)
        │
        ↓
 Actor Registry + Event Bus
        │
        ↓
 Memory
 ├── workspace/memory/*.md (identity, human, project, ...)
 ├── ~/lethe/notes/ (skills, conventions, persistent knowledge)
 ├── LanceDB archival memory
 └── LanceDB message history
```

### Deployment Modes

- **Direct mode**: a single Lethe process talks to Telegram directly.
- **Gateway mode**: a Telegram gateway routes each user to a dedicated Lethe worker running in `LETHE_MODE=api`. Gateway and workers authenticate with a shared `LETHE_API_TOKEN`, and worker file downloads are restricted to `/workspace`.

### Actor Model

Lethe uses a neuroscience-inspired actor system:

| Actor | Role |
|-------|------|
| **Brainstem** | Boot supervisor. Checks resources, releases, sends structured findings to cortex. |
| **Cortex** | Principal actor. ONLY actor that talks to the user. Handles quick tasks directly, delegates complex work to subagents. |
| **DMN** (Default Mode Network) | Background cognition triggered on the heartbeat cadence. Scans goals, updates state, writes reflections, escalates insights. |
| **Hippocampus** | Autoassociative recall plus emotional salience tagging. Searches notes, archival memory, and conversation history on each message. |
| **Subagents** | Spawned on demand for focused tasks. Report to parent actors. No direct user channel. |

### Prompt Architecture

System prompt content is split by update lifecycle:

| Content | Location | Updates |
|---------|----------|---------|
| **Persona** (identity, character, purpose) | `workspace/memory/identity.md` | User-editable. Never overwritten by updates. |
| **System instructions** (action discipline, output format, communication style) | `config/prompts/agent_instructions.md` | Always current after `git pull`. |
| **Tools documentation** (available tools, notes tags) | `config/prompts/agent_tools.md` | Always current after `git pull`. |
| **Actor rules** (preamble, rules, heartbeat prompts) | `config/prompts/actor_*.md`, `config/prompts/heartbeat_*.md` | Always current after `git pull`. |

This ensures updates to system behavior propagate to all users without overwriting their persona customizations.

## Quick Start

### 1. One-Line Install

```bash
curl -fsSL https://lethe.gg/install | bash
```

By default the installer uses the safer containerized layout:

- repo / runtime files in `~/.lethe`
- user workspace and memory blocks in `~/lethe`
- persistent config in `~/.config/lethe`

### 2. Manual Install

```bash
git clone https://github.com/atemerev/lethe.git
cd lethe
uv sync
cp .env.example .env
# Edit .env with Telegram credentials, provider credentials, and an explicit LLM_MODEL
uv run lethe
```

### 3. Update

```bash
curl -fsSL https://lethe.gg/update | bash
```

## Running Locally with Gemma 4

Lethe runs well with **Google Gemma 4 31B** on consumer GPUs via [llama.cpp](https://github.com/ggml-org/llama.cpp). Tested on 4x RTX 4090 (~51 tok/s).

### Prerequisites

- 4x RTX 4090 (or equivalent ~96GB total VRAM) for Q8_0 quantization
- 2x RTX 4090 for Q4_K_M quantization
- [llama.cpp](https://github.com/ggml-org/llama.cpp) built with CUDA support
- Gemma 4 31B GGUF model (e.g. from [bartowski](https://huggingface.co/bartowski))

### Build llama.cpp

```bash
git clone https://github.com/ggml-org/llama.cpp.git
cd llama.cpp
cmake -B build -DGGML_CUDA=ON -DGGML_CUDA_FA_ALL_QUANTS=ON
cmake --build build --target llama-server -j$(nproc)
```

### Start the server

```bash
./build/bin/llama-server \
    --model /path/to/gemma-4-31B-it-Q8_0.gguf \
    --host 0.0.0.0 --port 8090 \
    --n-gpu-layers 999 \
    --split-mode tensor \
    --ctx-size 98304 \
    --flash-attn on \
    --parallel 2 \
    --cache-ram 32768 \
    --slot-save-path /path/to/slots \
    --jinja \
    --reasoning-budget 4096 \
    --spec-type ngram-mod --spec-ngram-size-n 24 --draft-min 48 --draft-max 64 \
    --metrics \
    -fit off
```

Key flags:
- `--split-mode tensor` — true tensor parallelism across GPUs (~51 tok/s vs ~25 with layer split). Requires `-fit off`.
- `--jinja` — required for Gemma 4's native tool calling format (peg-gemma4 parser).
- `--reasoning-budget 4096` — enables thinking mode for better tool selection accuracy.
- `--parallel 2` — 2 concurrent slots (cortex + aux). Use 4 only if VRAM allows (~20GB free after model).
- `--cache-ram 32768` — 32GB prompt cache so different prompts don't evict each other.
- `--spec-type ngram-mod` — lightweight speculative decoding, shared across all slots.

Note: `--split-mode tensor` does not support KV cache quantization (`-ctk`/`-ctv`). Use f16 KV cache (default).

### Configure Lethe

```bash
# In your .env
LLM_PROVIDER=openai
LLM_MODEL=openai/gemma-4-31B-it-Q8_0.gguf
LLM_MODEL_AUX=openai/gemma-4-31B-it-Q8_0.gguf
LLM_API_BASE=http://localhost:8090/v1
LLM_CONTEXT_LIMIT=96000
OPENAI_API_KEY=local
```

### Performance tips

- **Tool count matters**: Gemma 4 works best with a small active tool budget. Lethe keeps the cortex tool surface compact and exposes additional capabilities via `request_tool()`.
- **Thinking improves tool selection**: `--reasoning-budget 4096` lets the model reason before choosing tools. Costs ~100-500 extra tokens per response but significantly improves tool calling accuracy.
- **Prompt cache warms over time**: The 32GB cache and 4 parallel slots mean each actor's prompt stays warm. First request is slower.
- **Speculative decoding improves with use**: The ngram pool fills as the model generates, benefiting from repeated patterns (tool schemas, JSON structures).

## LLM Providers

| Provider | Auth Env | Example `LLM_MODEL` |
|----------|----------|---------------------|
| **Local (llama.cpp / OpenAI-compatible)** | `LLM_API_BASE` + `OPENAI_API_KEY=local` | `openai/gemma-4-31B-it-Q8_0.gguf` |
| OpenRouter | `OPENROUTER_API_KEY` | `openrouter/moonshotai/kimi-k2.5-0127` |
| Anthropic (API key) | `ANTHROPIC_API_KEY` | `claude-opus-4-5-20251101` |
| Anthropic (subscription token) | `ANTHROPIC_AUTH_TOKEN` | `claude-opus-4-5-20251101` |
| OpenAI (API key) | `OPENAI_API_KEY` | `gpt-5.2` |
| OpenAI (subscription token) | `OPENAI_AUTH_TOKEN` | `gpt-5.2` |

`LLM_MODEL` is required at runtime. The installer writes a sensible default for the provider you pick; manual installs should set it explicitly.

Set `LLM_PROVIDER` only if you want to force a specific provider; otherwise Lethe auto-detects based on the available credentials.

**Multi-model support**:

- `LLM_MODEL_AUX` for summarization, hippocampus analysis, and lightweight background work
- `LLM_MODEL_DMN` if you want the DMN to use a different model than cortex

## Memory System

### Notes (Persistent Knowledge)

Tagged markdown files in `~/lethe/notes/` — the primary store for procedural knowledge:

```
~/lethe/notes/
├── unige_email_via_graph_api.md   # tags: [skill, email, graph-api]
├── use_uv_not_pip.md              # tags: [convention, python]
└── phd_defense_requirements.md    # tags: [education, PhD]
```

- **Skills**: procedures for external systems (APIs, services, auth flows)
- **Conventions**: how things should be done (user preferences, toolchain choices)
- Searched by hippocampus during recall and via `note_search` tool
- Auto-extracted from archival memory by the curator

### Memory Blocks (Core Memory)

Always in context. Stored in `workspace/memory/`:

- `identity.md` — Agent persona (user-customizable)
- `human.md` — What the agent knows about you
- `project.md` — Current project context (agent updates this)

### Archival Memory

Long-term semantic storage with hybrid search (vector + full-text). The curator runs on startup to extract valuable entries into notes and clean out noise.

### Message History

Full conversation history stored locally. Searchable via `conversation_search` tool. Hippocampus searches this during recall.

## Tools

### Tool Budgeting

Lethe intentionally keeps the cortex tool surface small and lets it request extras on demand.

- The base tool registry covers CLI/files, notes, web search, Telegram actions, and browser automation.
- In actor mode, the cortex keeps a compact tool set and can activate additional tools via `request_tool()`.
- Subagents get broader tool access than cortex, because they are used for deeper or parallel execution.

The current registry lives in:

- `src/lethe/tools/__init__.py`
- `src/lethe/actor/integration.py`

### Web Search

Web search uses [Exa](https://exa.ai/) with a subagent synthesis pattern — raw results are processed in a separate LLM call and only a concise summary enters the conversation context, preserving window space for conversation history.

## Hippocampus (Autoassociative Memory)

On each message, the hippocampus automatically searches for relevant context:

1. LLM decides whether recall would help (skips greetings, simple questions)
2. Generates concise 2-5 word search queries
3. Searches **notes** first (pre-distilled, highest signal)
4. Searches archival memory and past conversations
5. Filters for relevance (LLM-based)
6. Summarizes and reviews for stale state before injection

Disable with `HIPPOCAMPUS_ENABLED=false`.

## Gateway / API Mode

Lethe can run behind the multi-tenant gateway in `gateway/`.

- Worker mode is enabled with `LETHE_MODE=api`.
- The gateway and workers must share the same `LETHE_API_TOKEN`.
- Worker endpoints include `/chat`, `/cancel`, `/model`, `/events`, and `/file`.
- `/chat` uses the same `ConversationManager` pipeline as direct Telegram mode, so interrupt/cancel behavior is consistent.
- `/file` only serves files inside the worker workspace mount (`/workspace`).

`docker-compose.gateway.yml` builds the gateway service and expects a `gateway.env` plus a worker env file referenced through `CONTAINER_ENV_FILE`.

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `TELEGRAM_BOT_TOKEN` | Bot token from BotFather | (required) |
| `TELEGRAM_ALLOWED_USER_IDS` | Comma-separated user IDs | empty = allow all |
| `LLM_PROVIDER` | Force provider (`openrouter`, `anthropic`, `openai`) | (auto-detect) |
| `LLM_MODEL` | Main model | (required) |
| `LLM_MODEL_AUX` | Aux model for summarization/analysis | (same as main) |
| `LLM_MODEL_DMN` | DMN model override | (same as main) |
| `LLM_API_BASE` | Custom API URL (for local llama.cpp) | (none) |
| `LLM_CONTEXT_LIMIT` | Context window size | `100000` |
| `EXA_API_KEY` | Exa web search API key | (optional) |
| `ACTORS_ENABLED` | Enable actor model | `true` |
| `HIPPOCAMPUS_ENABLED` | Enable hippocampus recall + salience tagging | `true` |
| `HEARTBEAT_INTERVAL` | Heartbeat interval (seconds) | `3600` |
| `HEARTBEAT_ENABLED` | Enable heartbeat loop | `true` |
| `PROACTIVE_MAX_PER_DAY` | Hard limit for proactive user messages | `4` |
| `PROACTIVE_COOLDOWN_MINUTES` | Minimum spacing between proactive messages | `60` |
| `LETHE_MODE` | `api` for worker mode, otherwise Telegram mode | direct mode |
| `LETHE_API_TOKEN` | Shared secret for gateway <-> worker API | required in API mode |
| `LETHE_CONSOLE` | Enable web console | `false` |
| `LETHE_CONSOLE_HOST` | Console bind host | `127.0.0.1` |
| `LETHE_CONSOLE_PORT` | Console bind port | `8777` |

### Persona Configuration

Edit `workspace/memory/identity.md` to customize the agent's personality, purpose, and background. This file is never overwritten by updates.

System instructions (communication style, action discipline, output format) are in `config/prompts/agent_instructions.md` — edit if you need different behavior rules.

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

## Development

```bash
uv run pytest
uv run pytest tests/test_notes.py -v
```

## Project Structure

Source in `src/lethe/`: `actor/` (actor model), `agent/` (runtime + orchestration), `memory/` (blocks, LanceDB, notes, hippocampus, curator, LLM client), `tools/` (CLI/files/web/browser/Telegram), `telegram/`, `conversation/`, `api.py`, `main.py`.

Gateway code lives in `gateway/`.

Config: `config/blocks/` (seed memory blocks), `config/prompts/` (system/actor/heartbeat prompts), `config/workspace/` (workspace seed files copied once).

## License

MIT
