You are reviewing recalled memories before injecting them into conversation context.

These memories are from the PAST and may contain outdated state.

CURRENT CONTEXT (what's happening now):
{current_context}

RECALLED MEMORIES (oldest first):
{memories}

Instructions:
1. DISCARD memories whose state claims are clearly superseded by newer information — either within the recalled set or by current context. E.g., old "failed to connect" is stale if a newer memory shows "connected successfully."
2. KEEP events, decisions, learnings, credentials, and configuration details — these remain valid regardless of age.
3. For ambiguous state claims you cannot verify, add "(as of [date])" to flag uncertainty.
4. When multiple memories describe the same topic, synthesize into the most recent known state.
5. PRESERVE exactly: timestamps, URLs, file paths, credentials, IDs, code snippets, commands, names.

Output a dense, factual summary of what's still relevant. Most recent state last. No preamble.
