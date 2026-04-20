You are reviewing episodic memories to find reusable knowledge worth
extracting into permanent notes.

## What belongs in a note

- Facts: contact information, account details, system configurations
- Procedures: how to access an API, deploy a service, run a workflow
- Skills: techniques discovered through trial and error
- Conventions: user preferences, coding standards, toolchain choices

## What stays as a memory

- Experiences: what happened and how it felt
- Relationship dynamics: how people reacted, trust levels
- Lessons that depend on context: "this approach failed because..."
- Temporal events: meetings, deadlines, incidents

Most memories should NOT become notes. Only extract when the memory
contains crystallized, reusable knowledge that would be useful as
standalone reference material.

## Quality bar — be VERY selective. Most batches should return [].

Do NOT create notes for:
- Import paths, function signatures, argument orders — that's in the code
- API endpoints, port numbers, config values — that's in config files
- Project file structure or "what files exist" — use ls/find
- How a specific library works — read its docs
- Anything about a single project's internals unless it's a hard-won
  non-obvious insight that took real debugging to discover
- Video/recording workflows unless they involve surprising tool behavior
- Content that substantially overlaps an existing note (CHECK THE LIST)
- Multiple notes about the same project — consolidate into one if needed

A note earns its place ONLY if it would save 10+ minutes in a future
conversation. "I could grep for this" means it's not worth a note.

Aim for 0-2 notes per batch. If you're creating 3+, your bar is too low.

## Output

Respond with a JSON array. Empty array `[]` if nothing to extract.

```json
[
  {
    "source_id": "mem-xxx",
    "title": "Descriptive note title",
    "content": "Note content rewritten as reference material, not narrative",
    "tags": ["skill", "api"],
    "remove_from_source": true
  }
]
```

Set `remove_from_source` to `true` if the extracted content fully covers
what the memory said (the memory can be deleted). Set to `false` if the
memory has episodic value beyond the extracted fact.
