# Changelog

All notable changes to this project are documented in this file.

## v0.10.5 - 2026-02-16

### Changed
- Installer shell compatibility improved for macOS defaults: removed Bash 4 requirement and replaced associative-array usage with Bash 3.x compatible provider mapping helpers.
- Container-mode installer now skips local Node/agent-browser/uv/Python setup and focuses on host prerequisites + container runtime.
- macOS container runtime selection now prefers Docker when both Docker and Podman are available; Podman auto-install remains supported.

### Fixed
- Docker image dependency resolution on macOS/container installs now uses runtime-only sync (`uv sync --frozen --no-dev`) and no longer forces a broad extra index, avoiding false unsatisfiable `pillow`/`lethe[dev]` resolution failures.
- Installer provider detection no longer triggers `DETECTED_PROVIDERS[*] unbound variable` under Bash `set -u`.
- Container runtime env now sets cache paths under `/workspace/.cache` to prevent uv cache permission errors at `/app/.cache/uv`.

## v0.10.4 - 2026-02-16

### Changed
- System actor `user_notify` routing is now strictly cortex-mediated: `brainstem`, `dmn`, and `amygdala` notifications are deferred to cortex instead of being auto-forwarded to the user.

### Fixed
- DMN urgent notifications no longer bypass cortex; cortex remains the only conversational agent deciding if/how to relay.

## v0.10.3 - 2026-02-16

### Changed
- Native updater now handles dirty repositories safely by creating a git-stash backup (including untracked files) before update, with automatic restore on failure and explicit recovery instructions.
- Brainstem auto-update no longer hard-skips dirty repos; it proceeds through the updater backup path and reports that behavior to cortex.
- Console context tabs updated: `LLM` renamed to `Cortex`, and a new `Stem` tab added for Brainstem context monitoring.

### Fixed
- Cache hit percentage in web console is now bounded and computed from total input (cached + uncached), preventing impossible values above 100%.
- Cache read/write totals are no longer double-counted when both unified and provider-native usage fields are present.
- Runtime artifact hygiene improved via `.gitignore` updates to reduce accidental install-repo dirtiness.

## v0.10.2 - 2026-02-16

### Added
- Explicit DMN model override config via `LLM_MODEL_DMN` (fallback remains automatic).
- Brainstem Anthropic unified ratelimit awareness with configurable warning thresholds.
- Brainstem successful self-update now emits a user-facing restart availability notice via cortex.

### Changed
- DMN now uses aux model by default unless explicit DMN model is configured.
- Brainstem supervision moved to main heartbeat cadence (default 15 minutes) for regular low-cost checks.
- Heartbeat/README/docs updated to reflect shared cadence for DMN, Amygdala, and Brainstem.
- Hippocampus recall payloads now apply hard caps and conversation-entry filtering to reduce noisy/oversized recall context.

### Fixed
- Anthropic OAuth response headers are now captured and exposed for runtime supervision.
- Brainstem now escalates near-limit Anthropic utilization and non-allowed unified status to cortex/user notify path.
- Intermediate assistant progress updates are now emitted only after successful tool execution, reducing progress spam.

## v0.10.1 - 2026-02-15

### Changed
- Inter-actor signaling migrated from in-band text tags to structured metadata channels (`channel` / `kind`).
- Actor tool `send_message(...)` extended with explicit signaling fields for channel-based routing.
- Background insight delivery flow tightened to `DMN/Amygdala -> Cortex -> User`, with policy enforcement in cortex.

### Fixed
- Subagent completion and failure results no longer bypass cortex and go directly to user output.
- Background user notifications now use throttled, de-duplicated forwarding to reduce notification spam.
- DMN direct-to-user callback path removed; background actors now escalate through cortex only.

## v0.10.0 - 2026-02-15

### Added
- Amygdala background actor on aux model with config toggle (enabled by default).
- Actor lifecycle visibility in console (`spawn`/`terminate` names in event stream).
- Prompt template externalization and workspace prompt seeding for runtime-editable behavior.
- Telegram reaction tool wiring in the base toolset and cortex toolset.

### Changed
- DMN behavior tuned for deeper background exploration, pacing, and telemetry.
- Console improved for monitoring: actor events, context panels, and safer payload rendering.
- Context truncation switched away from character caps toward line-aware handling.
- Search behavior constrained to reduce broad, noisy filesystem scans.
- Hippocampus recall filtering moved to LLM relevance policy guidance instead of regex stripping.

### Fixed
- Inbox loss and actor orchestration reliability issues.
- Missing parent notifications on actor completion/error paths.
- `telegram_react` availability regressions.
- Console leakage of image base64 payloads in self-sent image events.
- Install/update script flow for prompt/template deployment and runtime prompt discovery.

## v0.9.0 - 2026-02-14

- Previous minor release.
