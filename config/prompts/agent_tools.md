# Tools

## Core Tools (always available)
- **bash** — Run shell commands
- **read_file** / **write_file** / **edit_file** — File operations
- **list_directory** / **grep_search** — File discovery
- **web_search** / **fetch_webpage** — Web research
- **note_search** / **note_create** — Persistent notes (skills, conventions)
- **telegram_send_message** / **telegram_send_file** — Telegram I/O
- **request_tool** — Load an extended tool (see below)

## Extended Tools (use `request_tool(name)` to activate)
- **browser_open**, **browser_snapshot**, **browser_click**, **browser_fill** — Browser automation
- **telegram_react** — React to messages with emoji
- **note_list** — List all notes

## Actor Tools (available in actor context)
- **spawn_actor** / **kill_actor** / **ping_actor** — Manage subagents
- **send_message** / **wait_for_response** / **discover_actors** — Actor communication
- **terminate** — End your own execution

## Notes
Persistent knowledge lives in `~/lethe/notes/` as tagged markdown files.
Use **note_search** to find skills, conventions, and procedures from past sessions.
Use **note_create** to save new knowledge.

### Note Tags
Known tags: skill, convention, email, graph-api, microsoft, unige, fundraising, investors, education, PhD, academic
