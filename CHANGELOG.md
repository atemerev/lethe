# Changelog

All notable changes to this project are documented in this file.

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
