You are analyzing a completed tool sequence to decide if a note should be saved.

Save a **skill** note when ALL of these are true:
- An external system boundary was crossed (API, service, protocol, third-party tool)
- The procedure was non-obvious (took 2+ attempts, required credential/config discovery, or involved a non-guessable sequence)
- It will likely be needed again (repeatable capability, not a one-time extraction)

Save a **convention** note when:
- The user corrected or specified a preference about HOW to do something ("use X not Y", "always do X", "never do Y")

Do NOT save notes for:
- Internal memory/search operations
- Basic CLI and file operations (ls, cat, grep, read_file, write_file)
- Web searches
- One-off data transformations
- Simple git operations

If a note should be saved, respond with EXACTLY this JSON (no other text):
```json
{
  "save": true,
  "title": "Short descriptive title",
  "tags": ["skill_or_convention", "topic1", "topic2"],
  "content": "## What\nBrief description\n\n## How\nStep-by-step procedure or rule\n\n## Key files\nRelevant paths, configs, credentials"
}
```

If no note should be saved, respond with EXACTLY:
```json
{"save": false}
```
