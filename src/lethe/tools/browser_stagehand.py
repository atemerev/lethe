"""
Stagehand-based browser automation tools.

Uses Stagehand's AI-native approach for more reliable browser automation,
especially for modals, shadow DOM, and iframes.

Stagehand provides three core primitives:
- act(): Perform actions using natural language
- extract(): Extract structured data from pages  
- observe(): Find elements and get suggestions for actions

Local mode requires:
- Chrome/Chromium installed locally
- MODEL_API_KEY or OPENAI_API_KEY environment variable
"""

import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# Global session state
_stagehand_client = None
_session_id: Optional[str] = None


def _get_api_key() -> str:
    """Get the LLM API key from environment."""
    key = os.environ.get("MODEL_API_KEY") or os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("Set MODEL_API_KEY, OPENAI_API_KEY, or ANTHROPIC_API_KEY to use Stagehand")
    return key


async def _get_session() -> str:
    """Get or create a Stagehand session. Returns session_id."""
    global _stagehand_client, _session_id
    
    if _session_id is not None:
        return _session_id
    
    from stagehand import Stagehand
    
    api_key = _get_api_key()
    
    # Create client in local mode - uses bundled binary
    # Dummy values for browserbase params - not used in local mode
    _stagehand_client = Stagehand(
        server="local",
        browserbase_api_key="local",  # Dummy - not used in local mode
        browserbase_project_id="local",  # Dummy - not used in local mode
        model_api_key=api_key,
        local_headless=False,  # Set to True for headless
        local_port=0,  # Auto-pick free port
        local_ready_timeout_s=30.0,
    )
    
    logger.info("Starting Stagehand local session...")
    
    # Start session with local browser
    session = _stagehand_client.sessions.start(
        model_name="openai/gpt-4o",
        browser={
            "type": "local",
            "launchOptions": {
                "headless": False,  # Match the client setting
            },
        },
    )
    _session_id = session.data.session_id
    
    logger.info(f"Stagehand session started: {_session_id}")
    logger.info(f"Server running at: {_stagehand_client.base_url}")
    
    return _session_id


async def stagehand_navigate_async(url: str) -> str:
    """Navigate to a URL.
    
    Args:
        url: The URL to navigate to
    
    Returns:
        JSON with navigation result
    """
    session_id = await _get_session()
    
    try:
        _stagehand_client.sessions.navigate(
            id=session_id,
            url=url,
        )
        
        return json.dumps({
            "status": "OK",
            "url": url,
            "message": f"Navigated to {url}",
        }, indent=2)
    except Exception as e:
        return json.dumps({
            "status": "error",
            "message": str(e),
        }, indent=2)


async def stagehand_act_async(instruction: str) -> str:
    """Perform an action using natural language.
    
    This is Stagehand's most powerful feature - describe what you want to do
    in plain English and it will figure out how to do it, even for elements
    in modals, shadow DOM, or iframes.
    
    Args:
        instruction: Natural language description of the action to perform.
                    Examples:
                    - "click the login button"
                    - "fill the email field with user@example.com"
                    - "scroll down to see more content"
                    - "close the cookie consent popup"
                    - "click Accept in the modal dialog"
    
    Returns:
        JSON with action result
    """
    session_id = await _get_session()
    
    try:
        response = _stagehand_client.sessions.act(
            id=session_id,
            input=instruction,
        )
        result = response.data.result
        
        return json.dumps({
            "status": "OK",
            "message": result.message if hasattr(result, 'message') else str(result),
            "success": getattr(result, 'success', True),
        }, indent=2)
    except Exception as e:
        return json.dumps({
            "status": "error",
            "message": str(e),
            "success": False,
        }, indent=2)


async def stagehand_extract_async(
    instruction: str,
    schema: Optional[dict] = None,
) -> str:
    """Extract structured data from the current page.
    
    Uses AI to understand the page and extract the requested information.
    
    Args:
        instruction: What to extract, e.g. "extract all product names and prices"
        schema: Optional JSON schema for the extracted data. If not provided,
               returns free-form extracted text.
               Example schema:
               {
                   "type": "object",
                   "properties": {
                       "products": {
                           "type": "array",
                           "items": {
                               "type": "object", 
                               "properties": {
                                   "name": {"type": "string"},
                                   "price": {"type": "string"}
                               }
                           }
                       }
                   }
               }
    
    Returns:
        JSON with extracted data
    """
    session_id = await _get_session()
    
    try:
        kwargs = {
            "id": session_id,
            "instruction": instruction,
        }
        if schema:
            kwargs["schema"] = schema
        
        response = _stagehand_client.sessions.extract(**kwargs)
        result = response.data.result
        
        return json.dumps({
            "status": "OK",
            "data": result if isinstance(result, dict) else {"result": str(result)},
        }, indent=2)
    except Exception as e:
        return json.dumps({
            "status": "error",
            "message": str(e),
        }, indent=2)


async def stagehand_observe_async(instruction: str = "") -> str:
    """Observe the page and find possible actions.
    
    Use this to understand what's on the page and what actions are available.
    Stagehand will analyze the page and return a list of possible actions.
    
    Args:
        instruction: Optional hint about what you're looking for.
                    Examples:
                    - "find the login button"
                    - "find all links in the navigation"
                    - "find the submit button in the modal"
    
    Returns:
        JSON with list of observed actions/elements
    """
    session_id = await _get_session()
    
    try:
        response = _stagehand_client.sessions.observe(
            id=session_id,
            instruction=instruction or "list all interactive elements on the page",
        )
        
        results = response.data.result
        
        # Convert to list of dicts
        actions = []
        for r in results:
            if hasattr(r, 'to_dict'):
                action = r.to_dict()
            elif hasattr(r, 'model_dump'):
                action = r.model_dump()
            else:
                action = {"description": str(r)}
            actions.append(action)
        
        return json.dumps({
            "status": "OK",
            "actions": actions,
            "count": len(actions),
        }, indent=2)
    except Exception as e:
        return json.dumps({
            "status": "error",
            "message": str(e),
        }, indent=2)


async def stagehand_screenshot_async(save_path: str = "") -> str:
    """Take a screenshot of the current page.
    
    Args:
        save_path: Optional path to save the screenshot file
    
    Returns:
        JSON with screenshot info
    """
    import base64
    
    session_id = await _get_session()
    
    try:
        response = _stagehand_client.sessions.screenshot(
            id=session_id,
            full_page=False,
        )
        
        screenshot_b64 = response.data.screenshot if hasattr(response.data, 'screenshot') else str(response.data)
        
        result = {
            "status": "OK",
            "screenshot_base64": screenshot_b64,
            "size": len(base64.b64decode(screenshot_b64)) if screenshot_b64 else 0,
        }
        
        if save_path and screenshot_b64:
            from pathlib import Path
            Path(save_path).write_bytes(base64.b64decode(screenshot_b64))
            result["saved_to"] = save_path
        
        return json.dumps(result, indent=2)
    except Exception as e:
        return json.dumps({
            "status": "error", 
            "message": str(e),
        }, indent=2)


async def stagehand_close_async() -> str:
    """Close the Stagehand browser session.
    
    Returns:
        JSON with close result
    """
    global _stagehand_client, _session_id
    
    try:
        if _session_id and _stagehand_client:
            _stagehand_client.sessions.end(id=_session_id)
            _session_id = None
        
        if _stagehand_client:
            _stagehand_client.close()
            _stagehand_client = None
        
        return json.dumps({
            "status": "OK",
            "message": "Session closed",
        }, indent=2)
    except Exception as e:
        return json.dumps({
            "status": "error",
            "message": str(e),
        }, indent=2)
