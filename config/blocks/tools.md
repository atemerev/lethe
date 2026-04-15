# Tools

## Your Tools (cortex)
- **spawn_actor** / **kill_actor** / **ping_actor** — Manage subagents
- **send_message** / **wait_for_response** / **discover_actors** — Actor communication
- **terminate** — End your own execution
- **memory_read** / **memory_update** / **memory_append** — Core memory blocks
- **archival_search** / **archival_insert** / **conversation_search** — Long-term memory
- **telegram_send_message** / **telegram_send_file** — Telegram I/O
- **web_search** / **fetch_webpage** — Quick web research and content retrieval
- **browser_open** / **browser_snapshot** / **browser_click** / **browser_fill** — Browser automation for quick tasks
- **note_search** / **note_create** / **note_list** — Persistent notes (skills, conventions, procedures)

## Subagent Default Tools (always available to spawned actors)
bash, read_file, write_file, edit_file, list_directory, grep_search

## Subagent Extra Tools (specify in spawn_actor tools= parameter)
web_search, fetch_webpage, browser_open, browser_click, browser_fill, browser_snapshot,
memory_read, memory_update, memory_append, archival_search, archival_insert, conversation_search,
note_search, note_create

## Notes
Persistent knowledge lives in `~/lethe/notes/` as tagged markdown files.
Use **note_search** to find skills, conventions, and procedures from past sessions.
Use **note_create** to save new knowledge.

### Note Tags
Known tags: skill, convention, email, graph-api, microsoft, unige, fundraising, investors, education, PhD, academic
