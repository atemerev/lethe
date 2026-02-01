"""Tools for the Lethe agent.

Tools are just Python functions. Schemas are auto-generated from type hints and docstrings.
"""

import inspect
import re
from typing import Callable, Any, get_type_hints, Optional

# Import all tool functions
from lethe.tools.cli import (
    bash,
    bash_output,
    get_terminal_screen,
    send_terminal_input,
    kill_bash,
    get_environment_info,
    check_command_exists,
)

from lethe.tools.filesystem import (
    read_file,
    write_file,
    edit_file,
    list_directory,
    glob_search,
    grep_search,
)

from lethe.tools.browser_agent import (
    browser_open_async as browser_open,
    browser_snapshot_async as browser_snapshot,
    browser_click_async as browser_click,
    browser_fill_async as browser_fill,
)


def _python_type_to_json(py_type) -> str:
    """Convert Python type to JSON schema type."""
    if py_type is None or py_type == type(None):
        return "string"
    
    type_name = getattr(py_type, "__name__", str(py_type))
    
    mapping = {
        "str": "string",
        "int": "integer",
        "float": "number",
        "bool": "boolean",
        "list": "array",
        "dict": "object",
    }
    return mapping.get(type_name, "string")


def _parse_docstring(docstring: str) -> tuple[str, dict[str, str]]:
    """Parse Google-style docstring into description and param descriptions."""
    if not docstring:
        return "", {}
    
    lines = docstring.strip().split("\n")
    description_lines = []
    param_descriptions = {}
    
    in_args = False
    current_param = None
    
    for line in lines:
        stripped = line.strip()
        
        if stripped.lower().startswith("args:"):
            in_args = True
            continue
        elif stripped.lower().startswith("returns:"):
            in_args = False
            continue
        
        if in_args:
            # Check for param line: "param_name: description" or "param_name (type): description"
            match = re.match(r"(\w+)(?:\s*\([^)]*\))?\s*:\s*(.+)", stripped)
            if match:
                current_param = match.group(1)
                param_descriptions[current_param] = match.group(2)
            elif current_param and stripped:
                # Continuation of previous param description
                param_descriptions[current_param] += " " + stripped
        elif not in_args and stripped:
            description_lines.append(stripped)
    
    return " ".join(description_lines), param_descriptions


def function_to_schema(func: Callable) -> dict:
    """Generate OpenAI function schema from a Python function."""
    sig = inspect.signature(func)
    
    try:
        hints = get_type_hints(func)
    except Exception:
        hints = {}
    
    description, param_docs = _parse_docstring(func.__doc__ or "")
    
    properties = {}
    required = []
    
    for name, param in sig.parameters.items():
        if name in ("self", "cls"):
            continue
        
        param_type = hints.get(name, str)
        json_type = _python_type_to_json(param_type)
        
        prop = {"type": json_type}
        if name in param_docs:
            prop["description"] = param_docs[name]
        
        properties[name] = prop
        
        # Required if no default value
        if param.default is inspect.Parameter.empty:
            required.append(name)
    
    return {
        "name": func.__name__,
        "description": description or f"Execute {func.__name__}",
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
        }
    }


def get_all_tools() -> list[tuple[Callable, dict]]:
    """Get all available tools as (function, schema) tuples."""
    # Tools with optional name override (for async imports)
    tools = [
        # CLI tools
        (bash, None),
        (bash_output, None),
        (kill_bash, None),
        
        # File tools
        (read_file, None),
        (write_file, None),
        (edit_file, None),
        (list_directory, None),
        (glob_search, None),
        (grep_search, None),
        
        # Browser tools (async, need name override)
        (browser_open, "browser_open"),
        (browser_snapshot, "browser_snapshot"),
        (browser_click, "browser_click"),
        (browser_fill, "browser_fill"),
    ]
    
    result = []
    for func, name_override in tools:
        schema = function_to_schema(func)
        if name_override:
            schema["name"] = name_override
        result.append((func, schema))
    return result


def get_tool_by_name(name: str) -> Optional[Callable]:
    """Get a tool function by name."""
    tools = {
        "bash": bash,
        "bash_output": bash_output,
        "kill_bash": kill_bash,
        "read_file": read_file,
        "write_file": write_file,
        "edit_file": edit_file,
        "list_directory": list_directory,
        "glob_search": glob_search,
        "grep_search": grep_search,
        # Browser tools (async versions)
        "browser_open": browser_open,
        "browser_snapshot": browser_snapshot,
        "browser_click": browser_click,
        "browser_fill": browser_fill,
    }
    return tools.get(name)


__all__ = [
    "function_to_schema",
    "get_all_tools",
    "get_tool_by_name",
    # CLI
    "bash",
    "bash_output",
    "get_terminal_screen",
    "send_terminal_input",
    "kill_bash",
    "get_environment_info",
    "check_command_exists",
    # Files
    "read_file",
    "write_file",
    "edit_file",
    "list_directory",
    "glob_search",
    "grep_search",
    # Browser
    "browser_open",
    "browser_snapshot",
    "browser_click",
    "browser_fill",
]
