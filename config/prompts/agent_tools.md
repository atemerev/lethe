# Tools

## Core Tools (always available)
- **bash** — Run shell commands
- **read_file** / **write_file** / **edit_file** — File operations
- **list_directory** / **grep_search** — File discovery
- **web_search** — Web research
- **memory_read** / **memory_update** — Core memory blocks
- **note_search** / **note_create** — Persistent notes (skills, conventions)
- **conversation_search** — Conversation history search
- **todo_create** / **todo_list** — Basic todos
- **telegram_send_message** / **telegram_send_file** / **telegram_react** — Telegram I/O when Telegram is active
- **request_tool** — Load an extended tool (see below)

## Extended Tools (use `request_tool(name)` to activate)
- **fetch_webpage** — Fetch full webpage text after search
- **browser_open**, **browser_snapshot**, **browser_click**, **browser_fill** — Browser automation
- **bash_output**, **kill_bash**, **get_terminal_screen**, **send_terminal_input** — Background shell controls
- **glob_search**, **view_image** — Additional file/image tools
- **memory_list**, **memory_append**, **archival_search**, **archival_insert** — Additional memory tools
- **note_list** — List all notes
- **todo_update**, **todo_complete**, **todo_search**, **todo_remind_check**, **todo_reminded** — Todo maintenance

## Actor Tools (available in actor context)
- **spawn_actor** / **kill_actor** / **ping_actor** — Manage subagents
- **send_message** / **wait_for_response** / **discover_actors** — Actor communication
- **spawn_chain** — Run sequential subagents with `{previous}` result passing
- **update_task_state** / **get_task_state** — Track actor state
- **terminate** / **restart_self** — End or restart your own execution

## Notes
Persistent knowledge lives in `notes/` under the configured workspace as tagged markdown files.
Use **note_search** to find skills, conventions, and procedures from past sessions.
Use **note_create** to save new knowledge.

### Note Tags
Known tags: skill, convention, email, graph-api, microsoft, unige, fundraising, investors, education, PhD, academic
