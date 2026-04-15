"""Web search tool using Exa API with subagent synthesis.

Exa provides AI-powered semantic search with high-quality results.
Search results are synthesized by a separate LLM call (subagent pattern)
so that only a concise summary enters the main conversation context,
preserving context window for conversation history.

Raw results are saved to a temp file and referenced in the output,
so the agent can read them with file tools if more detail is needed.

Optional - only works if EXA_API_KEY is set in environment.
"""

import json
import logging
import os
import tempfile
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# Directory for raw search result files
_RAW_RESULTS_DIR = os.path.join(tempfile.gettempdir(), "lethe_web_search")


def _get_exa_api_key() -> Optional[str]:
    """Resolve Exa API key at call time (supports runtime env updates)."""
    key = os.environ.get("EXA_API_KEY", "").strip()
    return key or None


def _is_tool(func):
    """Decorator to mark a function as a Letta tool."""
    func._is_tool = True
    return func


def _get_llm_config():
    """Get LLM config from environment for synthesis calls."""
    model = os.environ.get("LLM_MODEL_AUX") or os.environ.get("LLM_MODEL", "")
    api_base = os.environ.get("LLM_API_BASE", "")
    return model, api_base


def _load_synthesis_prompt() -> str:
    """Load the synthesis system prompt from config/prompts/."""
    try:
        from lethe.prompts import load_prompt_template
        return load_prompt_template("web_search_synthesize")
    except Exception:
        pass
    # Fallback
    return (
        "You are a research assistant. Synthesize search results into a concise, "
        "informative answer. Include specific facts, numbers, and dates. "
        "Cite sources by number [1], [2], etc. Be thorough but concise — "
        "aim for 200-400 words. If the results don't answer the query well, say so."
    )


def _save_raw_results(query: str, results: list[dict]) -> str:
    """Save raw search results to a temp file. Returns the file path."""
    os.makedirs(_RAW_RESULTS_DIR, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    # Sanitize query for filename
    safe_query = "".join(c if c.isalnum() or c in " -_" else "" for c in query)[:50].strip()
    filename = f"{timestamp}_{safe_query}.json"
    filepath = os.path.join(_RAW_RESULTS_DIR, filename)

    with open(filepath, "w") as f:
        json.dump({
            "query": query,
            "timestamp": timestamp,
            "num_results": len(results),
            "results": results,
        }, f, indent=2, ensure_ascii=False)

    return filepath


def _synthesize_results(query: str, raw_results: list[dict]) -> str:
    """Synthesize search results using a separate LLM call (subagent pattern).

    This runs in its own context — raw results never enter the main conversation.
    Returns a concise summary (~300-500 tokens).
    """
    try:
        from litellm import completion
    except ImportError:
        return _format_raw_results(query, raw_results, max_chars=2000)

    model, api_base = _get_llm_config()
    if not model:
        return _format_raw_results(query, raw_results, max_chars=2000)

    system_prompt = _load_synthesis_prompt()

    # Format results for the synthesis prompt
    formatted = []
    for i, r in enumerate(raw_results, 1):
        parts = [f"{i}. **{r.get('title', 'Untitled')}**"]
        parts.append(f"   URL: {r.get('url', '')}")
        if r.get("summary"):
            parts.append(f"   {r['summary']}")
        if r.get("highlights"):
            for h in r["highlights"][:2]:
                parts.append(f"   > {h}")
        if r.get("published"):
            parts.append(f"   Published: {r['published']}")
        formatted.append("\n".join(parts))

    results_text = "\n\n".join(formatted)

    try:
        kwargs = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Query: {query}\n\nSearch results:\n\n{results_text}"},
            ],
            "temperature": 0.3,
            "max_tokens": 600,
        }
        if api_base:
            kwargs["api_base"] = api_base

        response = completion(**kwargs)
        synthesis = response.choices[0].message.content or ""

        # Append source URLs for reference
        sources = []
        for i, r in enumerate(raw_results[:5], 1):
            title = r.get("title", "")[:60]
            url = r.get("url", "")
            if url:
                sources.append(f"[{i}] {title} — {url}")

        if sources:
            synthesis += "\n\nSources:\n" + "\n".join(sources)

        return synthesis

    except Exception as e:
        logger.warning(f"Synthesis LLM call failed ({e}), falling back to raw results")
        return _format_raw_results(query, raw_results, max_chars=2000)


def _format_raw_results(query: str, results: list[dict], max_chars: int = 2000) -> str:
    """Fallback: format raw results compactly when synthesis is unavailable."""
    lines = [f"Search: {query}", f"{len(results)} results:", ""]
    for i, r in enumerate(results, 1):
        title = r.get("title", "")[:80]
        url = r.get("url", "")
        summary = r.get("summary", "")[:150]
        lines.append(f"{i}. {title}")
        lines.append(f"   {url}")
        if summary:
            lines.append(f"   {summary}")
        lines.append("")

    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n[...truncated]"
    return text


def _do_exa_search(query: str, num_results: int = 10, include_text: bool = False,
                   category: str = "") -> tuple[list[dict], Optional[str]]:
    """Execute the Exa search API call. Returns (results, error_message)."""
    exa_api_key = _get_exa_api_key()
    if not exa_api_key:
        return [], "Exa API not configured. Set EXA_API_KEY environment variable."

    try:
        import httpx
    except ImportError:
        return [], "httpx not installed. Run: pip install httpx"

    num_results = max(1, min(20, num_results))

    url = "https://api.exa.ai/search"
    headers = {
        "x-api-key": exa_api_key,
        "Content-Type": "application/json",
    }

    payload = {
        "query": query,
        "numResults": num_results,
        "type": "auto",
        "contents": {
            "text": {"maxCharacters": 2000} if include_text else False,
            "highlights": {"numSentences": 3},
            "summary": {"query": query},
        },
    }

    valid_categories = ["company", "research paper", "news", "pdf", "github", "tweet"]
    if category and category.lower() in valid_categories:
        payload["category"] = category.lower()

    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
    except Exception as e:
        return [], f"Request failed: {str(e)}"

    results = []
    for item in data.get("results", []):
        result = {
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "summary": item.get("summary", ""),
        }
        if item.get("highlights"):
            result["highlights"] = item["highlights"]
        if include_text and item.get("text"):
            result["text"] = item["text"]
        if item.get("publishedDate"):
            result["published"] = item["publishedDate"]
        results.append(result)

    return results, None


@_is_tool
def web_search(
    query: str,
    num_results: int = 10,
    include_text: bool = False,
    category: str = "",
) -> str:
    """Search the web using Exa's AI-powered search engine.

    Returns a synthesized answer with source links. Raw results are saved
    to a file (path included in output) — use read_file to access full details.
    Use fetch_webpage for full page content when needed.

    Args:
        query: Search query (natural language works best)
        num_results: Number of results to return (1-20, default: 10)
        include_text: Whether to include full page text in synthesis (default: False)
        category: Optional category filter: company, research paper, news, pdf, github, tweet

    Returns:
        Synthesized answer with source links and path to raw results file
    """
    results, error = _do_exa_search(query, num_results, include_text, category)

    if error:
        return json.dumps({"status": "error", "message": error}, indent=2)

    if not results:
        return json.dumps({
            "status": "OK",
            "query": query,
            "message": "No results found.",
        }, indent=2)

    # Save raw results to temp file for later reference
    raw_file = _save_raw_results(query, results)

    # Synthesize in a separate LLM context (subagent pattern)
    synthesis = _synthesize_results(query, results)

    # Append raw file reference
    synthesis += f"\n\n(Raw results: {raw_file})"

    return synthesis


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

    api_url = "https://api.exa.ai/contents"
    headers = {
        "x-api-key": exa_api_key,
        "Content-Type": "application/json",
    }

    payload = {
        "ids": [url],
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
