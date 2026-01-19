"""Custom tools for the Lethe agent."""

import inspect
import os
import re
from typing import Any

# Import all tool modules
from lethe.tools import filesystem, cli, telegram_tools

# Conditionally import browser tools if dependencies are available
_browser_available = False
try:
    import playwright
    import steel
    from lethe.tools import browser
    _browser_available = True
except ImportError:
    browser = None  # type: ignore


def _strip_decorator(source: str) -> str:
    """Remove the @_is_tool decorator line from source code."""
    lines = source.split('\n')
    filtered = [line for line in lines if not line.strip().startswith('@_is_tool')]
    return '\n'.join(filtered)


def get_all_tools(include_browser: bool = True) -> list[dict[str, Any]]:
    """Get all tool definitions for registration with Letta.
    
    Args:
        include_browser: Include browser tools if dependencies are available
    """
    tools = []
    
    # Core tool modules
    modules = [filesystem, cli, telegram_tools]
    
    # Add browser module if available and requested
    if include_browser and _browser_available and browser is not None:
        modules.append(browser)

    # Collect tools from each module
    for module in modules:
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


def is_browser_available() -> bool:
    """Check if browser tools are available."""
    return _browser_available


def tool(func):
    """Decorator to mark a function as a Letta tool."""
    func._is_tool = True
    return func
