"""
Stealth browser automation with anti-detection features.

This module wraps agent-browser with stealth configurations to avoid bot detection
on sites like Twitter/X, LinkedIn, etc.

Key features:
- Disables automation flags (--disable-blink-features=AutomationControlled)
- Sets realistic user agents and viewport
- Uses persistent profiles for session continuity
- Adds random delays between actions to mimic human behavior
- Masks navigator.webdriver and other automation signals

Usage:
    from lethe.tools.stealth_browser import StealthBrowser
    
    browser = StealthBrowser()
    browser.open("https://twitter.com")
    browser.click("@e1")
"""

import asyncio
import json
import logging
import os
import random
import shutil
import time
from typing import Optional
from pathlib import Path

from lethe.paths import cache_dir as _cache_dir

logger = logging.getLogger(__name__)

STEALTH_PROFILE_DIR = _cache_dir() / "browser" / "stealth_profile"

# Realistic user agents (rotating)
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
]

# Chrome args for stealth
STEALTH_ARGS = [
    # Disable automation flags
    "--disable-blink-features=AutomationControlled",
    # Disable automation info bar
    "--disable-infobars",
    # Disable features that reveal automation
    "--disable-features=IsolateOrigins,site-per-process",
    # Set window size to realistic desktop
    "--window-size=1920,1080",
    # Disable default browser check
    "--no-default-browser-check",
    # Disable first run
    "--no-first-run",
    # Disable background networking
    "--disable-background-networking",
    # Disable default apps
    "--disable-default-apps",
    # Disable hang monitor
    "--disable-hang-monitor",
    # Disable popup blocking
    "--disable-popup-blocking",
    # Disable prompt on repost
    "--disable-prompt-on-repost",
    # Disable sync
    "--disable-sync",
    # Disable web security (careful with this)
    # "--disable-web-security",
    # Enable extensions
    "--enable-extensions",
    # Hide scrollbars
    "--hide-scrollbars",
    # Ignore certificate errors
    "--ignore-certificate-errors",
    # Ignore certificate errors SPKI list
    "--ignore-certificate-errors-spki-list",
    # Metrics recording only
    "--metrics-recording-only",
    # Mute audio
    "--mute-audio",
    # No sandbox (needed for Docker/some environments)
    "--no-sandbox",
    # Disable setuid sandbox
    "--disable-setuid-sandbox",
    # Disable dev shm usage (helps with memory issues)
    "--disable-dev-shm-usage",
    # Disable software rasterizer
    "--disable-software-rasterizer",
    # Disable gpu (can help stability)
    "--disable-gpu",
    # Disable background timer throttling
    "--disable-background-timer-throttling",
    # Disable backgrounding occluded windows
    "--disable-backgrounding-occluded-windows",
    # Disable renderer backgrounding
    "--disable-renderer-backgrounding",
    # Disable field trial testing config
    "--disable-field-trial-config",
    # Disable component extensions with background pages
    "--disable-component-extensions-with-background-pages",
    # Disable breakpad
    "--disable-breakpad",
    # Disable component update
    "--disable-component-update",
    # Skip restore
    "--disable-session-crashed-bubble",
    "--disable-session-crashed-bubble",
]


def _get_agent_browser_path() -> str:
    """Get the path to agent-browser CLI."""
    path = shutil.which("agent-browser")
    if not path:
        raise RuntimeError("agent-browser not found. Install with: npm install -g agent-browser")
    return path


def _get_stealth_args() -> str:
    """Get stealth browser args as comma-separated string."""
    return ",".join(STEALTH_ARGS)


def _get_random_user_agent() -> str:
    """Get a random realistic user agent."""
    return random.choice(USER_AGENTS)


class StealthBrowser:
    """
    Browser automation with anti-detection features.
    
    Wraps agent-browser CLI with stealth configurations for sites with
    sophisticated bot detection (Twitter/X, LinkedIn, etc.)
    """
    
    def __init__(
        self,
        profile_dir: Optional[Path] = None,
        user_agent: Optional[str] = None,
        viewport: Optional[tuple[int, int]] = None,
        headed: bool = False,
    ):
        """
        Initialize stealth browser.
        
        Args:
            profile_dir: Directory for persistent browser profile
            user_agent: Custom user agent (default: random realistic)
            viewport: Browser viewport size (default: 1920x1080)
            headed: Show browser window (default: False for headless)
        """
        self.profile_dir = Path(profile_dir) if profile_dir else STEALTH_PROFILE_DIR
        self.user_agent = user_agent or _get_random_user_agent()
        self.viewport = viewport or (1920, 1080)
        self.headed = headed
        self._command_timeout = 120.0
        
        # Ensure profile directory exists
        self.profile_dir.mkdir(parents=True, exist_ok=True)
    
    async def _run_command(self, args: list[str], timeout: Optional[float] = None) -> tuple[str, str, int]:
        """
        Run agent-browser command with stealth configuration.
        
        Args:
            args: Command arguments
            timeout: Command timeout in seconds
        
        Returns:
            Tuple of (stdout, stderr, returncode)
        """
        cmd = [_get_agent_browser_path()]
        
        # Add stealth profile
        cmd.extend(["--profile", str(self.profile_dir)])
        
        # Add stealth args
        cmd.extend(["--args", _get_stealth_args()])
        
        # Add user agent
        cmd.extend(["--user-agent", self.user_agent])
        
        # Add viewport size
        cmd.extend(["set", "viewport", str(self.viewport[0]), str(self.viewport[1])])
        
        # Add headed flag if requested
        if self.headed:
            cmd.append("--headed")
        
        # Add the actual command
        cmd.extend(args)
        
        logger.debug(f"Running stealth browser: {' '.join(cmd)}")
        
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        
        timeout = timeout or self._command_timeout
        
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return stdout.decode(), stderr.decode(), proc.returncode
        except asyncio.TimeoutError:
            proc.kill()
            return "", f"Command timed out after {timeout}s", -1
    
    async def _human_delay(self, min_ms: int = 100, max_ms: int = 500):
        """Add random delay to mimic human behavior."""
        delay = random.randint(min_ms, max_ms) / 1000
        await asyncio.sleep(delay)
    
    # === Core Browser Actions ===
    
    async def open(self, url: str) -> dict:
        """Navigate to URL with stealth configuration."""
        await self._human_delay(50, 200)
        stdout, stderr, code = await self._run_command(["open", url])
        
        if code != 0:
            return {"status": "error", "message": stderr or stdout}
        
        return {"status": "OK", "url": url, "message": stdout.strip()}
    
    async def snapshot(self, interactive_only: bool = True, compact: bool = True) -> dict:
        """Get accessibility tree snapshot."""
        args = ["snapshot"]
        if interactive_only:
            args.append("-i")
        if compact:
            args.append("-c")
        
        stdout, stderr, code = await self._run_command(args)
        
        if code != 0:
            return {"status": "error", "message": stderr or stdout}
        
        return {"status": "OK", "snapshot": stdout.strip()}
    
    async def click(self, ref_or_selector: str) -> dict:
        """Click element with human-like delay."""
        await self._human_delay(100, 400)  # Delay before click
        stdout, stderr, code = await self._run_command(["click", ref_or_selector])
        await self._human_delay(200, 800)  # Delay after click
        
        if code != 0:
            return {"status": "error", "message": stderr or stdout}
        
        return {"status": "OK", "message": f"Clicked {ref_or_selector}"}
    
    async def fill(self, ref_or_selector: str, text: str) -> dict:
        """Fill input with human-like typing (character by character)."""
        await self._human_delay(100, 300)
        
        # Use type instead of fill for more human-like behavior
        stdout, stderr, code = await self._run_command(["type", ref_or_selector, text])
        
        if code != 0:
            return {"status": "error", "message": stderr or stdout}
        
        await self._human_delay(200, 500)
        return {"status": "OK", "message": f"Typed into {ref_or_selector}"}
    
    async def type_slow(self, ref_or_selector: str, text: str, delay_ms: int = 50) -> dict:
        """Type text slowly character by character."""
        await self._human_delay(100, 300)
        
        # Type each character with delay
        for char in text:
            stdout, stderr, code = await self._run_command(
                ["type", ref_or_selector, char],
                timeout=10
            )
            if code != 0:
                return {"status": "error", "message": stderr or stdout}
            await asyncio.sleep(delay_ms / 1000 + random.uniform(0, 0.05))
        
        await self._human_delay(200, 500)
        return {"status": "OK", "message": f"Slow-typed into {ref_or_selector}"}
    
    async def press(self, key: str) -> dict:
        """Press keyboard key."""
        await self._human_delay(100, 300)
        stdout, stderr, code = await self._run_command(["press", key])
        await self._human_delay(200, 500)
        
        if code != 0:
            return {"status": "error", "message": stderr or stdout}
        
        return {"status": "OK", "message": f"Pressed {key}"}
    
    async def scroll(self, direction: str = "down", pixels: int = 500) -> dict:
        """Scroll page with human-like behavior."""
        await self._human_delay(100, 300)
        stdout, stderr, code = await self._run_command(["scroll", direction, str(pixels)])
        await self._human_delay(300, 700)
        
        if code != 0:
            return {"status": "error", "message": stderr or stdout}
        
        return {"status": "OK", "message": f"Scrolled {direction} {pixels}px"}
    
    async def screenshot(self, save_path: str = "", full_page: bool = False) -> dict:
        """Take screenshot."""
        args = ["screenshot"]
        if full_page:
            args.append("--full")
        if save_path:
            args.append(save_path)
        
        stdout, stderr, code = await self._run_command(args)
        
        if code != 0:
            return {"status": "error", "message": stderr or stdout}
        
        if save_path:
            return {"status": "OK", "saved_to": save_path}
        
        # Return base64 for inline display
        b64 = stdout.strip()
        return {
            "status": "OK",
            "size": len(b64) * 3 // 4,
            "_image_attachment": {"base64_data": b64, "media_type": "image/png"},
        }
    
    async def wait(self, selector: str = "", text: str = "", timeout_ms: int = 30000) -> dict:
        """Wait for element, text, or time."""
        if text:
            args = ["wait", "--text", text]
        elif selector:
            args = ["wait", selector]
        else:
            args = ["wait", str(timeout_ms)]
        
        stdout, stderr, code = await self._run_command(
            args, timeout=timeout_ms / 1000 + 5
        )
        
        if code != 0:
            return {"status": "error", "message": stderr or stdout}
        
        return {"status": "OK", "message": "Wait completed"}
    
    async def get_url(self) -> dict:
        """Get current page URL."""
        stdout, stderr, code = await self._run_command(["get", "url"])
        
        if code != 0:
            return {"status": "error", "message": stderr or stdout}
        
        return {"status": "OK", "url": stdout.strip()}
    
    async def get_text(self, ref_or_selector: str = "") -> dict:
        """Get text content."""
        args = ["get", "text"]
        if ref_or_selector:
            args.append(ref_or_selector)
        
        stdout, stderr, code = await self._run_command(args)
        
        if code != 0:
            return {"status": "error", "message": stderr or stdout}
        
        return {"status": "OK", "text": stdout.strip()}
    
    async def close(self) -> dict:
        """Close browser."""
        stdout, stderr, code = await self._run_command(["close"])
        
        if code != 0:
            return {"status": "error", "message": stderr or stdout}
        
        return {"status": "OK", "message": "Browser closed"}
    
    async def eval(self, js: str) -> dict:
        """Execute JavaScript on page."""
        stdout, stderr, code = await self._run_command(["eval", js])
        
        if code != 0:
            return {"status": "error", "message": stderr or stdout}
        
        return {"status": "OK", "result": stdout.strip()}
    
    # === Stealth Utilities ===
    
    async def mask_webdriver(self) -> dict:
        """
        Execute JavaScript to mask navigator.webdriver and other automation signals.
        Run this after opening a page to hide automation traces.
        """
        stealth_js = """
        // Overwrite the navigator.webdriver property
        Object.defineProperty(navigator, 'webdriver', {
            get: () => undefined,
        });
        
        // Overwrite the navigator.plugins to make it look real
        Object.defineProperty(navigator, 'plugins', {
            get: () => [
                {name: 'Chrome PDF Plugin'},
                {name: 'Chrome PDF Viewer'},
                {name: 'Native Client'}
            ],
        });
        
        // Overwrite the navigator.languages
        Object.defineProperty(navigator, 'languages', {
            get: () => ['en-US', 'en'],
        });
        
        // Remove webdriver-related properties from window
        delete navigator.__proto__.webdriver;
        
        // Hide the Chrome runtime if exposed
        if (window.chrome) {
            window.chrome.runtime = undefined;
        }
        
        // Overwrite permissions query to avoid detection
        const originalQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (parameters) => (
            parameters.name === 'notifications' 
                ? Promise.resolve({ state: Notification.permission })
                : originalQuery(parameters)
        );
        
        'Stealth applied';
        """
        
        return await self.eval(stealth_js)
    
    async def human_scroll(self, direction: str = "down", duration_ms: int = 2000) -> dict:
        """
        Scroll with human-like acceleration/deceleration pattern.
        
        Args:
            direction: "up" or "down"
            duration_ms: Total scroll duration in milliseconds
        """
        steps = 20
        step_delay = duration_ms / steps / 1000
        pixels_per_step = 500 / steps
        
        for i in range(steps):
            # Add some randomness to each step
            variation = random.uniform(0.8, 1.2)
            actual_pixels = int(pixels_per_step * variation)
            
            result = await self.scroll(direction, actual_pixels)
            if result.get("status") != "OK":
                return result
            
            # Variable delay between steps
            await asyncio.sleep(step_delay * random.uniform(0.7, 1.3))
        
        return {"status": "OK", "message": f"Human scroll {direction} completed"}


# === Sync Wrappers for Tool Integration ===

def stealth_open(url: str, profile_dir: Optional[str] = None) -> str:
    """Sync wrapper for stealth browser open."""
    browser = StealthBrowser(profile_dir=Path(profile_dir) if profile_dir else None)
    result = asyncio.get_event_loop().run_until_complete(browser.open(url))
    return json.dumps(result, indent=2)


def stealth_snapshot(interactive_only: bool = True, compact: bool = True) -> str:
    """Sync wrapper for stealth snapshot."""
    browser = StealthBrowser()
    result = asyncio.get_event_loop().run_until_complete(
        browser.snapshot(interactive_only, compact)
    )
    return json.dumps(result, indent=2)


def stealth_click(ref_or_selector: str) -> str:
    """Sync wrapper for stealth click."""
    browser = StealthBrowser()
    result = asyncio.get_event_loop().run_until_complete(browser.click(ref_or_selector))
    return json.dumps(result, indent=2)


def stealth_fill(ref_or_selector: str, text: str) -> str:
    """Sync wrapper for stealth fill."""
    browser = StealthBrowser()
    result = asyncio.get_event_loop().run_until_complete(browser.fill(ref_or_selector, text))
    return json.dumps(result, indent=2)


def stealth_screenshot(save_path: str = "", full_page: bool = False) -> str:
    """Sync wrapper for stealth screenshot."""
    browser = StealthBrowser()
    result = asyncio.get_event_loop().run_until_complete(
        browser.screenshot(save_path, full_page)
    )
    return json.dumps(result, indent=2)


def stealth_mask_webdriver() -> str:
    """Sync wrapper for masking webdriver."""
    browser = StealthBrowser()
    result = asyncio.get_event_loop().run_until_complete(browser.mask_webdriver())
    return json.dumps(result, indent=2)
