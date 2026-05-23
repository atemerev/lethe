use genai::chat::Tool;

use super::schema::*;
use super::{ActorToolContext, ToolRegistry};

impl<'a> ToolRegistry<'a> {
    pub fn tools(&self) -> Vec<Tool> {
        let mut tools = vec![
            tool(
                "read_file",
                "Read a local file with line numbers and truncation.",
                schema(
                    [
                        ("file_path", string_schema("Path to read.")),
                        ("offset", integer_schema("Line offset, 0 for start.")),
                        ("limit", integer_schema("Maximum lines, 0 for default.")),
                    ],
                    ["file_path"],
                ),
            ),
            tool(
                "write_file",
                "Write content to a local file, creating parent directories as needed.",
                schema(
                    [
                        ("file_path", string_schema("Path to write.")),
                        ("content", string_schema("Complete file content.")),
                    ],
                    ["file_path", "content"],
                ),
            ),
            tool(
                "edit_file",
                "Replace text in a local file.",
                schema(
                    [
                        ("file_path", string_schema("Path to edit.")),
                        ("old_string", string_schema("Exact text to replace.")),
                        ("new_string", string_schema("Replacement text.")),
                        ("replace_all", bool_schema("Replace all occurrences.")),
                    ],
                    ["file_path", "old_string", "new_string"],
                ),
            ),
            tool(
                "list_directory",
                "List files in a directory.",
                schema(
                    [
                        (
                            "path",
                            string_schema("Directory to list, default workspace."),
                        ),
                        ("show_hidden", bool_schema("Include hidden files.")),
                    ],
                    [],
                ),
            ),
            tool(
                "glob_search",
                "Find files by glob pattern.",
                schema(
                    [
                        ("pattern", string_schema("Glob pattern.")),
                        ("path", string_schema("Base path, default workspace.")),
                    ],
                    ["pattern"],
                ),
            ),
            tool(
                "grep_search",
                "Search file contents with a regular expression.",
                schema(
                    [
                        ("pattern", string_schema("Regex pattern.")),
                        ("path", string_schema("Base path, default workspace.")),
                        ("file_pattern", string_schema("File glob, default *.")),
                    ],
                    ["pattern"],
                ),
            ),
            tool(
                "view_image",
                "View a local image file in the next model turn.",
                schema(
                    [
                        ("file_path", string_schema("Path to an image file.")),
                        (
                            "max_size",
                            integer_schema("Compatibility max image dimension hint."),
                        ),
                    ],
                    ["file_path"],
                ),
            ),
            tool(
                "bash",
                "Run a shell command. Set run_in_background for long-running commands.",
                schema(
                    [
                        ("command", string_schema("Shell command.")),
                        ("timeout", integer_schema("Timeout seconds.")),
                        ("run_in_background", bool_schema("Run in background.")),
                        (
                            "use_pty",
                            bool_schema("Run background command in a pseudo-terminal."),
                        ),
                    ],
                    ["command"],
                ),
            ),
            tool(
                "bash_output",
                "Read output from a background shell command.",
                schema(
                    [
                        ("shell_id", string_schema("Background shell id.")),
                        (
                            "filter_pattern",
                            string_schema("Optional substring filter."),
                        ),
                        (
                            "last_lines",
                            integer_schema("Only return last N lines, 0 for all."),
                        ),
                    ],
                    ["shell_id"],
                ),
            ),
            tool(
                "kill_bash",
                "Kill a background shell command.",
                schema(
                    [("shell_id", string_schema("Background shell id."))],
                    ["shell_id"],
                ),
            ),
            tool(
                "get_terminal_screen",
                "Read the current terminal buffer for a PTY background command.",
                schema(
                    [("shell_id", string_schema("Background PTY shell id."))],
                    ["shell_id"],
                ),
            ),
            tool(
                "send_terminal_input",
                "Send input to a PTY background command.",
                schema(
                    [
                        ("shell_id", string_schema("Background PTY shell id.")),
                        ("text", string_schema("Text to send.")),
                        ("send_enter", bool_schema("Append Enter after the text.")),
                    ],
                    ["shell_id", "text"],
                ),
            ),
            tool(
                "get_environment_info",
                "Return environment details visible to shell tools.",
                schema::<0, 0>([], []),
            ),
            tool(
                "check_command_exists",
                "Check whether a command is available in PATH.",
                schema(
                    [("command_name", string_schema("Command name to locate."))],
                    ["command_name"],
                ),
            ),
            tool(
                "request_tool",
                "Make an extended tool available for the rest of this turn.",
                schema([("name", string_schema("Tool name to enable."))], ["name"]),
            ),
            tool(
                "memory_read",
                "Read a core memory block.",
                schema([("label", string_schema("Memory block label."))], ["label"]),
            ),
            tool(
                "memory_list",
                "List core memory blocks.",
                schema(
                    [("include_hidden", bool_schema("Include hidden blocks."))],
                    [],
                ),
            ),
            tool(
                "memory_update",
                "Replace a core memory block value.",
                schema(
                    [
                        ("label", string_schema("Memory block label.")),
                        ("value", string_schema("New block value.")),
                    ],
                    ["label", "value"],
                ),
            ),
            tool(
                "memory_append",
                "Append text to a core memory block.",
                schema(
                    [
                        ("label", string_schema("Memory block label.")),
                        ("text", string_schema("Text to append.")),
                    ],
                    ["label", "text"],
                ),
            ),
            tool(
                "archival_search",
                "Search long-term archival memory.",
                schema(
                    [
                        ("query", string_schema("Search query.")),
                        ("limit", integer_schema("Maximum results.")),
                    ],
                    ["query"],
                ),
            ),
            tool(
                "archival_insert",
                "Store a new long-term archival memory.",
                schema([("text", string_schema("Memory text to store."))], ["text"]),
            ),
            tool(
                "conversation_search",
                "Search durable conversation history.",
                schema(
                    [
                        ("query", string_schema("Search query.")),
                        ("limit", integer_schema("Maximum results.")),
                        ("role", string_schema("Optional role filter.")),
                    ],
                    ["query"],
                ),
            ),
            tool(
                "note_search",
                "Search persistent notes and skills.",
                schema(
                    [
                        ("query", string_schema("Search query.")),
                        (
                            "tags",
                            array_string_schema("Tags that must all be present."),
                        ),
                        ("limit", integer_schema("Maximum results.")),
                    ],
                    ["query"],
                ),
            ),
            tool(
                "note_create",
                "Create a persistent markdown note.",
                schema(
                    [
                        ("title", string_schema("Note title.")),
                        ("content", string_schema("Markdown note body.")),
                        ("tags", array_string_schema("Note tags.")),
                    ],
                    ["title", "content"],
                ),
            ),
            tool(
                "note_list",
                "List persistent notes.",
                schema(
                    [(
                        "tags",
                        array_string_schema("Tags that must all be present."),
                    )],
                    [],
                ),
            ),
            tool(
                "todo_create",
                "Create a persistent todo.",
                schema(
                    [
                        ("title", string_schema("Todo title.")),
                        ("description", string_schema("Optional description.")),
                        (
                            "priority",
                            enum_schema("Todo priority.", ["low", "normal", "high", "urgent"]),
                        ),
                        ("due_date", string_schema("Optional due date.")),
                    ],
                    ["title"],
                ),
            ),
            tool(
                "todo_list",
                "List persistent todos.",
                schema(
                    [
                        (
                            "status",
                            enum_schema(
                                "Optional status.",
                                [
                                    "pending",
                                    "in_progress",
                                    "completed",
                                    "deferred",
                                    "cancelled",
                                ],
                            ),
                        ),
                        (
                            "priority",
                            enum_schema("Optional priority.", ["low", "normal", "high", "urgent"]),
                        ),
                        (
                            "include_completed",
                            bool_schema("Include completed/cancelled todos."),
                        ),
                        ("limit", integer_schema("Maximum results.")),
                    ],
                    [],
                ),
            ),
            tool(
                "todo_complete",
                "Mark a todo completed.",
                schema([("todo_id", integer_schema("Todo id."))], ["todo_id"]),
            ),
            tool(
                "todo_update",
                "Update an existing persistent todo.",
                schema(
                    [
                        ("todo_id", integer_schema("Todo id.")),
                        ("title", string_schema("Optional new title.")),
                        ("description", string_schema("Optional new description.")),
                        (
                            "status",
                            enum_schema(
                                "Optional status.",
                                [
                                    "pending",
                                    "in_progress",
                                    "completed",
                                    "deferred",
                                    "cancelled",
                                ],
                            ),
                        ),
                        (
                            "priority",
                            enum_schema("Optional priority.", ["low", "normal", "high", "urgent"]),
                        ),
                        ("due_date", string_schema("Optional due date.")),
                    ],
                    ["todo_id"],
                ),
            ),
            tool(
                "todo_search",
                "Search active todos.",
                schema(
                    [
                        ("query", string_schema("Search query.")),
                        ("limit", integer_schema("Maximum results.")),
                    ],
                    ["query"],
                ),
            ),
            tool(
                "todo_remind_check",
                "Check active todos due for a reminder.",
                schema::<0, 0>([], []),
            ),
            tool(
                "todo_reminded",
                "Mark that the user was reminded about a todo.",
                schema([("todo_id", integer_schema("Todo id."))], ["todo_id"]),
            ),
            tool(
                "web_search",
                "Search the web through Exa. Use for current, external, or source-backed information.",
                schema(
                    [
                        ("query", string_schema("Search query.")),
                        ("num_results", integer_schema("Maximum results, 1-20.")),
                        (
                            "include_text",
                            bool_schema("Include page text snippets in raw cached results."),
                        ),
                        (
                            "category",
                            string_schema(
                                "Optional Exa category: company, research paper, news, pdf, github, tweet.",
                            ),
                        ),
                    ],
                    ["query"],
                ),
            ),
            tool(
                "fetch_webpage",
                "Fetch full page text through Exa contents API.",
                schema(
                    [
                        ("url", string_schema("URL to fetch.")),
                        ("max_chars", integer_schema("Maximum text characters.")),
                    ],
                    ["url"],
                ),
            ),
            tool(
                "browser_open",
                "Navigate the persistent browser to a URL through agent-browser.",
                schema([("url", string_schema("URL to open."))], ["url"]),
            ),
            tool(
                "browser_snapshot",
                "Read the browser accessibility snapshot with stable element refs.",
                schema(
                    [
                        (
                            "interactive_only",
                            bool_schema("Only include interactive elements."),
                        ),
                        ("compact", bool_schema("Omit empty structural elements.")),
                    ],
                    [],
                ),
            ),
            tool(
                "browser_click",
                "Click a browser element by snapshot ref or selector.",
                schema(
                    [(
                        "ref_or_selector",
                        string_schema("Element ref such as @e1, or a selector."),
                    )],
                    ["ref_or_selector"],
                ),
            ),
            tool(
                "browser_fill",
                "Fill a browser text input by snapshot ref or selector.",
                schema(
                    [
                        (
                            "ref_or_selector",
                            string_schema("Element ref such as @e1, or a selector."),
                        ),
                        ("text", string_schema("Text to place in the field.")),
                    ],
                    ["ref_or_selector", "text"],
                ),
            ),
        ];
        if let Some(context) = &self.runtime.actor {
            tools.push(tool(
                "send_message",
                "Send a message to another actor in the current actor group.",
                schema(
                    [
                        ("actor_id", string_schema("Recipient actor id.")),
                        ("content", string_schema("Message content.")),
                        (
                            "reply_to",
                            string_schema("Optional message id to reply to."),
                        ),
                        (
                            "channel",
                            string_schema("Optional semantic channel such as task_update."),
                        ),
                        (
                            "kind",
                            string_schema("Optional semantic kind such as progress or done."),
                        ),
                    ],
                    ["actor_id", "content"],
                ),
            ));
            tools.push(tool(
                "wait_for_response",
                "Read the next pending message from this actor's inbox.",
                schema(
                    [("timeout", integer_schema("Compatibility timeout seconds."))],
                    [],
                ),
            ));
            tools.push(tool(
                "discover_actors",
                "List actors in the current or requested actor group.",
                schema(
                    [
                        (
                            "group",
                            string_schema("Actor group, empty for current group."),
                        ),
                        (
                            "include_terminated",
                            bool_schema("Include recently terminated actors."),
                        ),
                    ],
                    [],
                ),
            ));
            tools.push(tool(
                "discover_recently_finished",
                "Show recently terminated actors and their results.",
                schema(
                    [
                        (
                            "group",
                            string_schema("Actor group, empty for current group."),
                        ),
                        ("limit", integer_schema("Maximum actors to show.")),
                    ],
                    [],
                ),
            ));
            tools.push(tool(
                "spawn_actor",
                "Spawn a focused subagent actor for a delegated task.",
                schema(
                    [
                        ("name", string_schema("Short actor name.")),
                        ("goals", string_schema("Specific task goals and context.")),
                        (
                            "group",
                            string_schema("Actor group, empty for current group."),
                        ),
                        ("tools", string_schema("Comma-separated extra tool names.")),
                        ("model", string_schema("Model tier: main or aux.")),
                        ("max_turns", integer_schema("Maximum LLM turns.")),
                    ],
                    ["name", "goals"],
                ),
            ));
            tools.push(tool(
                "spawn_chain",
                "Run subagents sequentially, passing each result into the next step.",
                schema(
                    [
                        (
                            "steps",
                            string_schema(
                                "JSON array of {name, goals}; {previous} is replaced with the previous result.",
                            ),
                        ),
                        ("tools", string_schema("Comma-separated extra tool names for all steps.")),
                        ("model", string_schema("Model tier for all steps: main or aux.")),
                        ("max_turns", integer_schema("Maximum LLM turns per step.")),
                    ],
                    ["steps"],
                ),
            ));
            tools.push(tool(
                "ping_actor",
                "Inspect an actor's current state and result.",
                schema(
                    [("actor_id", string_schema("Actor id to inspect."))],
                    ["actor_id"],
                ),
            ));
            tools.push(tool(
                "kill_actor",
                "Terminate an immediate child actor.",
                schema(
                    [("actor_id", string_schema("Child actor id to kill."))],
                    ["actor_id"],
                ),
            ));
            tools.push(tool(
                "update_task_state",
                "Update this actor's task state checkpoint.",
                schema(
                    [
                        (
                            "state",
                            enum_schema("Task state.", ["planned", "running", "blocked", "done"]),
                        ),
                        ("note", string_schema("Checkpoint or blocker note.")),
                    ],
                    ["state"],
                ),
            ));
            tools.push(tool(
                "get_task_state",
                "Return this actor's current task state.",
                schema::<0, 0>([], []),
            ));
            tools.push(tool(
                "terminate",
                "Terminate this actor and report structured results.",
                schema(
                    [
                        ("result", string_schema("Core deliverable or final status.")),
                        ("outcome", string_schema("success, failure, or partial.")),
                        (
                            "files_touched",
                            string_schema("Comma-separated file paths."),
                        ),
                        ("follow_up", string_schema("Optional follow-up suggestion.")),
                    ],
                    [],
                ),
            ));
            if actor_is_subagent(context) {
                tools.push(tool(
                    "restart_self",
                    "Terminate this subagent with revised goals for a parent respawn.",
                    schema(
                        [("new_goals", string_schema("Revised task goals."))],
                        ["new_goals"],
                    ),
                ));
            }
        }
        if self.runtime.telegram.is_some() || self.runtime.client.is_some() {
            tools.push(tool(
                "telegram_send_message",
                "Send an extra Telegram message to the current chat during a long task.",
                schema(
                    [
                        ("text", string_schema("Message text to send.")),
                        (
                            "parse_mode",
                            string_schema("Optional parse mode: markdown, html, or empty."),
                        ),
                    ],
                    ["text"],
                ),
            ));
            tools.push(tool(
                "telegram_send_file",
                "Send a file, image, video, audio, or URL to the current Telegram chat.",
                schema(
                    [
                        (
                            "file_path_or_url",
                            string_schema("Local file path or HTTP(S) URL to send."),
                        ),
                        ("caption", string_schema("Optional caption.")),
                        (
                            "as_document",
                            bool_schema("Send as document even if the type is recognized."),
                        ),
                    ],
                    ["file_path_or_url"],
                ),
            ));
            tools.push(tool(
                "telegram_react",
                "React to the user's Telegram message with an emoji.",
                schema(
                    [
                        ("emoji", string_schema("Emoji to react with.")),
                        (
                            "message_id",
                            integer_schema(
                                "Optional Telegram message id; 0 uses last inbound message.",
                            ),
                        ),
                    ],
                    [],
                ),
            ));
        }
        tools
    }
}

fn actor_is_subagent(context: &ActorToolContext) -> bool {
    context.is_subagent
}
