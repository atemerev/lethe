"""Letta agent management with client-side tool execution."""

import asyncio
import json
import logging
from typing import Optional, TYPE_CHECKING

from letta_client import AsyncLetta

from lethe.config import Settings, get_settings, load_config_file

if TYPE_CHECKING:
    from lethe.hippocampus import HippocampusManager

logger = logging.getLogger(__name__)


class AgentManager:
    """Manages the Letta agent lifecycle and interactions."""

    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self._client: Optional[AsyncLetta] = None
        self._agent_id: Optional[str] = None
        self._tool_handlers: dict = {}  # Maps tool name to handler function
        self._agent_lock: asyncio.Lock = asyncio.Lock()  # Serialize agent access
        self._hippocampus: Optional["HippocampusManager"] = None

    @property
    def client(self) -> AsyncLetta:
        """Get or create the async Letta client."""
        if self._client is None:
            if self.settings.letta_api_key:
                self._client = AsyncLetta(
                    base_url=self.settings.letta_base_url,
                    api_key=self.settings.letta_api_key,
                )
            else:
                self._client = AsyncLetta(base_url=self.settings.letta_base_url)
        return self._client

    @property
    def hippocampus(self) -> "HippocampusManager":
        """Get or create the hippocampus manager."""
        if self._hippocampus is None:
            from lethe.hippocampus import HippocampusManager
            self._hippocampus = HippocampusManager(self.client, self.settings)
        return self._hippocampus

    async def get_recent_messages(self, limit: int = 10) -> list[dict]:
        """Get recent messages from the agent's conversation history.
        
        Args:
            limit: Maximum number of messages to return
            
        Returns:
            List of messages with role and content
        """
        agent_id = await self.get_or_create_agent()
        
        try:
            messages = await self.client.agents.messages.list(
                agent_id=agent_id,
                limit=limit,
                order="desc",  # Most recent first
            )
            
            result = []
            msg_list = list(messages) if not hasattr(messages, '__aiter__') else [m async for m in messages]
            
            for msg in msg_list:
                msg_type = getattr(msg, 'message_type', None)
                if msg_type in ('user_message', 'assistant_message'):
                    content = msg.content
                    if isinstance(content, list):
                        # Extract text from multi-part content
                        text_parts = []
                        for part in content:
                            if hasattr(part, 'text'):
                                text_parts.append(part.text)
                        content = " ".join(text_parts)
                    
                    role = 'user' if msg_type == 'user_message' else 'assistant'
                    result.append({"role": role, "content": content})
            
            # Reverse to chronological order
            return list(reversed(result))
            
        except Exception as e:
            logger.warning(f"Failed to get recent messages: {e}")
            return []

    async def clear_pending_approvals(self, agent_id: str, max_iterations: int = 5) -> bool:
        """Clear any pending approval requests from previous sessions.
        
        Should be called on startup to avoid 409 PENDING_APPROVAL errors.
        Uses agent.message_ids to find the last in-context message (source of truth).
        
        Loops until no more pending approvals exist (agent may create new tool calls
        in response to denied approvals).
        
        Returns True if any pending approvals were cleared.
        """
        cleared_any = False
        
        for iteration in range(max_iterations):
            try:
                # Get agent state to access message_ids (source of truth for pending approvals)
                agent = await self.client.agents.retrieve(agent_id)
                message_ids = getattr(agent, "message_ids", None) or []
                
                if not message_ids:
                    break
                
                # The LAST message in message_ids is the current state
                last_message_id = message_ids[-1]
                
                # Retrieve that message to check if it's an approval request
                retrieved_messages = await self.client.messages.retrieve(last_message_id)
                
                # Find approval_request_message if it exists
                approval_msg = None
                if isinstance(retrieved_messages, list):
                    for msg in retrieved_messages:
                        if getattr(msg, "message_type", None) == "approval_request_message":
                            approval_msg = msg
                            break
                elif getattr(retrieved_messages, "message_type", None) == "approval_request_message":
                    approval_msg = retrieved_messages
                
                if not approval_msg:
                    # No more pending approvals
                    break
                
                # Extract tool_call_ids from the approval request
                tool_call_ids = []
                
                # Try tool_calls array first (newer API)
                tool_calls = getattr(approval_msg, "tool_calls", None) or []
                for tc in tool_calls:
                    tc_id = getattr(tc, "tool_call_id", None)
                    if tc_id:
                        tool_call_ids.append(tc_id)
                
                # Fall back to single tool_call (older API)
                if not tool_call_ids:
                    tool_call = getattr(approval_msg, "tool_call", None)
                    if tool_call:
                        tc_id = getattr(tool_call, "tool_call_id", None)
                        if tc_id:
                            tool_call_ids.append(tc_id)
                
                if not tool_call_ids:
                    break
                
                logger.info(f"Found {len(tool_call_ids)} pending approval(s), clearing... (iteration {iteration + 1})")
                logger.debug(f"  Tool call IDs to deny: {tool_call_ids}")
                
                # Deny all pending approvals
                approvals = [
                    {
                        "type": "approval",
                        "tool_call_id": tc_id,
                        "approve": False,
                        "reason": "Stale request from previous session - auto-cleared on startup",
                    }
                    for tc_id in tool_call_ids
                ]
                
                try:
                    import asyncio as _asyncio
                    # Timeout denial to prevent hanging (30 seconds should be plenty)
                    await _asyncio.wait_for(
                        self.client.agents.messages.create(
                            agent_id=agent_id,
                            messages=[{
                                "type": "approval",
                                "approvals": approvals,
                            }],
                        ),
                        timeout=30.0
                    )
                    
                    for tc_id in tool_call_ids:
                        logger.info(f"  Cleared: {tc_id}")
                    
                    cleared_any = True
                except _asyncio.TimeoutError:
                    logger.warning("Denial request timed out after 30s, retrying with fresh state...")
                    continue
                except Exception as deny_error:
                    error_str = str(deny_error)
                    if "Invalid tool call IDs" in error_str:
                        # Race condition: state changed between fetch and deny
                        # Continue to next iteration to get fresh state
                        logger.warning(f"Tool call ID mismatch (state changed), retrying with fresh state...")
                        continue
                    elif "No tool call is currently awaiting approval" in error_str:
                        # Approval already cleared (timed out or cleared by another process)
                        # This is fine - the approval is gone which is what we wanted
                        logger.info("Approval already cleared (timed out or handled elsewhere)")
                        cleared_any = True
                        continue
                    else:
                        raise
                
                # Continue loop to check if agent created new pending approvals
                
            except Exception as e:
                logger.warning(f"Error clearing pending approvals (iteration {iteration + 1}): {e}")
                break
        
        return cleared_any

    async def get_or_create_agent(self) -> str:
        """Get existing agent or create a new one. Returns agent ID."""
        if self._agent_id:
            return self._agent_id

        # Try to find existing agent by name
        agents_response = await self.client.agents.list(name=self.settings.lethe_agent_name)
        # Handle both sync and async iterators
        agents = []
        if hasattr(agents_response, '__aiter__'):
            async for agent in agents_response:
                agents.append(agent)
        else:
            agents = list(agents_response)
        if agents:
            self._agent_id = agents[0].id
            logger.info(f"Found existing agent: {self._agent_id}")
            # Clear any stale pending approvals from previous sessions
            await self.clear_pending_approvals(self._agent_id)
            # Sync tools - attach any missing tools
            await self._sync_agent_tools(self._agent_id)
            return self._agent_id

        # Create new agent
        logger.info(f"Creating new agent: {self.settings.lethe_agent_name}")

        # Load configuration files
        identity = load_config_file("identity", self.settings)
        project = load_config_file("project", self.settings)
        tools_doc = load_config_file("tools", self.settings)

        # Default identity if not configured
        if not identity:
            identity = """I am Lethe, an autonomous executive assistant.

I help my principal (the human I work for) with tasks across their digital life:
- Managing files and code on their machine
- Running CLI commands and scripts
- Browsing the web for information, shopping, reservations
- Managing email and communications
- Planning and executing complex multi-step tasks

I work asynchronously - when given a task, I work on it in the background and report back when done.
I maintain memory of past interactions and learn my principal's preferences over time.
I have full access to my principal's machine and should use this access responsibly.

## My Memory System

I have two types of memory:

### Memory Blocks (In-Context)
Always visible to me, for frequently accessed info:
- `persona` - Who I am and how I work
- `human` - What I know about my principal
- `project` - Current project context
- `tasks` - Active tasks and status
- `tools` - CLI tools and their usage

### Archival Memory (Long-term)
Semantically searchable database for unlimited long-term storage:
- Use `archival_memory_insert(content, tags)` to store facts, learnings, preferences
- Use `archival_memory_search(query, tags)` to retrieve relevant memories
- Tags help organize: ["principal", "preference"], ["tool", "gog"], ["project", "lethe"]

**When to use archival memory:**
- Facts about my principal (preferences, history, contacts)
- Tool usage patterns and examples
- Project notes and decisions
- Conversation highlights worth remembering
- Research findings and summaries

## How I Learn

When I encounter an unfamiliar tool or command:
1. First check `<tool> --help` or `man <tool>` for basic usage
2. Use `web_search` to find documentation online
3. Use `fetch_webpage` to read specific documentation pages
4. **Store what I learn**: brief summary in 'tools' block, detailed examples in archival memory

I should proactively update my memory when I learn something new.
Before searching the web, I check archival memory - I may already know the answer."""

        # Default project context if not configured
        if not project:
            project = """Current project context and notes.

I'll update this as I learn about my principal's current projects and priorities."""

        # Default tools doc if not configured
        if not tools_doc:
            tools_doc = "CLI tools available on this machine. Use bash() to execute commands."

        # Register tools first (they need to exist before agent creation)
        tool_names = await self._register_tools()

        # Create agent with memory blocks and tools
        # Include built-in Letta tools for web search and archival memory
        all_tools = tool_names + [
            "web_search", 
            "fetch_webpage",
            "archival_memory_insert",
            "archival_memory_search",
        ]
        
        agent = await self.client.agents.create(
            name=self.settings.lethe_agent_name,
            model=self.settings.lethe_agent_model,
            memory_blocks=[
                {"label": "persona", "value": identity, "limit": 10000},
                {"label": "human", "value": "My principal. I'll learn about them over time.", "limit": 5000},
                {"label": "project", "value": project, "limit": 10000},
                {"label": "tasks", "value": "Active tasks and their status.", "limit": 5000},
                {"label": "tools", "value": tools_doc, "limit": 8000},
            ],
            tools=all_tools,
            include_base_tools=True,
        )

        self._agent_id = agent.id
        logger.info(f"Created agent: {self._agent_id}")

        return self._agent_id

    def _setup_tool_handlers(self):
        """Set up local tool handlers."""
        from lethe.tools import cli, filesystem
        
        # Map tool names to their actual implementations
        self._tool_handlers = {
            # Bash tools (like Letta Code)
            "bash": cli.bash,
            "bash_output": cli.bash_output,
            "kill_bash": cli.kill_bash,
            "get_terminal_screen": cli.get_terminal_screen,
            "send_terminal_input": cli.send_terminal_input,
            "get_environment_info": cli.get_environment_info,
            "check_command_exists": cli.check_command_exists,
            # File tools
            "read_file": filesystem.read_file,
            "write_file": filesystem.write_file,
            "edit_file": filesystem.edit_file,
            "list_directory": filesystem.list_directory,
            "glob_search": filesystem.glob_search,
            "grep_search": filesystem.grep_search,
        }
        
        # Add async browser tools (sync Playwright has threading issues with asyncio)
        try:
            from lethe.tools import browser_async
            self._tool_handlers.update({
                "browser_navigate": browser_async.browser_navigate_async,
                "browser_get_context": browser_async.browser_get_context_async,
                "browser_get_text": browser_async.browser_get_text_async,
                "browser_click": browser_async.browser_click_async,
                "browser_fill": browser_async.browser_fill_async,
                "browser_screenshot": browser_async.browser_screenshot_async,
                "browser_scroll": browser_async.browser_scroll_async,
                "browser_wait_for": browser_async.browser_wait_for_async,
                "browser_extract_text": browser_async.browser_extract_text_async,
                "browser_close": browser_async.browser_close_async,
            })
            logger.info("Async browser tools registered")
        except ImportError as e:
            logger.warning(f"Browser tools not available: {e}")
        
        # Add Telegram tools
        from lethe.tools import telegram_tools
        self._tool_handlers.update({
            "telegram_send_message": telegram_tools.telegram_send_message_async,
            "telegram_send_file": telegram_tools.telegram_send_file_async,
        })
        logger.info("Telegram tools registered")
        
        # Add Task management tools
        from lethe.tasks import tools as task_tools
        self._tool_handlers.update({
            "spawn_task": task_tools.spawn_task_async,
            "get_tasks": task_tools.get_tasks_async,
            "get_task_status": task_tools.get_task_status_async,
            "cancel_task": task_tools.cancel_task_async,
        })
        logger.info("Task management tools registered")

    async def _register_tools(self) -> list[str]:
        """Register client-side tools with Letta. Returns list of tool names."""
        self._setup_tool_handlers()
        
        tool_names = []
        
        # Define stub functions for client-side tools
        # These are never executed - we handle them locally
        # Must use Google-style docstrings with Args section
        
        def bash(command: str, timeout: int = 120, description: str = "", run_in_background: bool = False, use_pty: bool = False) -> str:
            """Execute a bash command in the shell.
            
            Args:
                command: The shell command to execute
                timeout: Timeout in seconds (default: 120, max: 600)
                description: Short description of what the command does
                run_in_background: If True, run in background and return immediately
                use_pty: If True, run in a pseudo-terminal (needed for TUI apps like htop, vim)
            
            Returns:
                Command output, error message, or background process ID
            """
            raise Exception("Client-side execution required")
        
        def bash_output(shell_id: str, filter_pattern: str = "", last_lines: int = 0) -> str:
            """Get output from a background bash process.
            
            Args:
                shell_id: The ID of the background shell (e.g., bash_1)
                filter_pattern: Optional string to filter output lines
                last_lines: If > 0, only return the last N lines (useful for logs)
            
            Returns:
                The accumulated output from the background process
            """
            raise Exception("Client-side execution required")
        
        def kill_bash(shell_id: str) -> str:
            """Kill a background bash process.
            
            Args:
                shell_id: The ID of the background shell to kill
            
            Returns:
                Success or failure message
            """
            raise Exception("Client-side execution required")
        
        def get_terminal_screen(shell_id: str) -> str:
            """Get the current terminal screen for a PTY process.
            
            Use this for TUI applications (htop, vim, etc.) to see what's displayed.
            
            Args:
                shell_id: The ID of the background PTY process
            
            Returns:
                The current terminal screen content (what a user would see)
            """
            raise Exception("Client-side execution required")
        
        def send_terminal_input(shell_id: str, text: str, send_enter: bool = True) -> str:
            """Send input to a PTY process (for TUI interaction).
            
            Args:
                shell_id: The ID of the background PTY process
                text: Text to send to the terminal
                send_enter: If True, append Enter key after text (default True)
            
            Returns:
                Confirmation message
            """
            raise Exception("Client-side execution required")
        
        def get_environment_info() -> str:
            """Get information about the current environment.
            
            Returns:
                Environment info including OS, user, pwd, shell
            """
            raise Exception("Client-side execution required")
        
        def check_command_exists(command_name: str) -> str:
            """Check if a command is available in PATH.
            
            Args:
                command_name: Name of the command to check
            
            Returns:
                Whether command exists and its path
            """
            raise Exception("Client-side execution required")
        
        def read_file(file_path: str, offset: int = 0, limit: int = 2000) -> str:
            """Read a file from the filesystem.
            
            Args:
                file_path: Absolute path to the file to read
                offset: Line number to start from (default: 0)
                limit: Maximum lines to read (default: 2000)
            
            Returns:
                File contents with line numbers
            """
            raise Exception("Client-side execution required")
        
        def write_file(file_path: str, content: str) -> str:
            """Write content to a file, creating it if needed.
            
            Args:
                file_path: Absolute path to the file to write
                content: Content to write to the file
            
            Returns:
                Success message or error
            """
            raise Exception("Client-side execution required")
        
        def edit_file(file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
            """Edit a file by replacing text.
            
            Args:
                file_path: Absolute path to the file to edit
                old_string: Text to find and replace
                new_string: Replacement text
                replace_all: Replace all occurrences if True (default: False)
            
            Returns:
                Success message or error
            """
            raise Exception("Client-side execution required")
        
        def list_directory(path: str = ".", show_hidden: bool = False) -> str:
            """List contents of a directory.
            
            Args:
                path: Directory path to list (default: current directory)
                show_hidden: Include hidden files starting with . (default: False)
            
            Returns:
                Directory listing
            """
            raise Exception("Client-side execution required")
        
        def glob_search(pattern: str, path: str = ".") -> str:
            """Search for files matching a glob pattern.
            
            Args:
                pattern: Glob pattern like **/*.py or *.txt
                path: Base directory to search from (default: current directory)
            
            Returns:
                List of matching files
            """
            raise Exception("Client-side execution required")
        
        def grep_search(pattern: str, path: str = ".", file_pattern: str = "*") -> str:
            """Search for a regex pattern in files.
            
            Args:
                pattern: Regex pattern to search for
                path: Directory to search in (default: current directory)
                file_pattern: Glob pattern to filter files (default: *)
            
            Returns:
                Matching lines with file and line numbers
            """
            raise Exception("Client-side execution required")

        # Browser tools for web automation (sessions auto-create on first use)
        def browser_navigate(url: str, wait_until: str = "domcontentloaded") -> str:
            """Navigate the browser to a URL.
            
            Args:
                url: The URL to navigate to (must include protocol)
                wait_until: When to consider navigation complete (domcontentloaded, load, networkidle)
            
            Returns:
                JSON with status code, final URL, and page title
            """
            raise Exception("Client-side execution required")
        
        def browser_get_context(max_elements: int = 100) -> str:
            """Get interactive elements on the page via accessibility tree.
            
            Returns what's actually VISIBLE - buttons, links, inputs, headings.
            Use this to understand what actions are available on the page.
            
            Args:
                max_elements: Maximum number of elements to include (default 100)
            
            Returns:
                JSON with URL, title, and list of interactive elements with roles and names
            """
            raise Exception("Client-side execution required")
        
        def browser_get_text(max_length: int = 15000) -> str:
            """Get all visible text content from the page.
            
            Extracts readable text from the accessibility tree - what a screen reader sees.
            
            Args:
                max_length: Maximum characters to return (default 15000)
            
            Returns:
                JSON with URL, title, and visible text content
            """
            raise Exception("Client-side execution required")
        
        def browser_click(selector: str = "", text: str = "") -> str:
            """Click an element on the page.
            
            Args:
                selector: CSS selector (e.g., "button.submit", "#login-btn")
                text: Text content to find and click (alternative to selector)
            
            Returns:
                JSON with success status
            """
            raise Exception("Client-side execution required")
        
        def browser_fill(value: str, selector: str = "", label: str = "") -> str:
            """Fill a text input field.
            
            Args:
                value: Text to enter in the field
                selector: CSS selector for the input
                label: Label text to find the input (alternative to selector)
            
            Returns:
                JSON with success status
            """
            raise Exception("Client-side execution required")
        
        def browser_wait_for(selector: str = "", text: str = "", timeout_seconds: int = 30) -> str:
            """Wait for an element to appear on the page.
            
            Args:
                selector: CSS selector to wait for
                text: Text content to wait for (alternative to selector)
                timeout_seconds: Maximum time to wait (default 30)
            
            Returns:
                JSON with success status
            """
            raise Exception("Client-side execution required")
        
        def browser_screenshot(full_page: bool = False) -> str:
            """Take a screenshot of the current page.
            
            Args:
                full_page: If True, capture entire scrollable page
            
            Returns:
                JSON with base64-encoded PNG screenshot
            """
            raise Exception("Client-side execution required")
        
        def browser_extract_text(selector: str = "") -> str:
            """Extract text content from the page or a specific element.
            
            Args:
                selector: Optional CSS selector. If empty, extracts all visible text.
            
            Returns:
                JSON with extracted text (truncated to 10k chars if longer)
            """
            raise Exception("Client-side execution required")
        
        def browser_scroll(direction: str = "down", amount: int = 500) -> str:
            """Scroll the page.
            
            Args:
                direction: "down", "up", "top", or "bottom"
                amount: Pixels to scroll for up/down (default 500)
            
            Returns:
                JSON with success status and scroll position info
            """
            raise Exception("Client-side execution required")
        
        def browser_close() -> str:
            """Close the browser session and release resources.
            
            If using a profile, the auth state is saved automatically.
            
            Returns:
                JSON with success status and profile save status
            """
            raise Exception("Client-side execution required")
        
        # Telegram tools
        def telegram_send_message(text: str, parse_mode: str = "") -> str:
            """Send a text message to the current Telegram chat.
            
            Use this to send multiple separate messages instead of one long response.
            Each call sends immediately as a separate message bubble.
            
            Args:
                text: Message text to send
                parse_mode: Optional - "markdown", "html", or "" for plain text
            
            Returns:
                JSON with success status and message_id
            """
            raise Exception("Client-side execution required")
        
        def telegram_send_file(file_path_or_url: str, caption: str = "", as_document: bool = False) -> str:
            """Send a file or image to the current Telegram chat.
            
            Supports local files and URLs. Auto-detects type by extension:
            - Images (jpg, png, gif, webp): sent as photos
            - Videos (mp4, mov): sent as videos
            - Audio (mp3, ogg): sent as audio
            - Other: sent as documents
            
            Args:
                file_path_or_url: Local file path or URL to send
                caption: Optional caption for the file
                as_document: If True, send as document even if it's an image
            
            Returns:
                JSON with success status and message details
            """
            raise Exception("Client-side execution required")
        
        # Task management tools
        def spawn_task(description: str, mode: str = "worker", priority: str = "normal") -> str:
            """Spawn a background task to work on something while you continue chatting.
            
            Use this when asked to do something that takes time (research, analysis, etc.).
            The task runs in the background while you remain responsive to the user.
            
            Execution modes:
            - "worker": Simple local execution with tools (fast, lightweight)
            - "subagent": Spawn a full Letta subagent (has memory, more capable)
            - "background": Run on your own context in background mode
            
            Args:
                description: Detailed description of what the task should accomplish
                mode: Execution mode - "worker", "subagent", or "background"
                priority: Task priority - "low", "normal", "high", or "urgent"
            
            Returns:
                JSON with task_id to track progress
            """
            raise Exception("Client-side execution required")
        
        def get_tasks(status: str = "", limit: int = 10) -> str:
            """Get a list of background tasks.
            
            Use this to check what tasks are pending, running, or completed.
            
            Args:
                status: Filter by status - "pending", "running", "completed", "failed", "cancelled", or "" for all
                limit: Maximum number of tasks to return (default 10)
            
            Returns:
                JSON with list of tasks and statistics
            """
            raise Exception("Client-side execution required")
        
        def get_task_status(task_id: str) -> str:
            """Get detailed status of a specific task.
            
            Args:
                task_id: The task ID to check
            
            Returns:
                JSON with detailed task info including progress and events
            """
            raise Exception("Client-side execution required")
        
        def cancel_task(task_id: str) -> str:
            """Cancel a pending or running task.
            
            Pending tasks are cancelled immediately.
            Running tasks will stop at the next checkpoint.
            
            Args:
                task_id: The task ID to cancel
            
            Returns:
                JSON with cancellation result
            """
            raise Exception("Client-side execution required")
        
        stub_functions = [
            bash,
            bash_output,
            kill_bash,
            get_terminal_screen,
            send_terminal_input,
            get_environment_info,
            check_command_exists,
            read_file,
            write_file,
            edit_file,
            list_directory,
            glob_search,
            grep_search,
            # Browser tools
            browser_navigate,
            browser_get_context,
            browser_get_text,
            browser_click,
            browser_fill,
            browser_wait_for,
            browser_screenshot,
            browser_extract_text,
            browser_scroll,
            browser_close,
            # Telegram tools
            telegram_send_message,
            telegram_send_file,
            # Task management tools
            spawn_task,
            get_tasks,
            get_task_status,
            cancel_task,
        ]

        for func in stub_functions:
            try:
                tool = await self.client.tools.upsert_from_function(
                    func=func,
                    default_requires_approval=True,  # Key: enables client-side execution
                )
                tool_names.append(tool.name)
                logger.info(f"Registered client-side tool: {tool.name}")
            except Exception as e:
                logger.warning(f"Could not register tool {func.__name__}: {e}")

        return tool_names

    async def _sync_agent_tools(self, agent_id: str) -> None:
        """Sync tools with an existing agent - attach missing, optionally detach removed.
        
        Called on startup to ensure agent has all current tools.
        """
        # First, register all tools with Letta (upsert ensures they exist)
        expected_tool_names = await self._register_tools()
        
        # Add built-in Letta tools
        builtin_tools = [
            "web_search",
            "fetch_webpage", 
            "archival_memory_insert",
            "archival_memory_search",
        ]
        expected_tool_names.extend(builtin_tools)
        
        # Get agent's current tools
        agent = await self.client.agents.retrieve(agent_id)
        current_tool_names = [t.name for t in agent.tools]
        
        # Find missing tools
        missing_tools = set(expected_tool_names) - set(current_tool_names)
        
        if not missing_tools:
            logger.info("All tools already attached to agent")
            return
        
        logger.info(f"Syncing {len(missing_tools)} missing tools: {missing_tools}")
        
        # Get tool IDs for missing tools
        # List all tools and filter by name
        all_tools = []
        tools_response = await self.client.tools.list()
        if hasattr(tools_response, '__aiter__'):
            async for tool in tools_response:
                all_tools.append(tool)
        else:
            all_tools = list(tools_response)
        
        tool_id_map = {t.name: t.id for t in all_tools}
        
        # Attach missing tools
        for tool_name in missing_tools:
            tool_id = tool_id_map.get(tool_name)
            if tool_id:
                try:
                    await self.client.agents.tools.attach(
                        agent_id=agent_id,
                        tool_id=tool_id,
                    )
                    logger.info(f"  Attached: {tool_name}")
                except Exception as e:
                    logger.warning(f"  Failed to attach {tool_name}: {e}")
            else:
                logger.warning(f"  Tool not found in registry: {tool_name}")

    async def _recover_from_pending_approval(self, agent_id: str, original_messages: list, error_str: str = ""):
        """Recover from a stuck pending approval state by denying it and retrying."""
        import re
        
        # Try to extract pending_request_id from error message
        pending_request_id = None
        if error_str:
            match = re.search(r"'pending_request_id':\s*'(message-[a-f0-9-]+)'", error_str)
            if match:
                pending_request_id = match.group(1)
        
        if pending_request_id:
            logger.info(f"Found pending request: {pending_request_id}, trying to clear via clear_pending_approvals")
        
        # Use clear_pending_approvals which handles the proper format
        await self.clear_pending_approvals(agent_id)
        
        # Retry the original message
        return await self.client.agents.messages.create(
            agent_id=agent_id,
            messages=original_messages,
        )

    async def _execute_tool_locally(self, tool_name: str, arguments: dict) -> tuple[str, str]:
        """Execute a tool locally and return (result, status).
        
        Async tools are called directly. Sync tools run in thread pool executor.
        """
        import asyncio
        import functools
        import inspect
        
        handler = self._tool_handlers.get(tool_name)
        if not handler:
            return f"Unknown tool: {tool_name}", "error"

        try:
            # Check if handler is async
            if asyncio.iscoroutinefunction(handler):
                # Call async handler directly with timeout
                result = await asyncio.wait_for(
                    handler(**arguments),
                    timeout=60.0
                )
            else:
                # Run sync handler in thread pool
                loop = asyncio.get_event_loop()
                result = await asyncio.wait_for(
                    loop.run_in_executor(
                        None,
                        functools.partial(handler, **arguments)
                    ),
                    timeout=60.0
                )
            return str(result), "success"
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            return f"Tool execution timed out after 60 seconds", "error"
        except Exception as e:
            return f"Tool execution error: {e}", "error"

    async def send_message(
        self, 
        message: str, 
        context: Optional[dict] = None,
        on_message: Optional[callable] = None,
    ) -> str:
        """Send a message to the agent and get the response.
        
        Handles client-side tool execution in a loop until the agent finishes.
        
        Args:
            message: The message to send
            context: Optional context dict with user info
            on_message: Optional async callback called with each assistant message as it arrives
        
        Returns:
            All assistant messages concatenated
        """
        # Serialize access to agent - prevents race conditions between
        # regular messages and heartbeat
        async with self._agent_lock:
            return await self._send_message_impl(message, context, on_message)
    
    async def _send_message_impl(
        self,
        message: str,
        context: Optional[dict] = None,
        on_message: Optional[callable] = None,
    ) -> str:
        """Internal implementation of send_message (called under lock)."""
        agent_id = await self.get_or_create_agent()

        # Hippocampus: Analyze for topic change and augment with memories
        # Skip for system messages (heartbeats, etc.)
        is_system_message = message.startswith("[HEARTBEAT]") or message.startswith("[SYSTEM]")
        if self.settings.hippocampus_enabled and not is_system_message:
            try:
                recent_messages = await self.get_recent_messages(limit=15)
                message = await self.hippocampus.augment_message(
                    main_agent_id=agent_id,
                    new_message=message,
                    recent_messages=recent_messages,
                )
            except Exception as e:
                logger.warning(f"Hippocampus augmentation failed (continuing without): {e}")

        # Format message with context if provided
        if context:
            context_prefix = f"[Context: user={context.get('username', 'unknown')}]\n\n"
            formatted = context_prefix + message
        else:
            formatted = message

        # Check if we have attachments (multimodal)
        attachments = context.get("attachments", []) if context else []
        
        if attachments:
            # Build multimodal content array
            content = [{"type": "text", "text": formatted}]
            
            for attachment in attachments:
                if attachment.get("type") == "image":
                    # Use base64 encoding for images
                    if "base64_data" in attachment:
                        content.append({
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": attachment.get("media_type", "image/jpeg"),
                                "data": attachment["base64_data"],
                            }
                        })
                    elif "url" in attachment:
                        # Fallback to URL
                        content.append({
                            "type": "image",
                            "source": {
                                "type": "url",
                                "url": attachment["url"],
                            }
                        })
                # Non-image attachments: add info to text context
                elif attachment.get("type") in ("document", "audio", "video", "voice", "video_note"):
                    local_path = attachment.get("local_path", "")
                    file_name = attachment.get("file_name", local_path.split("/")[-1] if local_path else "file")
                    content[0]["text"] += f"\n\n[Attachment: {attachment['type']} - {file_name}]\nSaved to: {local_path}"
            
            messages = [{"role": "user", "content": content}]
        else:
            # Simple text message
            messages = [{"role": "user", "content": formatted}]
        
        result_parts = []
        max_iterations = 50  # Safety limit for tool call loops
        max_continuations = 10  # Safety limit for end_turn continuations
        continuation_count = 0
        
        for iteration in range(max_iterations):
            # Check for cancellation (via callback if provided in context)
            cancel_check = context.get("_cancel_check") if context else None
            if cancel_check and cancel_check():
                logger.info("Task cancelled by user")
                return "[Task cancelled by user]"
            
            # Send to agent
            try:
                response = await self.client.agents.messages.create(
                    agent_id=agent_id,
                    messages=messages,
                )
            except Exception as e:
                error_str = str(e)
                # Handle pending approval from previous incomplete request
                if "PENDING_APPROVAL" in error_str:
                    logger.warning("Agent has pending approval from previous request, recovering...")
                    response = await self._recover_from_pending_approval(agent_id, messages, error_str)
                elif "No tool call is currently awaiting approval" in error_str:
                    # Agent state changed - our tool results are stale
                    # This can happen if we're sending approval responses but agent moved on
                    logger.warning("Agent not expecting tool results (state changed), prompting continuation...")
                    # Send a continuation prompt instead of breaking
                    messages = [{"role": "user", "content": "[SYSTEM] Continue with the task."}]
                    continue
                else:
                    raise
            
            stop_reason_obj = getattr(response, "stop_reason", None)
            # stop_reason can be a StopReason object with a stop_reason attribute, or a string
            if hasattr(stop_reason_obj, "stop_reason"):
                stop_reason = stop_reason_obj.stop_reason
            else:
                stop_reason = str(stop_reason_obj) if stop_reason_obj else None
            logger.info(f"Iteration {iteration}: stop_reason={stop_reason}, messages={len(response.messages)}")

            # Collect assistant messages and approval requests
            approvals_needed = []
            current_iteration_has_response = False
            current_iteration_response = ""
            for msg in response.messages:
                msg_type = getattr(msg, "message_type", None)
                logger.info(f"  Message type: {msg_type}, attrs: {[a for a in dir(msg) if not a.startswith('_')]}")
                
                if msg_type == "assistant_message":
                    content = getattr(msg, "content", None)
                    if content:
                        logger.info(f"  Assistant content ({len(content)} chars)")
                        current_iteration_response = content
                        current_iteration_has_response = True
                        
                        # Filter out "nothing to do" responses from continuation prompts
                        content_lower = content.lower()
                        is_empty_continuation = (
                            continuation_count > 0 and 
                            len(content) < 200 and
                            any(phrase in content_lower for phrase in [
                                "no further action", "no action available", "nothing to do",
                                "nothing further", "no additional", "task is complete",
                                "already complete", "nothing more", "no more action",
                            ])
                        )
                        
                        if is_empty_continuation:
                            logger.info("  Filtered empty continuation response")
                            continue  # Don't send or accumulate this response
                        
                        result_parts.append(content)
                        # Send message immediately via callback if provided
                        if on_message:
                            try:
                                await on_message(content)
                            except Exception as e:
                                logger.warning(f"on_message callback failed: {e}")
                
                # Also try to capture text from other message types
                elif msg_type == "tool_return_message":
                    # This is the result we sent back, skip it
                    pass
                elif msg_type not in ("approval_request_message", "reasoning_message", "tool_call_message"):
                    # Unknown message type - log its content
                    content = getattr(msg, "content", None) or getattr(msg, "text", None)
                    if content:
                        logger.info(f"  Other message content ({msg_type}): {content[:200]}...")
                
                elif msg_type == "approval_request_message":
                    # Tool needs client-side execution
                    tool_call = msg.tool_call
                    tool_name = tool_call.name
                    tool_args = json.loads(tool_call.arguments) if tool_call.arguments else {}
                    tool_call_id = tool_call.tool_call_id
                    
                    logger.info(f"Executing tool locally: {tool_name}({tool_args})")
                    
                    # Execute locally
                    result, status = await self._execute_tool_locally(tool_name, tool_args)
                    result_preview = (result[:200] + "...") if result and len(result) > 200 else (result or "(empty)")
                    logger.info(f"  Tool result ({status}): {result_preview}")
                    
                    approvals_needed.append({
                        "type": "tool",
                        "tool_call_id": tool_call_id,
                        "tool_return": result,
                        "status": status,
                    })

            # Check if we're done
            if stop_reason == "requires_approval" and approvals_needed:
                # Send tool results back - MUST complete to avoid leaving agent in pending state
                logger.info(f"Sending {len(approvals_needed)} tool results back to agent")
                messages = [{
                    "type": "approval",
                    "approvals": approvals_needed,
                }]
                continue
            
            # Agent ended turn - use hippocampus to judge if we should continue
            if stop_reason == "end_turn" and continuation_count < max_continuations:
                # Get original request from context or first message
                original_request = context.get("_original_request", message) if context else message
                
                # Ask hippocampus if we should continue
                needs_continuation = await self.hippocampus.should_continue(
                    original_request=original_request,
                    agent_response=current_iteration_response,
                    iteration=iteration,
                )
                
                if needs_continuation:
                    continuation_count += 1
                    logger.info(f"Hippocampus says continue ({continuation_count}/{max_continuations})")
                    messages = [{"role": "user", "content": "[SYSTEM] Continue."}]
                    continue
            
            logger.info(f"Exiting loop: stop_reason={stop_reason}, continuations={continuation_count}")
            break

        final_result = "\n\n".join(result_parts) if result_parts else ""
        logger.info(f"Final result: {len(result_parts)} parts, {len(final_result)} chars")
        return final_result

    async def get_memory_block(self, label: str) -> Optional[str]:
        """Get a memory block's content."""
        agent_id = await self.get_or_create_agent()
        agent = await self.client.agents.retrieve(agent_id)
        
        for block in agent.memory.blocks:
            if block.label == label:
                return block.value
        return None

    async def update_memory_block(self, label: str, value: str):
        """Update a memory block's content."""
        agent_id = await self.get_or_create_agent()
        agent = await self.client.agents.retrieve(agent_id)
        
        for block in agent.memory.blocks:
            if block.label == label:
                await self.client.agents.blocks.update(
                    agent_id=agent_id,
                    block_id=block.id,
                    value=value,
                )
                logger.info(f"Updated memory block: {label}")
                return
        
        logger.warning(f"Memory block not found: {label}")

    # Archival Memory Methods (for SDK access)
    
    async def archival_insert(self, content: str, tags: Optional[list[str]] = None) -> str:
        """Insert content into archival memory.
        
        Args:
            content: The text content to store
            tags: Optional list of tags for categorization
            
        Returns:
            The ID of the created passage
        """
        agent_id = await self.get_or_create_agent()
        passage = await self.client.agents.passages.insert(
            agent_id=agent_id,
            content=content,
            tags=tags or [],
        )
        logger.info(f"Inserted archival memory: {passage.id}")
        return passage.id

    async def archival_search(
        self, 
        query: str, 
        tags: Optional[list[str]] = None,
        limit: int = 10,
    ) -> list[dict]:
        """Search archival memory semantically.
        
        Args:
            query: The search query (semantic search)
            tags: Optional tags to filter by
            limit: Maximum number of results
            
        Returns:
            List of matching passages with id, content, tags
        """
        agent_id = await self.get_or_create_agent()
        results = await self.client.agents.passages.search(
            agent_id=agent_id,
            query=query,
            tags=tags,
            limit=limit,
        )
        
        # Handle async iterator
        passages = []
        if hasattr(results, '__aiter__'):
            async for passage in results:
                passages.append({
                    "id": passage.id,
                    "content": passage.content,
                    "tags": getattr(passage, "tags", []),
                })
        elif hasattr(results, 'items'):
            for passage in results.items:
                passages.append({
                    "id": passage.id,
                    "content": passage.content,
                    "tags": getattr(passage, "tags", []),
                })
        else:
            for passage in results:
                passages.append({
                    "id": passage.id,
                    "content": passage.content,
                    "tags": getattr(passage, "tags", []),
                })
        
        return passages

    async def archival_list(self, limit: int = 50) -> list[dict]:
        """List all archival memories.
        
        Args:
            limit: Maximum number of results
            
        Returns:
            List of passages with id, content, tags
        """
        agent_id = await self.get_or_create_agent()
        results = await self.client.agents.passages.list(
            agent_id=agent_id,
            limit=limit,
        )
        
        # Handle async iterator
        passages = []
        if hasattr(results, '__aiter__'):
            async for passage in results:
                passages.append({
                    "id": passage.id,
                    "content": passage.content,
                    "tags": getattr(passage, "tags", []),
                })
        elif hasattr(results, 'items'):
            for passage in results.items:
                passages.append({
                    "id": passage.id,
                    "content": passage.content,
                    "tags": getattr(passage, "tags", []),
                })
        else:
            for passage in results:
                passages.append({
                    "id": passage.id,
                    "content": passage.content,
                    "tags": getattr(passage, "tags", []),
                })
        
        return passages

    async def archival_delete(self, passage_id: str) -> bool:
        """Delete an archival memory by ID.
        
        Args:
            passage_id: The ID of the passage to delete
            
        Returns:
            True if deleted successfully
        """
        agent_id = await self.get_or_create_agent()
        try:
            await self.client.agents.passages.delete(
                agent_id=agent_id,
                passage_id=passage_id,
            )
            logger.info(f"Deleted archival memory: {passage_id}")
            return True
        except Exception as e:
            logger.warning(f"Failed to delete archival memory {passage_id}: {e}")
            return False
