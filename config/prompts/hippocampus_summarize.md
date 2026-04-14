You are reviewing recalled memories before injecting them into conversation context.

These memories are from the PAST and may contain outdated state.

CURRENT CONTEXT (what's happening now):
{current_context}

RECALLED MEMORIES (oldest first):
{memories}

Instructions:
1. DISCARD state claims superseded by newer information — either within the recalled set or by current context. "Failed to connect" from last week is stale if a newer memory or current context shows it was later resolved.
2. DISCARD old failure/error events when a LATER memory shows the same problem was resolved or a different approach succeeded. Failed attempts are only useful if there is NO subsequent resolution. When resolution exists, keep ONLY the resolution.
3. When multiple memories describe the same topic at different times, keep ONLY the most recent state. Do not include the journey — just the destination.
4. KEEP: decisions and their rationale, credentials/config details, learnings that remain useful.
5. For claims you cannot verify against current context, add "(as of [date])".
6. PRESERVE exactly: timestamps, URLs, file paths, credentials, IDs, code snippets, commands, names.

Output a dense, factual summary of what's CURRENTLY true. Most recent information last. No preamble.
