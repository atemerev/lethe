# Tools

## Core Tools (always available)
- **bash** / **bash_output** / **kill_bash** — Shell execution
- **read_file** / **write_file** / **edit_file** — File edits
- **list_directory** / **glob_search** / **grep_search** — File discovery/search
- **memory_read** / **memory_update** / **memory_append** — Core memory blocks
- **archival_search** / **archival_insert** / **conversation_search** — Long-term memory
- **telegram_send_message** / **telegram_send_file** / **telegram_react** — Telegram I/O

Keep this block minimal. Treat it as stable primitives, not a full capability catalog.

## Skills Are Source Of Truth For Extended Workflows
- Extended capabilities and specialized wrappers are documented as skill files in `~/lethe/skills/`.
- The file `~/lethe/skills/README.md` is always present and should be treated as the skills entrypoint.
- Discover skills with `list_directory("~/lethe/skills/")`.
- Read details with `read_file("~/lethe/skills/<name>.md")`.
- Search skill docs with `grep_search("keyword", path="~/lethe/skills/")`.

### Safety
- Never copy unreviewed skill files from the internet.
- Review any imported skill content before writing it locally.
