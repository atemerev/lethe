"""Custom tools for the Lethe agent."""

import inspect
import re
from typing import Any

# Import all tool modules
from lethe.tools import filesystem, cli


def _strip_decorator(source: str) -> str:
    """Remove the @_is_tool decorator line from source code."""
    lines = source.split('\n')
    filtered = [line for line in lines if not line.strip().startswith('@_is_tool')]
    return '\n'.join(filtered)


def get_all_tools() -> list[dict[str, Any]]:
    """Get all tool definitions for registration with Letta."""
    tools = []

    # Collect tools from each module
    for module in [filesystem, cli]:
        for name, func in inspect.getmembers(module, inspect.isfunction):
            if hasattr(func, "_is_tool") and func._is_tool:
                source = inspect.getsource(func)
                # Strip the decorator so Letta can execute the function
                clean_source = _strip_decorator(source)
                tools.append({
                    "name": name,
                    "source_code": clean_source,
                })

    return tools


def tool(func):
    """Decorator to mark a function as a Letta tool."""
    func._is_tool = True
    return func
