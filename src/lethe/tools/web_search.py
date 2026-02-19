"""Web search tool using Exa API.

Exa provides AI-powered semantic search with high-quality results.
Optional - only works if EXA_API_KEY is set in environment.
"""

import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


def _get_exa_api_key() -> Optional[str]:
    """Resolve Exa API key at call time (supports runtime env updates)."""
    key = os.environ.get("EXA_API_KEY", "").strip()
    return key or None


def _is_tool(func):
    """Decorator to mark a function as a Letta tool."""
    func._is_tool = True
    return func


@_is_tool
def web_search(
    query: str,
    num_results: int = 10,
    include_text: bool = False,
    category: str = "",
) -> str:
    """Search the web using Exa's AI-powered search engine.
    
    Returns relevant web pages with titles, URLs, and summaries.
    Use include_text=True to get full page content (uses more tokens).
    
    Args:
        query: Search query (natural language works best)
        num_results: Number of results to return (1-20, default: 10)
        include_text: Whether to include full page text (default: False, just summaries)
        category: Optional category filter: company, research paper, news, pdf, github, tweet
    
    Returns:
        JSON with search results including title, url, summary, and optionally full text
    """
    exa_api_key = _get_exa_api_key()
    if not exa_api_key:
        return json.dumps({
            "status": "error",
            "message": "Exa API not configured. Set EXA_API_KEY environment variable.",
        }, indent=2)
    
    try:
        import httpx
    except ImportError:
        return json.dumps({
            "status": "error",
            "message": "httpx not installed. Run: pip install httpx",
        }, indent=2)
    
    # Clamp num_results
    num_results = max(1, min(20, num_results))
    
    # Build request
    url = "https://api.exa.ai/search"
    headers = {
        "x-api-key": exa_api_key,
        "Content-Type": "application/json",
    }
    
    payload = {
        "query": query,
        "numResults": num_results,
        "type": "auto",  # Let Exa decide between keyword and neural
        "contents": {
            "text": {"maxCharacters": 2000} if include_text else False,
            "highlights": {"numSentences": 3},
            "summary": {"query": query},
        },
    }
    
    # Add category filter if specified
    valid_categories = ["company", "research paper", "news", "pdf", "github", "tweet"]
    if category and category.lower() in valid_categories:
        payload["category"] = category.lower()
    
    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as e:
        return json.dumps({
            "status": "error",
            "message": f"Exa API error: {e.response.status_code} - {e.response.text[:200]}",
        }, indent=2)
    except Exception as e:
        return json.dumps({
            "status": "error",
            "message": f"Request failed: {str(e)}",
        }, indent=2)
    
    # Format results
    results = []
    for item in data.get("results", []):
        result = {
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "summary": item.get("summary", ""),
        }
        
        # Add highlights if available
        if item.get("highlights"):
            result["highlights"] = item["highlights"]
        
        # Add full text if requested
        if include_text and item.get("text"):
            result["text"] = item["text"]
        
        # Add published date if available
        if item.get("publishedDate"):
            result["published"] = item["publishedDate"]
        
        results.append(result)
    
    return json.dumps({
        "status": "OK",
        "query": query,
        "num_results": len(results),
        "results": results,
    }, indent=2)


@_is_tool  
def fetch_webpage(url: str, max_chars: int = 5000) -> str:
    """Fetch and extract text content from a webpage.
    
    Uses Exa's content extraction to get clean text from a URL.
    
    Args:
        url: The URL to fetch
        max_chars: Maximum characters to return (default: 5000)
    
    Returns:
        Extracted text content from the page
    """
    exa_api_key = _get_exa_api_key()
    if not exa_api_key:
        return json.dumps({
            "status": "error", 
            "message": "Exa API not configured. Set EXA_API_KEY environment variable.",
        }, indent=2)
    
    try:
        import httpx
    except ImportError:
        return json.dumps({
            "status": "error",
            "message": "httpx not installed. Run: pip install httpx",
        }, indent=2)
    
    # Use Exa's contents endpoint
    api_url = "https://api.exa.ai/contents"
    headers = {
        "x-api-key": exa_api_key,
        "Content-Type": "application/json",
    }
    
    payload = {
        "ids": [url],  # Exa accepts URLs as IDs for content fetching
        "text": {"maxCharacters": max_chars},
    }
    
    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(api_url, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as e:
        return json.dumps({
            "status": "error",
            "message": f"Exa API error: {e.response.status_code} - {e.response.text[:200]}",
        }, indent=2)
    except Exception as e:
        return json.dumps({
            "status": "error",
            "message": f"Request failed: {str(e)}",
        }, indent=2)
    
    # Extract content
    results = data.get("results", [])
    if not results:
        return json.dumps({
            "status": "error",
            "message": f"Could not fetch content from {url}",
        }, indent=2)
    
    content = results[0]
    return json.dumps({
        "status": "OK",
        "url": content.get("url", url),
        "title": content.get("title", ""),
        "text": content.get("text", ""),
    }, indent=2)


def is_available() -> bool:
    """Check if web search is available (API key configured)."""
    return bool(_get_exa_api_key())
