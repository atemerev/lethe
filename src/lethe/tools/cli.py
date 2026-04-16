"""CLI/shell tools for the agent - Bash-like implementation.

Supports two modes:
1. Regular: subprocess with stdout/stderr capture (for most commands)
2. PTY: pseudo-terminal with screen emulation (for TUI apps like htop, vim, etc.)
"""

import os
import subprocess
import threading
from datetime import datetime
from pathlib import Path

from lethe.tools.process_manager import (
    BackgroundProcess,
    background_processes,
    create_pty_process,
    get_next_bash_id,
    get_process,
    list_processes,
    register_process,
    remove_process,
)

# Limits
DEFAULT_TIMEOUT = 120  # 2 minutes
MAX_TIMEOUT = 600  # 10 minutes

from lethe.tools.truncate import (
    truncate_tail,
    format_truncation_notice,
    format_size,
    DEFAULT_MAX_LINES,
    DEFAULT_MAX_BYTES,
)


def _truncate_output(output: str) -> str:
    """Truncate bash output from tail (keep end where errors/results are)."""
    result = truncate_tail(output)
    if not result.truncated:
        return result.content
    
    # Build output with truncation notice
    notice = format_truncation_notice(result, start_line=result.total_lines - result.output_lines + 1)
    return f"{result.content}\n\n{notice}"


def _is_tool(func):
    """Decorator to mark a function as a tool."""
    func._is_tool = True
    return func


@_is_tool
def bash(
    command: str,
    timeout: int = DEFAULT_TIMEOUT,
    description: str = "",
    run_in_background: bool = False,
    use_pty: bool = False,
) -> str:
    """Execute a bash command in the current working directory.
    
    Output is truncated to last 2000 lines or 50KB (whichever is hit first).
    Truncation keeps the END of output where errors and results typically are.
    
    Args:
        command: The shell command to execute
        timeout: Timeout in seconds (default: 120, max: 600)
        description: Short description of what the command does
        run_in_background: If True, run in background and return immediately
        use_pty: If True, run in a pseudo-terminal (needed for TUI apps like htop, vim)
    
    Returns:
        Command output (stdout + stderr), error message, or background process ID
    """
    # Special command to list background processes
    if command == "/bg":
        procs = list_processes()
        if not procs:
            return "(no background processes)"
        
        lines = []
        for shell_id, proc in procs.items():
            runtime = ""
            if proc.start_time:
                elapsed = (datetime.now() - proc.start_time).total_seconds()
                runtime = f", runtime: {int(elapsed)}s"
            mode = "PTY" if proc.is_pty else "subprocess"
            lines.append(f"{shell_id}: {proc.command} ({proc.status}, {mode}{runtime})")
        return "\n".join(lines)
    
    cwd = os.environ.get("USER_CWD", os.getcwd())
    env = {**os.environ}
    
    # Auto-prepend sudo for package manager commands (needed in containers)
    # Only if not already using sudo and we're not root
    if os.getuid() != 0:
        apt_commands = ("apt-get ", "apt ", "dpkg ", "apt-cache ")
        cmd_stripped = command.strip()
        if cmd_stripped.startswith(apt_commands) and not cmd_stripped.startswith("sudo"):
            command = f"sudo {command}"
    
    # Clamp timeout
    effective_timeout = max(1, min(timeout, MAX_TIMEOUT))
    
    if run_in_background:
        if use_pty:
            return _run_background_pty(command, cwd, env)
        else:
            env["TERM"] = "dumb"
            return _run_background(command, cwd, env, effective_timeout)
    else:
        env["TERM"] = "dumb"
        return _run_foreground(command, cwd, env, effective_timeout)


def _run_foreground(command: str, cwd: str, env: dict, timeout: int) -> str:
    """Run a command in the foreground and wait for completion."""
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        
        output_parts = []
        if result.stdout:
            output_parts.append(result.stdout)
        if result.stderr:
            if output_parts:
                output_parts.append("\n--- stderr ---\n")
            output_parts.append(result.stderr)
        
        output = "".join(output_parts).strip()
        output = _truncate_output(output)
        
        if result.returncode != 0:
            return f"Exit code: {result.returncode}\n{output}"
        
        return output if output else "(command completed with no output)"
        
    except subprocess.TimeoutExpired:
        return f"Error: Command timed out after {timeout} seconds"
    except Exception as e:
        return f"Error executing command: {e}"


def _run_background(command: str, cwd: str, env: dict, timeout: int) -> str:
    """Run a command in the background (regular subprocess mode)."""
    bash_id = get_next_bash_id()
    
    try:
        process = subprocess.Popen(
            command,
            shell=True,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        
        bg_proc = BackgroundProcess(
            process=process,
            command=command,
            start_time=datetime.now(),
        )
        register_process(bash_id, bg_proc)
        
        # Start threads to read output
        def read_stdout():
            if process.stdout:
                for line in process.stdout:
                    bg_proc.stdout.append(line.rstrip('\n'))
        
        def read_stderr():
            if process.stderr:
                for line in process.stderr:
                    bg_proc.stderr.append(line.rstrip('\n'))
        
        def monitor_process():
            """Monitor process and update status on completion."""
            exit_code = process.wait()
            bg_proc.exit_code = exit_code
            bg_proc.status = "completed" if exit_code == 0 else "failed"
        
        # Start reader threads (daemon so they don't block shutdown)
        stdout_thread = threading.Thread(target=read_stdout, daemon=True)
        stderr_thread = threading.Thread(target=read_stderr, daemon=True)
        monitor_thread = threading.Thread(target=monitor_process, daemon=True)
        
        stdout_thread.start()
        stderr_thread.start()
        monitor_thread.start()
        
        # Set up timeout if specified
        if timeout > 0:
            def timeout_handler():
                if bg_proc.status == "running":
                    process.kill()
                    bg_proc.status = "failed"
                    bg_proc.stderr.append(f"Command timed out after {timeout}s")
            
            timer = threading.Timer(timeout, timeout_handler)
            timer.daemon = True
            timer.start()
        
        return f"Command running in background with ID: {bash_id}"
        
    except Exception as e:
        return f"Error starting background command: {e}"


def _run_background_pty(command: str, cwd: str, env: dict) -> str:
    """Run a command in a PTY (for TUI apps)."""
    try:
        bash_id, bg_proc = create_pty_process(command, cwd, env)
        return f"Command running in PTY with ID: {bash_id} (use get_terminal_screen to view)"
    except Exception as e:
        return f"Error starting PTY command: {e}"


@_is_tool
def bash_output(shell_id: str, filter_pattern: str = "", last_lines: int = 0) -> str:
    """Get output from a background bash process.
    
    Args:
        shell_id: The ID of the background shell (e.g., bash_1)
        filter_pattern: Optional string to filter output lines
        last_lines: If > 0, only return the last N lines (useful for logs)
    
    Returns:
        The accumulated output from the background process
    """
    proc = get_process(shell_id)
    if not proc:
        return f"No background process found with ID: {shell_id}"
    
    # For PTY processes, suggest using get_terminal_screen instead
    if proc.is_pty:
        return (
            f"Process {shell_id} is running in PTY mode.\n"
            f"Use get_terminal_screen('{shell_id}') to view the terminal screen.\n"
            f"Status: {proc.status}"
        )
    
    # Combine stdout and stderr
    stdout = "\n".join(proc.stdout)
    stderr = "\n".join(proc.stderr)
    
    output = stdout
    if stderr:
        output = f"{output}\n{stderr}" if output else stderr
    
    # Apply filter if specified
    if filter_pattern:
        lines = output.split("\n")
        lines = [line for line in lines if filter_pattern in line]
        output = "\n".join(lines)
    
    # Apply last_lines limit
    if last_lines > 0:
        lines = output.split("\n")
        if len(lines) > last_lines:
            lines = lines[-last_lines:]
            output = f"... [{len(proc.stdout) + len(proc.stderr) - last_lines} earlier lines]\n" + "\n".join(lines)
        else:
            output = "\n".join(lines)
    
    output = _truncate_output(output)
    
    if not output:
        status_info = f" (status: {proc.status})"
        if proc.exit_code is not None:
            status_info += f", exit code: {proc.exit_code}"
        return f"(no output yet){status_info}"
    
    return output


@_is_tool
def get_terminal_screen(shell_id: str) -> str:
    """Get the current terminal screen for a PTY process.
    
    Use this for TUI applications (htop, vim, etc.) to see what's currently displayed.
    
    Args:
        shell_id: The ID of the background PTY process
    
    Returns:
        The current terminal screen content (what a user would see)
    """
    proc = get_process(shell_id)
    if not proc:
        return f"No background process found with ID: {shell_id}"
    
    if not proc.is_pty:
        return (
            f"Process {shell_id} is not running in PTY mode.\n"
            f"Use bash_output('{shell_id}') to view output.\n"
            f"To run in PTY mode, use: bash(command, run_in_background=True, use_pty=True)"
        )
    
    screen_text = proc.get_screen_text()
    cursor_row, cursor_col = proc.get_cursor_position()
    
    # Add status info
    status_line = f"\n--- Process: {proc.status}"
    if proc.exit_code is not None:
        status_line += f", exit code: {proc.exit_code}"
    status_line += f", cursor: ({cursor_row}, {cursor_col}) ---"
    
    return screen_text + status_line


@_is_tool
def send_terminal_input(shell_id: str, text: str, send_enter: bool = True) -> str:
    """Send input to a PTY process (for TUI interaction).
    
    Args:
        shell_id: The ID of the background PTY process
        text: Text to send to the terminal
        send_enter: If True, append Enter key after text (default True)
    
    Returns:
        Confirmation message
    """
    proc = get_process(shell_id)
    if not proc:
        return f"No background process found with ID: {shell_id}"
    
    if not proc.is_pty:
        return f"Process {shell_id} is not running in PTY mode. Cannot send input."
    
    if proc.status != "running":
        return f"Process {shell_id} is not running (status: {proc.status})"
    
    try:
        input_text = text
        if send_enter:
            input_text += "\n"
        proc.write_input(input_text)
        return f"Sent input to {shell_id}: {repr(text)}"
    except Exception as e:
        return f"Error sending input: {e}"


@_is_tool
def kill_bash(shell_id: str) -> str:
    """Kill a background bash process.
    
    Args:
        shell_id: The ID of the background shell to kill
    
    Returns:
        Success or failure message
    """
    proc = get_process(shell_id)
    if not proc:
        return f"No background process found with ID: {shell_id}"
    
    try:
        if proc.status == "running":
            if proc.is_pty and proc.pty_pid:
                # Kill PTY process
                import signal
                os.kill(proc.pty_pid, signal.SIGTERM)
                proc.status = "failed"
            elif proc.process:
                proc.process.kill()
                proc.status = "failed"
        
        remove_process(shell_id)
        return f"Killed background process: {shell_id}"
        
    except Exception as e:
        return f"Error killing process: {e}"


@_is_tool
def get_environment_info() -> str:
    """Get information about the current environment.
    
    Returns:
        Environment info including OS, user, pwd, shell
    """
    try:
        info = {
            "user": os.environ.get("USER", "unknown"),
            "home": os.environ.get("HOME", "unknown"),
            "pwd": os.getcwd(),
            "shell": os.environ.get("SHELL", "unknown"),
        }
        
        result = subprocess.run(
            "uname -a",
            shell=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        info["os"] = result.stdout.strip() if result.returncode == 0 else "unknown"
        
        lines = [f"{k}: {v}" for k, v in info.items()]
        return "Environment Information:\n" + "\n".join(lines)
        
    except Exception as e:
        return f"Error getting environment info: {e}"


@_is_tool
def check_command_exists(command_name: str) -> str:
    """Check if a command is available in PATH.
    
    Args:
        command_name: Name of the command to check
    
    Returns:
        Whether the command exists and its path
    """
    try:
        result = subprocess.run(
            f"which {command_name}",
            shell=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        
        if result.returncode == 0:
            return f"'{command_name}' is available at: {result.stdout.strip()}"
        else:
            return f"'{command_name}' is not found in PATH"
            
    except Exception as e:
        return f"Error checking command: {e}"
