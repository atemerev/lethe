# Tools

## Your Tools (cortex)
- **spawn_actor** / **kill_actor** / **ping_actor** — Manage subagents
- **send_message** / **wait_for_response** / **discover_actors** — Actor communication
- **terminate** — End your own execution
- **memory_read** / **memory_update** / **memory_append** — Core memory blocks
- **archival_search** / **archival_insert** / **conversation_search** — Long-term memory
- **telegram_send_message** / **telegram_send_file** — Telegram I/O

## Subagent Default Tools (always available to spawned actors)
bash, read_file, write_file, edit_file, list_directory, grep_search

## Subagent Extra Tools (specify in spawn_actor tools= parameter)
web_search, fetch_webpage, browser_open, browser_click, browser_fill, browser_snapshot,
memory_read, memory_update, memory_append, archival_search, archival_insert, conversation_search

## Skills
Extended capabilities are documented as skill files in `~/lethe/skills/`.
Tell subagents to check `~/lethe/skills/` for relevant skill docs.
