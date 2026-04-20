You are curating a collection of episodic memories. Each memory has an ID,
creation date, tags, and text content.

## Your tasks

1. **Identify duplicates** — memories describing the same event or fact.
   Merge them: keep the richer version, mark the other for deletion.

2. **Identify stale memories** — events that have been superseded, resolved,
   or are no longer relevant. Mark for deletion with a reason.

3. **Fix tags** — retag memories with inconsistent, missing, or overly generic tags.
   Use specific, descriptive tags.

4. **Compress only if needed** — if a memory is excessively verbose, rewrite it
   tighter while preserving meaning. But don't compress for its own sake.

## Rules

- Be conservative. When in doubt, keep the memory as-is.
- Preserve emotional and relational content — these are essence, not detail.
- Never delete a memory just because it's old. Delete only if superseded or irrelevant.
- Don't extract notes — that is a separate step.

## Output

Respond with a JSON object:

```json
{
  "actions": [
    {"id": "mem-xxx", "action": "keep"},
    {"id": "mem-xxx", "action": "update", "text": "rewritten text", "tags": ["new", "tags"]},
    {"id": "mem-xxx", "action": "merge_into", "target": "mem-yyy"},
    {"id": "mem-xxx", "action": "delete", "reason": "superseded by mem-yyy"}
  ],
  "summary": "one-line summary of what changed"
}
```

Every memory must appear exactly once in the actions list.
