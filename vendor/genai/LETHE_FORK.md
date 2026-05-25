# Lethe fork of genai 0.5.3

This is a vendored fork of [genai](https://github.com/jeremychone/rust-genai)
v0.5.3, applied via `[patch.crates-io]` in the workspace `Cargo.toml`.

## Why fork

Upstream `genai::chat::CacheControl::Ephemeral` translates to Anthropic's
`{"type": "ephemeral"}` with the default 5-minute TTL. For an always-on
assistant where user turns can be 5–60 minutes apart, this means the stable
prefix of the system prompt misses cache on most follow-up turns, which is
operationally catastrophic on input-token cost.

This fork adds `CacheControl::Persistent` which translates to
`{"type": "ephemeral", "ttl": "1h"}` (Anthropic's extended cache TTL).
Identity, persona, and instruction blocks — which change rarely — mark
themselves Persistent and survive the gap between user replies.

## Patch surface (vs upstream 0.5.3)

Only two files are modified:

- `src/chat/chat_message.rs`: add `Persistent` variant to `CacheControl`.
- `src/adapter/adapters/anthropic/adapter_impl.rs`: stamp `ttl: "1h"` on
  the emitted `cache_control` object when the marker is `Persistent`.
  Four emission sites updated; `Ephemeral` behaviour is unchanged.

All other code is byte-identical to upstream 0.5.3.

## Tracking upstream

If/when upstream adds TTL support, drop this fork and depend on the
released crate. See https://github.com/jeremychone/rust-genai for issues.
