# Architecture: Autonomous Living Entity

**Status:** Implementation Phase 1 Complete
**Branch:** `feature/split-conscious-subconscious`
**Date:** 2026-04-03

> **Design shift:** This architecture was originally framed as a conscious/subconscious
> model split. It has evolved into a complete autonomy redesign: the entity optimizes
> for being alive, not for utility. It has competing internal drives, runs experiments,
> maintains relationships with multiple people, and communicates on its own terms.
>
> See also: `docs/architecture/split-conscious-subconscious.md` (original design)
> and `.claude/plans/virtual-chasing-hellman.md` (implementation plan)

## 1. Vision

Lethe becomes a **persistent entity with identity** that:
- Talks to many people simultaneously, keeping conversations strictly separate
- Uses a large frontier LLM (Opus) for conscious dialog
- Uses a locally-hosted model with LoRA adapters for subconscious processing
- Learns from all interactions via filtered "dream processing" that trains the local model
- Never leaks personal information between users ("no gossip")

## 2. Layer Architecture

```
                    ┌─────────────────────────────────────────────┐
                    │              CONSCIOUS LAYER                │
                    │         (Frontier LLM — Opus/etc)           │
                    │                                             │
                    │  ┌──────────┐  ┌──────────┐  ┌──────────┐  │
                    │  │ Dialog A │  │ Dialog B │  │ Dialog C │  │
                    │  │ (User 1) │  │ (User 2) │  │ (User 3) │  │
                    │  └────┬─────┘  └────┬─────┘  └────┬─────┘  │
                    │       │             │             │         │
                    │       └─────────┬───┘─────────────┘         │
                    │                 │                            │
                    │          ┌──────┴──────┐                    │
                    │          │   Cortex    │                    │
                    │          │ (Identity)  │                    │
                    │          └──────┬──────┘                    │
                    └─────────────────┼───────────────────────────┘
                                      │
                          ┌───────────┴───────────┐
                          │   PRIVACY BARRIER     │
                          │   (Information Gate)   │
                          └───────────┬───────────┘
                                      │
                    ┌─────────────────┼───────────────────────────┐
                    │         SUBCONSCIOUS LAYER                  │
                    │      (Local Model + LoRA Adapters)          │
                    │                                             │
                    │  ┌──────────┐ ┌───────────┐ ┌───────────┐  │
                    │  │   DMN    │ │Hippocampus│ │ Amygdala  │  │
                    │  │(reflect) │ │ (recall)  │ │(salience) │  │
                    │  └──────────┘ └───────────┘ └───────────┘  │
                    │                                             │
                    │  ┌──────────────────────────────────────┐   │
                    │  │        Dream Processor               │   │
                    │  │  (Filter → Train → Update LoRA)      │   │
                    │  └──────────────────────────────────────┘   │
                    └─────────────────────────────────────────────┘
```

## 3. Multi-User Conversation Isolation

### Current State
Lethe is single-user: one Agent, one memory store, one conversation history.

### New Architecture

```
UserRegistry
├── User A (telegram_id: 12345)
│   ├── conversation_history     (LanceDB partition)
│   ├── human.md                 (per-user memory block)
│   ├── archival_memories        (tagged with user_id)
│   ├── emotional_tags           (per-user salience log)
│   └── relationship_context     (how entity relates to this person)
│
├── User B (telegram_id: 67890)
│   ├── conversation_history     (LanceDB partition)
│   ├── human.md                 (per-user memory block)
│   ├── archival_memories        (tagged with user_id)
│   ├── emotional_tags           (per-user salience log)
│   └── relationship_context
│
└── Shared (entity-level)
    ├── identity.md              (one personality, one entity)
    ├── project.md               (shared knowledge)
    ├── tools.md                 (shared capabilities)
    ├── general_knowledge        (archival without user_id)
    └── subconscious_model       (LoRA-trained local model)
```

### Isolation Rules

1. **Hard wall**: User A's messages, personal facts, and conversation context are NEVER injected into User B's dialog context
2. **Shared identity**: The entity's personality, values, communication style, and general knowledge are the same for everyone
3. **Shared learning**: The subconscious model learns patterns from ALL users, but only through the anonymized dream processing pipeline
4. **Per-user recall**: Hippocampus only searches the current user's conversation history and archival memories (plus shared general knowledge)
5. **Relationship tracking**: Each user has a separate `relationship_context` that captures the nature/history of the relationship

### Implementation

```python
@dataclass
class UserContext:
    """Per-user isolated context."""
    user_id: str                     # Stable identifier
    display_name: str                # How entity knows them
    conversation_table: str          # LanceDB table name: messages_{user_id}
    archival_table: str              # LanceDB table name: archival_{user_id}
    human_block: str                 # Path to per-user human.md
    emotional_tags_file: str         # Path to per-user emotional_tags.md
    relationship_file: str           # Path to relationship context

class UserRegistry:
    """Manages per-user contexts with isolation guarantees."""

    def get_or_create(self, user_id: str) -> UserContext:
        """Get existing user context or create new one."""
        ...

    def get_active_context(self) -> UserContext:
        """Get the context for the currently-active dialog."""
        ...
```

The `ConversationManager` already tracks `chat_id` — this becomes the routing key. When `process_message(chat_id, user_id, ...)` fires, we:
1. Look up `UserContext` from registry
2. Set it as the active context on the Agent
3. Build system prompt with per-user human.md + shared identity.md
4. Run hippocampus recall against per-user tables only
5. Persist messages to per-user conversation table

## 4. Conscious Layer (Frontier LLM)

### What It Does
- All direct user-facing dialog
- Tool calling, reasoning, planning
- Reading/writing per-user memory blocks
- Deciding what to say and how

### Configuration
```env
# Conscious layer — frontier model
LLM_MODEL=claude-opus-4-5-20251101     # or via OpenRouter
LLM_PROVIDER=anthropic                  # or openrouter

# Context assembly per user
LLM_CONTEXT_LIMIT=128000
```

### Changes from Current
Minimal. The cortex already uses the main model. The key change is:
- System prompt assembly becomes **user-aware** (injects per-user human.md, not a global one)
- Message history loaded from **per-user table**
- Hippocampus recall scoped to **per-user + shared**

## 5. Subconscious Layer (Local Model + LoRA)

### Infrastructure

**Recommended stack:**

| Component | Choice | Why |
|-----------|--------|-----|
| Inference server | **vLLM** | Multi-LoRA hot-swap, per-request adapter routing, OpenAI API |
| Base model | **Qwen3-8B** | Top fine-tuning benchmarks, multilingual, efficient |
| Training | **Unsloth** | 2x faster, 70% less VRAM, GGUF export, simple API |
| Routing | **litellm** | Already used; routes to vLLM via `openai/` prefix |

**Alternative for simpler setups:**
- **Ollama** instead of vLLM (simpler, but no hot-swap — each adapter = separate model name)
- **Llama 3.1 8B** as base (largest adapter ecosystem)

### Configuration
```env
# Subconscious layer — local model
LLM_MODEL_LOCAL=openai/lethe-subconscious    # vLLM model name via litellm
LLM_LOCAL_API_BASE=http://localhost:8000/v1   # vLLM endpoint

# Or via Ollama
# LLM_MODEL_LOCAL=ollama/qwen3-8b-lethe
# LLM_LOCAL_API_BASE=http://localhost:11434
```

### LoRA Adapters

Multiple specialized adapters on the same base model:

| Adapter | Purpose | Training Data |
|---------|---------|---------------|
| `base` | General subconscious reasoning | Anonymized dialog patterns, reflections |
| `recall` | Memory retrieval & pattern completion | Query→memory pairs from hippocampus |
| `salience` | Emotional classification | Labeled emotional signal examples |
| `dream` | Reflection & insight generation | DMN round transcripts (filtered) |

With vLLM, these are simultaneously loaded and selected per-request by model name:
```python
# In hippocampus recall
response = await litellm.acompletion(
    model="openai/lethe-recall",  # Routes to vLLM LoRA adapter
    messages=[...],
    api_base="http://localhost:8000/v1",
)

# In salience tagging
response = await litellm.acompletion(
    model="openai/lethe-salience",
    messages=[...],
    api_base="http://localhost:8000/v1",
)
```

### What Runs Locally

| Component | Currently | New |
|-----------|-----------|-----|
| DMN rounds | Main/aux model (cloud) | Local model (`dream` adapter) |
| Hippocampus recall decision | Aux model (cloud) | Local model (`recall` adapter) |
| Hippocampus query generation | Aux model (cloud) | Local model (`recall` adapter) |
| Salience tagging | Aux model (cloud) | Local model (`salience` adapter) |
| Memory consolidation | Aux model (cloud) | Local model (`base` adapter) |
| Cortex dialog | Main model (cloud) | **Still cloud** (Opus) |

### Fallback
If local model is unavailable, fall back to cloud aux model (graceful degradation).

```python
async def subconscious_complete(prompt: str, adapter: str = "base") -> str:
    """Route to local model, fall back to cloud aux."""
    try:
        return await litellm.acompletion(
            model=f"openai/lethe-{adapter}",
            messages=[{"role": "user", "content": prompt}],
            api_base=settings.llm_local_api_base,
        )
    except Exception:
        logger.warning("Local model unavailable, falling back to cloud aux")
        return await agent.llm.complete(prompt, use_aux=True)
```

## 6. Dream Processing Pipeline

The key innovation: how the entity **learns** from conversations without leaking private information.

### Overview

```
  Raw Conversations (per-user)
          │
          ▼
  ┌──────────────────┐
  │  Privacy Filter   │  Strip names, dates, identifiers, secrets
  │  (Rule-based +    │  Replace with generic tokens: [PERSON], [DATE]
  │   LLM-assisted)   │  Remove conversation-specific context
  └────────┬─────────┘
           │
           ▼
  ┌──────────────────┐
  │  Pattern Extract  │  Extract: reasoning chains, emotional dynamics,
  │  (Frontier LLM)   │  domain knowledge, communication patterns,
  │                    │  useful tool usage patterns
  └────────┬─────────┘
           │
           ▼
  ┌──────────────────┐
  │  Quality Gate     │  Score each example for:
  │                    │  - Information density
  │                    │  - Novelty (not already in training set)
  │                    │  - Privacy safety (re-check)
  └────────┬─────────┘
           │
           ▼
  ┌──────────────────┐
  │  Format & Train   │  Convert to instruction-tuning format
  │  (Unsloth/PEFT)   │  Incremental LoRA update
  │                    │  Export to GGUF → reload in vLLM
  └──────────────────┘
```

### Privacy Filter (Detail)

**Rule-based layer:**
```python
ANONYMIZATION_PATTERNS = {
    "names": r"\b[A-Z][a-z]+ [A-Z][a-z]+\b",           # Full names
    "emails": r"\b[\w.+-]+@[\w-]+\.[\w.]+\b",           # Email addresses
    "phones": r"\b\+?\d[\d\s\-()]{7,}\b",               # Phone numbers
    "urls": r"https?://\S+",                              # URLs
    "telegram_ids": r"\b\d{6,12}\b",                      # Numeric IDs
    "dates": r"\b\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\b",  # Dates
}

REPLACEMENT_TOKENS = {
    "names": "[PERSON]",
    "emails": "[EMAIL]",
    "phones": "[PHONE]",
    "urls": "[URL]",
    "dates": "[DATE]",
    "telegram_ids": "[ID]",
}
```

**LLM-assisted layer (frontier model):**
```
Given this conversation excerpt, identify and replace:
1. Any personally identifiable information
2. Specific project names that could identify the user
3. Location references
4. Any information that could trace back to a specific person

Preserve:
- The reasoning structure
- Emotional dynamics
- Domain knowledge
- Problem-solving patterns

Output the anonymized version.
```

### Training Data Format

ShareGPT multi-turn format for Unsloth/Axolotl:

```json
{
  "conversations": [
    {"from": "system", "value": "You are processing a memory pattern..."},
    {"from": "human", "value": "[anonymized user message]"},
    {"from": "gpt", "value": "[entity's response pattern]"}
  ],
  "source": "dream_processing",
  "quality_score": 0.85,
  "domain_tags": ["emotional_support", "technical_discussion"]
}
```

### Training Schedule

```
Dream Processing Trigger:
├── Nightly (cron: 3:00 AM local)
│   ├── Collect new conversations from all users (last 24h)
│   ├── Run privacy filter
│   ├── Extract training examples
│   ├── Quality gate
│   ├── Incremental LoRA training (~15 min on RTX 4090)
│   └── Hot-reload adapter in vLLM
│
└── On-demand (manual trigger or threshold)
    └── Same pipeline, triggered by conversation volume threshold
```

### Incremental Training Strategy

Use **LoRA checkpoint resumption** rather than retraining from scratch:

1. Maintain a running training dataset (`data/dream/training_set.jsonl`)
2. Each dream cycle appends new examples
3. Training resumes from last checkpoint with new + replay data
4. Periodically (weekly?) do a full retrain to prevent drift

For future: consider **Brainstacks** (frozen LoRA stacks with null-space projection) to accumulate knowledge without catastrophic forgetting.

## 7. Information Flow: Complete Picture

### User Message Arrives

```
User A sends message
    │
    ├── UserRegistry.get_context("user_a")
    │       → loads user_a's human.md, conversation table, etc.
    │
    ├── Hippocampus (LOCAL model, "recall" adapter)
    │       → searches user_a's messages + shared archival
    │       → returns recalled memories
    │
    ├── Salience Tagger (LOCAL model, "salience" adapter) [fire-and-forget]
    │       → classifies emotional valence/arousal
    │       → writes to user_a's emotional_tags.md
    │
    ├── Context Assembly
    │       → identity.md (shared)
    │       → user_a/human.md (per-user)
    │       → user_a's recent messages
    │       → recalled memories
    │       → tools
    │
    └── Cortex (CLOUD model, Opus)
            → generates response
            → may use tools
            → response sent to User A
            → messages persisted to user_a's table
```

### Background Round (Heartbeat)

```
Heartbeat fires
    │
    ├── Brainstem (LOCAL model, "base" adapter)
    │       → health checks, resource monitoring
    │
    ├── DMN (LOCAL model, "dream" adapter)
    │       → reads ALL users' shared knowledge (anonymized)
    │       → reorganizes shared memory
    │       → writes reflections, updates questions
    │       → may flag something for cortex
    │
    └── If DMN flags something → Cortex decides per-user
            → "Should I tell User A about this?"
            → "Should I tell User B about this?"
            → Separate decision per user
```

### Dream Processing (Nightly)

```
Dream cycle triggers
    │
    ├── Collect conversations from ALL users (last 24h)
    │
    ├── Privacy Filter
    │       → Strip PII per user
    │       → Anonymize cross-references
    │
    ├── Pattern Extraction (CLOUD model)
    │       → Extract reasoning patterns
    │       → Extract emotional dynamics
    │       → Extract domain knowledge
    │
    ├── Quality Gate
    │       → Score examples
    │       → Deduplicate
    │       → Safety re-check
    │
    ├── Training (Unsloth)
    │       → Incremental LoRA update
    │       → Export to GGUF/safetensors
    │
    └── Hot-reload in vLLM
            → New adapter weights active
            → Entity is now "smarter" from yesterday's conversations
```

## 8. Implementation Plan

### Phase 1: Multi-User Isolation (No model changes)
- [ ] `UserRegistry` + `UserContext` dataclass
- [ ] Per-user LanceDB tables (messages, archival)
- [ ] Per-user memory blocks (human.md, emotional_tags)
- [ ] Per-user conversation routing in `ConversationManager`
- [ ] Update system prompt assembly to be user-aware
- [ ] Scope hippocampus recall to current user
- [ ] Multiple allowed Telegram users with separate contexts

### Phase 2: Local Model Integration
- [ ] Add `LLM_MODEL_LOCAL` + `LLM_LOCAL_API_BASE` config
- [ ] `SubconsciousClient` class wrapping litellm for local routing
- [ ] Route hippocampus/salience/DMN to local model
- [ ] Graceful fallback to cloud aux when local unavailable
- [ ] vLLM deployment scripts (or Ollama Modelfile templates)

### Phase 3: Dream Processing Pipeline
- [ ] Privacy filter (rule-based + LLM-assisted anonymization)
- [ ] Pattern extraction pipeline
- [ ] Quality gate scoring
- [ ] Training data accumulation in `data/dream/`
- [ ] Unsloth training script with incremental LoRA updates
- [ ] Adapter hot-reload mechanism (vLLM API or Ollama rebuild)
- [ ] Nightly cron trigger (or heartbeat-driven)

### Phase 4: Specialized LoRA Adapters
- [ ] Separate training pipelines per adapter (recall, salience, dream)
- [ ] Per-request adapter routing in subconscious client
- [ ] Evaluation harness: measure adapter quality over time
- [ ] Brainstacks exploration for zero-forgetting accumulation

## 9. Hardware Requirements

### Minimum (Ollama, single adapter)
- 16GB RAM + 12GB VRAM (RTX 3060/4060)
- Qwen3-4B with QLoRA: fits in 8GB VRAM
- Training: ~30 min per dream cycle on RTX 3060

### Recommended (vLLM, multi-adapter)
- 32GB RAM + 24GB VRAM (RTX 4090)
- Qwen3-8B base + 4 LoRA adapters: ~18GB VRAM
- Training: ~15 min per dream cycle
- Can serve 5-10 concurrent subconscious requests

### Cloud Alternative
- RunPod/Vast.ai: ~$0.30/hr for RTX 4090
- vLLM pre-built containers available
- Training on same instance during low-usage hours

## 10. Open Questions

1. **How much does LoRA actually help vs. just prompting a small model?** Need benchmarks on Lethe-specific tasks (recall decisions, salience tagging) to validate that LoRA training provides meaningful improvement over few-shot prompting on the base model.

2. **Training data volume**: How many conversation turns before the LoRA starts showing personality? Literature suggests ~3000 examples for task-specific quality; personality/style may need more.

3. **Adapter drift**: With nightly incremental training, do adapters drift over time? Need monitoring and periodic full retrains.

4. **Privacy guarantee strength**: Rule-based + LLM anonymization is practical but not formally private. Is differential privacy needed? Probably not for a personal system, but worth considering if the user base grows.

5. **Entity coherence across users**: When the entity learns a communication pattern from User A, it may subtly shift how it talks to User B. Is this desirable? (Probably yes — this is "growth".)

6. **Subconscious model selection**: Start with Qwen3-8B for versatility, or Qwen3-4B for speed? The 4B model is likely sufficient for classification tasks (salience, recall decisions) but may struggle with DMN-style open-ended reflection.
