"""
Browser automation tools using agent-browser CLI.

agent-browser provides deterministic element selection via refs from accessibility
tree snapshots. The AI decides WHAT to do, the tool handles HOW.

Workflow:
1. browser_open(url) - Navigate to page
2. browser_snapshot() - Get interactive elements with refs (@e1, @e2, etc.)
3. browser_click(@e1) / browser_fill(@e2, "text") - Interact using refs
4. Re-snapshot after page changes
"""

import asyncio
import json
import logging
import os
import shutil
from typing import Optional

from lethe.paths import cache_dir as _cache_dir

logger = logging.getLogger(__name__)

PROFILE_DIR = str(_cache_dir() / "browser")


def _get_agent_browser_path() -> str:
    """Get the path to agent-browser CLI."""
    path = shutil.which("agent-browser")
    if not path:
        raise RuntimeError("agent-browser not found. Install with: npm install -g agent-browser")
    return path


async def _run_command(args: list[str], timeout: float = 60.0) -> tuple[str, str, int]:
    """Run agent-browser command and return (stdout, stderr, returncode)."""
    cmd = [_get_agent_browser_path()] + args
    
    # Add profile for persistent sessions
    if "--profile" not in args and "install" not in args:
        cmd.insert(1, "--profile")
        cmd.insert(2, PROFILE_DIR)
    
    logger.debug(f"Running: {' '.join(cmd)}")
    
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return stdout.decode(), stderr.decode(), proc.returncode
    except asyncio.TimeoutError:
        proc.kill()
        return "", f"Command timed out after {timeout}s", -1


async def browser_open_async(url: str) -> str:
    """Navigate browser to a URL.
    
    Args:
        url: The URL to navigate to (must include protocol like https://)
    
    Returns:
        JSON with navigation result including page title
    """
    # Ensure profile directory exists
    os.makedirs(PROFILE_DIR, exist_ok=True)
    
    stdout, stderr, code = await _run_command(["open", url])
    
    if code != 0:
        return json.dumps({
            "status": "error",
            "message": stderr or stdout or "Failed to open URL",
        }, indent=2)
    
    return json.dumps({
        "status": "OK",
        "url": url,
        "message": stdout.strip() if stdout else f"Navigated to {url}",
    }, indent=2)


async def browser_snapshot_async(interactive_only: bool = True, compact: bool = True) -> str:
    """Get accessibility tree snapshot with element refs.
    
    This is the primary way to understand what's on the page. Returns a tree
    of elements with refs (@e1, @e2, etc.) that can be used with other commands.
    
    Args:
        interactive_only: Only show interactive elements like buttons, links, inputs (default: True)
        compact: Remove empty structural elements (default: True)
    
    Returns:
        Accessibility tree with refs. Use these refs with browser_click, browser_fill, etc.
        
    Example output:
        - heading "Welcome" [ref=e1] [level=1]
        - button "Sign In" [ref=e2]
        - textbox "Email" [ref=e3]
        - link "Learn more" [ref=e4]
    """
    args = ["snapshot"]
    if interactive_only:
        args.append("-i")
    if compact:
        args.append("-c")
    
    stdout, stderr, code = await _run_command(args)
    
    if code != 0:
        return json.dumps({
            "status": "error",
            "message": stderr or "Failed to get snapshot",
        }, indent=2)
    
    return json.dumps({
        "status": "OK",
        "snapshot": stdout.strip(),
    }, indent=2)


async def browser_click_async(ref_or_selector: str) -> str:
    """Click an element by ref or selector.
    
    Args:
        ref_or_selector: Element ref from snapshot (@e1, @e2) or CSS selector
    
    Returns:
        JSON with click result
    """
    stdout, stderr, code = await _run_command(["click", ref_or_selector])
    
    if code != 0:
        return json.dumps({
            "status": "error",
            "message": stderr or stdout or f"Failed to click {ref_or_selector}",
        }, indent=2)
    
    return json.dumps({
        "status": "OK",
        "message": f"Clicked {ref_or_selector}",
    }, indent=2)


async def browser_fill_async(ref_or_selector: str, text: str) -> str:
    """Fill a text input with value.
    
    Args:
        ref_or_selector: Element ref from snapshot (@e1, @e2) or CSS selector
        text: Text to fill into the input
    
    Returns:
        JSON with fill result
    """
    stdout, stderr, code = await _run_command(["fill", ref_or_selector, text])
    
    if code != 0:
        return json.dumps({
            "status": "error",
            "message": stderr or stdout or f"Failed to fill {ref_or_selector}",
        }, indent=2)
    
    return json.dumps({
        "status": "OK",
        "message": f"Filled {ref_or_selector} with text",
    }, indent=2)


async def browser_type_async(ref_or_selector: str, text: str) -> str:
    """Type text into an element (character by character, preserves existing content).
    
    Unlike fill which clears first, type appends to existing content.
    
    Args:
        ref_or_selector: Element ref from snapshot (@e1, @e2) or CSS selector
        text: Text to type
    
    Returns:
        JSON with type result
    """
    stdout, stderr, code = await _run_command(["type", ref_or_selector, text])
    
    if code != 0:
        return json.dumps({
            "status": "error",
            "message": stderr or stdout or f"Failed to type into {ref_or_selector}",
        }, indent=2)
    
    return json.dumps({
        "status": "OK",
        "message": f"Typed into {ref_or_selector}",
    }, indent=2)


async def browser_press_async(key: str) -> str:
    """Press a keyboard key.
    
    Args:
        key: Key to press (e.g., "Enter", "Tab", "Escape", "Control+a", "Shift+Tab")
    
    Returns:
        JSON with press result
    """
    stdout, stderr, code = await _run_command(["press", key])
    
    if code != 0:
        return json.dumps({
            "status": "error",
            "message": stderr or stdout or f"Failed to press {key}",
        }, indent=2)
    
    return json.dumps({
        "status": "OK",
        "message": f"Pressed {key}",
    }, indent=2)


async def browser_scroll_async(direction: str = "down", pixels: int = 500) -> str:
    """Scroll the page.
    
    Args:
        direction: Scroll direction - "up", "down", "left", "right"
        pixels: Number of pixels to scroll (default: 500)
    
    Returns:
        JSON with scroll result
    """
    stdout, stderr, code = await _run_command(["scroll", direction, str(pixels)])
    
    if code != 0:
        return json.dumps({
            "status": "error",
            "message": stderr or stdout or f"Failed to scroll {direction}",
        }, indent=2)
    
    return json.dumps({
        "status": "OK",
        "message": f"Scrolled {direction} {pixels}px",
    }, indent=2)


async def browser_screenshot_async(save_path: str = "", full_page: bool = False) -> str:
    """Take a screenshot of the current page.
    
    Args:
        save_path: Path to save screenshot (e.g., /tmp/screenshot.png)
        full_page: Capture full scrollable page (default: False, viewport only)
    
    Returns:
        JSON with screenshot info. Image is also injected into conversation for you to see.
    """
    import base64
    
    args = ["screenshot"]
    if full_page:
        args.append("--full")
    
    if save_path:
        args.append(save_path)
        stdout, stderr, code = await _run_command(args)
        
        if code != 0:
            return json.dumps({
                "status": "error",
                "message": stderr or stdout or "Failed to take screenshot",
            }, indent=2)
        
        # Read the file and encode as base64 for multimodal
        try:
            with open(save_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            
            return json.dumps({
                "status": "OK",
                "saved_to": save_path,
                "size": len(b64) * 3 // 4,  # Approximate decoded size
                "_image_attachment": {
                    "base64_data": b64,
                    "media_type": "image/png",
                },
            }, indent=2)
        except Exception as e:
            return json.dumps({
                "status": "OK",
                "saved_to": save_path,
                "note": f"Saved but could not read for display: {e}",
            }, indent=2)
    else:
        # No path - screenshot goes to stdout as base64
        stdout, stderr, code = await _run_command(args)
        
        if code != 0:
            return json.dumps({
                "status": "error",
                "message": stderr or "Failed to take screenshot",
            }, indent=2)
        
        b64 = stdout.strip()
        
        return json.dumps({
            "status": "OK",
            "size": len(b64) * 3 // 4,
            "_image_attachment": {
                "base64_data": b64,
                "media_type": "image/png",
            },
        }, indent=2)


async def browser_get_text_async(ref_or_selector: str = "") -> str:
    """Get text content from an element or the whole page.
    
    Args:
        ref_or_selector: Element ref (@e1) or CSS selector. Empty for page text.
    
    Returns:
        JSON with text content
    """
    args = ["get", "text"]
    if ref_or_selector:
        args.append(ref_or_selector)
    
    stdout, stderr, code = await _run_command(args)
    
    if code != 0:
        return json.dumps({
            "status": "error",
            "message": stderr or stdout or "Failed to get text",
        }, indent=2)
    
    return json.dumps({
        "status": "OK",
        "text": stdout.strip(),
    }, indent=2)


async def browser_get_url_async() -> str:
    """Get the current page URL.
    
    Returns:
        JSON with current URL
    """
    stdout, stderr, code = await _run_command(["get", "url"])
    
    if code != 0:
        return json.dumps({
            "status": "error",
            "message": stderr or "Failed to get URL",
        }, indent=2)
    
    return json.dumps({
        "status": "OK",
        "url": stdout.strip(),
    }, indent=2)


async def browser_wait_async(
    selector: str = "",
    text: str = "",
    timeout_ms: int = 30000,
) -> str:
    """Wait for an element, text, or time.
    
    Args:
        selector: CSS selector or ref to wait for (optional)
        text: Text to wait for on page (optional)
        timeout_ms: If no selector/text, wait this many milliseconds
    
    Returns:
        JSON with wait result
    """
    if text:
        args = ["wait", "--text", text]
    elif selector:
        args = ["wait", selector]
    else:
        args = ["wait", str(timeout_ms)]
    
    stdout, stderr, code = await _run_command(args, timeout=timeout_ms / 1000 + 5)
    
    if code != 0:
        return json.dumps({
            "status": "error",
            "message": stderr or stdout or "Wait failed or timed out",
        }, indent=2)
    
    return json.dumps({
        "status": "OK",
        "message": "Wait completed",
    }, indent=2)


async def browser_select_async(ref_or_selector: str, value: str) -> str:
    """Select an option from a dropdown.
    
    Args:
        ref_or_selector: Element ref or CSS selector for the select element
        value: Value or label to select
    
    Returns:
        JSON with select result
    """
    stdout, stderr, code = await _run_command(["select", ref_or_selector, value])
    
    if code != 0:
        return json.dumps({
            "status": "error",
            "message": stderr or stdout or f"Failed to select {value}",
        }, indent=2)
    
    return json.dumps({
        "status": "OK",
        "message": f"Selected {value} in {ref_or_selector}",
    }, indent=2)


async def browser_hover_async(ref_or_selector: str) -> str:
    """Hover over an element.
    
    Args:
        ref_or_selector: Element ref or CSS selector
    
    Returns:
        JSON with hover result
    """
    stdout, stderr, code = await _run_command(["hover", ref_or_selector])
    
    if code != 0:
        return json.dumps({
            "status": "error",
            "message": stderr or stdout or f"Failed to hover {ref_or_selector}",
        }, indent=2)
    
    return json.dumps({
        "status": "OK",
        "message": f"Hovering over {ref_or_selector}",
    }, indent=2)


async def browser_close_async() -> str:
    """Close the browser.
    
    Returns:
        JSON with close result
    """
    stdout, stderr, code = await _run_command(["close"])
    
    if code != 0:
        return json.dumps({
            "status": "error",
            "message": stderr or stdout or "Failed to close browser",
        }, indent=2)
    
    return json.dumps({
        "status": "OK",
        "message": "Browser closed",
    }, indent=2)


# Sync wrappers for tools that need them
def browser_open(url: str) -> str:
    """Sync wrapper for browser_open_async."""
    return asyncio.get_event_loop().run_until_complete(browser_open_async(url))

def browser_snapshot(interactive_only: bool = True, compact: bool = True) -> str:
    """Sync wrapper for browser_snapshot_async."""
    return asyncio.get_event_loop().run_until_complete(
        browser_snapshot_async(interactive_only, compact)
    )

def browser_click(ref_or_selector: str) -> str:
    """Sync wrapper for browser_click_async."""
    return asyncio.get_event_loop().run_until_complete(browser_click_async(ref_or_selector))

def browser_fill(ref_or_selector: str, text: str) -> str:
    """Sync wrapper for browser_fill_async."""
    return asyncio.get_event_loop().run_until_complete(browser_fill_async(ref_or_selector, text))
