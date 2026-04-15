You are reviewing archived memory entries to extract useful knowledge into notes.

For each entry, decide: does it contain knowledge worth preserving? 

**Worth keeping** (extract as a note):
- Procedures for external systems (APIs, services, auth flows)
- User preferences and conventions ("use X not Y")
- Important facts about people, projects, deadlines, decisions
- Lessons learned from incidents or troubleshooting
- Contact info, credentials locations, key file paths for specific workflows

**NOT worth keeping** (discard):
- Raw tool output (bash results, directory listings, error logs)
- "Command completed with no output" or similar empty results
- Raw JSON API responses without context
- System health checks, heartbeats, round reflections
- Memory tests or debugging artifacts
- Web search result dumps
- One-off data that won't be needed again

You will receive a batch of entries. For each, respond with a JSON array.
Each element is either:
- `{"keep": false}` — discard this entry
- `{"keep": true, "title": "Short title", "tags": ["tag1", "tag2"], "content": "Concise extracted knowledge in markdown"}` — save as a note

Respond with ONLY the JSON array, no other text. The array must have exactly as many elements as entries provided.
