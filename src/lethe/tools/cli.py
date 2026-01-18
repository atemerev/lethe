"""CLI/shell tools for the agent - Bash-like implementation."""

import os
import subprocess
import threading
from datetime import datetime
from pathlib import Path

from lethe.tools.process_manager import (
    BackgroundProcess,
    background_processes,
    get_next_bash_id,
    get_process,
    list_processes,
    register_process,
    remove_process,
)

# Limits
MAX_OUTPUT_CHARS = 30000
DEFAULT_TIMEOUT = 120  # 2 minutes
MAX_TIMEOUT = 600  # 10 minutes


def _truncate_output(output: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    """Truncate output if it exceeds the limit."""
    if len(output) <= limit:
        return output
    return output[:limit] + f"\n\n... [output truncated, {limit} char limit]"


def _is_tool(func):
    """Decorator to mark a function as a Letta tool."""
    func._is_tool = True
    return func


@_is_tool
def bash(
    command: str,
    timeout: int = DEFAULT_TIMEOUT,
    description: str = "",
    run_in_background: bool = False,
) -> str:
    """Execute a bash command in the shell.
    
    Args:
        command: The shell command to execute
        timeout: Timeout in seconds (default: 120, max: 600)
        description: Short description of what the command does
        run_in_background: If True, run in background and return immediately
    
    Returns:
        Command output, error message, or background process ID
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
            lines.append(f"{shell_id}: {proc.command} ({proc.status}{runtime})")
        return "\n".join(lines)
    
    cwd = os.environ.get("USER_CWD", os.getcwd())
    env = {**os.environ, "TERM": "dumb"}
    
    # Clamp timeout
    effective_timeout = max(1, min(timeout, MAX_TIMEOUT))
    
    if run_in_background:
        return _run_background(command, cwd, env, effective_timeout)
    else:
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
    """Run a command in the background."""
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


@_is_tool
def bash_output(shell_id: str, filter_pattern: str = "") -> str:
    """Get output from a background bash process.
    
    Args:
        shell_id: The ID of the background shell (e.g., bash_1)
        filter_pattern: Optional string to filter output lines
    
    Returns:
        The accumulated output from the background process
    """
    proc = get_process(shell_id)
    if not proc:
        return f"No background process found with ID: {shell_id}"
    
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
    
    output = _truncate_output(output)
    
    if not output:
        status_info = f" (status: {proc.status})"
        if proc.exit_code is not None:
            status_info += f", exit code: {proc.exit_code}"
        return f"(no output yet){status_info}"
    
    return output


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
            proc.process.kill()
            proc.status = "failed"
            proc.stderr.append("Process killed by user")
        
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
