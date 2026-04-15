# Lethe

[![Release](https://img.shields.io/github/v/release/atemerev/lethe?style=flat-square&color=blue)](https://github.com/atemerev/lethe/releases/latest)
[![License](https://img.shields.io/badge/license-MIT-green?style=flat-square)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11+-blue?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![Telegram](https://img.shields.io/badge/Telegram-bot-blue?style=flat-square&logo=telegram)](https://telegram.org)

Autonomous executive assistant with persistent memory and a multi-agent architecture.

Lethe is a 24/7 AI assistant that you communicate with via Telegram. It remembers everything — your preferences, your projects, conversations from months ago. The more you use it, the more useful it becomes.

**Local-first architecture** — runs on your hardware with a local LLM, or with any cloud LLM API.

See [`CHANGELOG.md`](CHANGELOG.md) for release notes.

## Architecture

```
User (Telegram) <-> Cortex (principal actor, user-facing)
                     │
              Brainstem (supervisor)
                     │
          ┌──────────┼──────────┬──────────┐
          ↓          ↓          ↓          ↓
        DMN       Hippocampus  Subagents   Runtime
     (background) (recall+notes) (workers)  health
          │          │          │
          └──────────┴──────────┘
                     │
                     ↓
              Actor Registry + Event Bus
                     │
                     ↓
               Memory (LanceDB)
               ├── blocks (workspace — persona, user context)
               ├── notes (~/lethe/notes/ — skills, conventions)
               ├── archival (vector + FTS)
               └── messages (conversation history)
```

### Actor Model

Lethe uses a neuroscience-inspired actor system:

| Actor | Role |
|-------|------|
| **Brainstem** | Boot supervisor. Checks resources, releases, sends structured findings to cortex. |
| **Cortex** | Principal actor. ONLY actor that talks to the user. Handles quick tasks directly, delegates complex work to subagents. |
| **DMN** (Default Mode Network) | Periodic background cognition: scans goals, updates state, writes reflections, escalates insights. |
| **Hippocampus** | Autoassociative recall: searches notes, archival memory, and conversation history on each message. |
| **Subagents** | Spawned on demand for focused tasks. Report to parent actors. No direct user channel. |

### Prompt Architecture

System prompt content is split by update lifecycle:

| Content | Location | Updates |
|---------|----------|---------|
| **Persona** (identity, character, purpose) | `workspace/memory/identity.md` | User-editable. Never overwritten by updates. |
| **System instructions** (action discipline, output format, communication style) | `config/prompts/agent_instructions.md` | Always current after `git pull`. |
| **Tools documentation** (available tools, notes tags) | `config/prompts/agent_tools.md` | Always current after `git pull`. |
| **Actor rules** (preamble, rules, heartbeat prompts) | `config/prompts/actor_*.md` | Always current after `git pull`. |

This ensures updates to system behavior propagate to all users without overwriting their persona customizations.

## Quick Start

### 1. One-Line Install

```bash
curl -fsSL https://lethe.gg/install | bash
```

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
    --parallel 4 \
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
- `--parallel 4` — 4 concurrent slots for cortex, DMN, hippocampus, brainstem.
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

- **Tool count matters**: Gemma 4 works best with <15 tools. Lethe's two-tier tool system registers 15 core tools, with extended tools available via `request_tool()`.
- **Thinking improves tool selection**: `--reasoning-budget 4096` lets the model reason before choosing tools. Costs ~100-500 extra tokens per response but significantly improves tool calling accuracy.
- **Prompt cache warms over time**: The 32GB cache and 4 parallel slots mean each actor's prompt stays warm. First request is slower.
- **Speculative decoding improves with use**: The ngram pool fills as the model generates, benefiting from repeated patterns (tool schemas, JSON structures).

## LLM Providers

| Provider | Env Variable | Default Model |
|----------|--------------|---------------|
| **Local (llama.cpp)** | `LLM_API_BASE` + `OPENAI_API_KEY=local` | (your GGUF) |
| OpenRouter | `OPENROUTER_API_KEY` | `moonshotai/kimi-k2.5-0127` |
| Anthropic (API key) | `ANTHROPIC_API_KEY` | `claude-opus-4-5-20251101` |
| Anthropic (subscription) | `ANTHROPIC_AUTH_TOKEN` | `claude-opus-4-5-20251101` |
| OpenAI | `OPENAI_API_KEY` | `gpt-5.2` |

Set `LLM_PROVIDER` to force a specific provider, or let it auto-detect from available keys.

**Multi-model support**: Set `LLM_MODEL_AUX` for a cheaper/faster model used in summarization and hippocampus analysis.

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
- Auto-extracted from archival memory by the memory organizer on startup

### Memory Blocks (Core Memory)

Always in context. Stored in `workspace/memory/`:

- `identity.md` — Agent persona (user-customizable)
- `human.md` — What the agent knows about you
- `project.md` — Current project context (agent updates this)

### Archival Memory

Long-term semantic storage with hybrid search (vector + full-text). The memory organizer runs on startup to extract valuable entries into notes and clean out noise.

### Message History

Full conversation history stored locally. Searchable via `conversation_search` tool. Hippocampus searches this during recall.

## Tools

### Two-Tier Tool System

Gemma 4 works best with fewer tools. Lethe registers ~15 core tools with full schemas, with additional tools available on demand via `request_tool()`.

**Core tools** (always available):
`bash`, `read_file`, `write_file`, `edit_file`, `note_search`, `note_create`, `note_list`, `telegram_send_message`, `telegram_react`, `conversation_search`, `spawn_actor`, `send_message`, `discover_actors`, `kill_actor`, `request_tool`

**Extended tools** (via `request_tool("name")`):
`list_directory`, `grep_search`, `web_search`, `fetch_webpage`, `memory_read`, `memory_update`, `memory_append`, `archival_search`, `archival_insert`, `browser_open`, `browser_snapshot`, `browser_click`, `browser_fill`, `telegram_send_file`, and more.

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

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `TELEGRAM_BOT_TOKEN` | Bot token from BotFather | (required) |
| `TELEGRAM_ALLOWED_USER_IDS` | Comma-separated user IDs | (required) |
| `LLM_PROVIDER` | Force provider (`openrouter`, `anthropic`, `openai`) | (auto-detect) |
| `LLM_MODEL` | Main model | (provider default) |
| `LLM_MODEL_AUX` | Aux model for summarization/analysis | (same as main) |
| `LLM_API_BASE` | Custom API URL (for local llama.cpp) | (none) |
| `LLM_CONTEXT_LIMIT` | Context window size | `128000` |
| `EXA_API_KEY` | Exa web search API key | (optional) |
| `HIPPOCAMPUS_ENABLED` | Enable memory recall | `true` |
| `ACTORS_ENABLED` | Enable actor model | `true` |
| `HEARTBEAT_INTERVAL` | Main heartbeat interval (seconds) | `900` |
| `LETHE_CONSOLE` | Enable web console | `false` |

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

Source in `src/lethe/`: `actor/` (actor model), `agent/` (init + tools), `memory/` (LanceDB, notes, hippocampus, organizer, LLM client), `tools/` (bash, files, web, browser, notes), `telegram/`, `main.py`.

Config: `config/blocks/` (persona seeds, copied to workspace once), `config/prompts/` (system instructions, always loaded fresh — updates propagate).

## License

MIT
