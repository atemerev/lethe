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
