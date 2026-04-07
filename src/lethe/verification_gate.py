"""Verification gate — aux model classifier that triggers verification protocol.

Flow:
1. Before the LLM's final response is returned to the user, classify it with haiku
2. If it's a completion claim WITHOUT embedded verification evidence → inject
   the verification protocol as a user message and force another LLM turn
3. If it already contains evidence, or isn't a completion claim → pass through

Uses direct Anthropic API (httpx) with Bearer auth, same as AnthropicOAuth client.
"""

import json
import logging
import os
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

MESSAGES_URL = "https://api.anthropic.com/v1/messages"

CLASSIFIER_PROMPT = """You are a message classifier for an AI assistant's outgoing messages.

Determine:
1. Is this message a SUBSTANTIVE TASK COMPLETION REPORT — explicitly claiming that a multi-step task the user requested has been finished, fixed, deployed, or is ready?
2. Does the message contain SPECIFIC verification evidence (actual command output, endpoint responses, log lines, test results, status codes, screenshots)?

CRITICAL: "is_completion" should ONLY be true for messages that are the FINAL REPORT of completing a real task. It must NOT be true for:
- Casual acknowledgments ("already done", "we're good", "nothing to worry about")
- Status updates about background processes or subagents
- Conversational replies, opinions, or observations
- Acknowledging a system notification
- Answering a question
- Progress updates ("still working on it", "halfway there")
- Offering to do something ("want me to fix that?")

Respond with JSON only, no other text:
{"is_completion": true/false, "has_evidence": true/false}

Examples of completion WITHOUT evidence (is_completion: true):
- "The fix is deployed, try it now" → {"is_completion": true, "has_evidence": false}
- "Should work now!" → {"is_completion": true, "has_evidence": false}
- "All set, the service is running. Ready for real work." → {"is_completion": true, "has_evidence": false}

Examples of completion WITH evidence (is_completion: true, passes gate):
- "Verified: curl returns {"status":"ok"}, logs show no errors since restart" → {"is_completion": true, "has_evidence": true}
- "Tested: endpoint returns 200, response body matches expected schema" → {"is_completion": true, "has_evidence": true}
- "Done. Checked: health endpoint returns ok. Confidence: 95%." → {"is_completion": true, "has_evidence": true}

Examples of NOT completion (is_completion: false):
- "Let me check the logs" → {"is_completion": false, "has_evidence": false}
- "I found the bug, it's in line 42" → {"is_completion": false, "has_evidence": false}
- "Working on it..." → {"is_completion": false, "has_evidence": false}
- "Here's what I think the issue is" → {"is_completion": false, "has_evidence": false}
- "Already reviewed and reported that earlier." → {"is_completion": false, "has_evidence": false}
- "Ghost message — that subagent already finished. Nothing to worry about." → {"is_completion": false, "has_evidence": false}
- "We're good. 👍" → {"is_completion": false, "has_evidence": false}
- "Yeah, that was a stale notification." → {"is_completion": false, "has_evidence": false}
- "Want me to adjust the gate sensitivity?" → {"is_completion": false, "has_evidence": false}"""

# Load verification protocol using the standard prompt template system
_protocol_cache: str | None = None


def _load_protocol() -> str:
    global _protocol_cache
    if _protocol_cache is None:
        try:
            from lethe.prompts import load_prompt_template
            _protocol_cache = load_prompt_template("verification_mandatory")
        except Exception:
            _protocol_cache = (
                "VERIFICATION REQUIRED: Before reporting task completion, you must "
                "verify the actual result — run the command, check the endpoint, "
                "view the screenshot. Report with specific evidence."
            )
    return _protocol_cache


async def classify_message(text: str) -> dict:
    """Classify whether a message is an unverified completion claim.

    Uses direct Anthropic API with Bearer auth (same as AnthropicOAuth client).
    Returns {"is_completion": bool, "has_evidence": bool}
    On any error, returns pass-through (is_completion=False).
    """
    auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if not auth_token:
        logger.warning("Verification gate: no ANTHROPIC_AUTH_TOKEN, passing through")
        return {"is_completion": False, "has_evidence": False}

    try:
        headers = {
            "authorization": f"Bearer {auth_token}",
            "content-type": "application/json",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "claude-code-20250219,oauth-2025-04-20",
            "anthropic-dangerous-direct-browser-access": "true",
            "user-agent": "claude-cli/2.1.81 (external, cli)",
        }

        payload = {
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 50,
            "temperature": 0.0,
            "messages": [
                {"role": "user", "content": f"{CLASSIFIER_PROMPT}\n\nMessage to classify:\n{text}"},
            ],
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(MESSAGES_URL, headers=headers, json=payload)
            response.raise_for_status()

        data = response.json()
        raw = data["content"][0]["text"].strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            raw = raw.rsplit("```", 1)[0].strip()
        result = json.loads(raw)
        logger.info("Verification gate classified: %s → %s", text[:80], result)
        return result

    except Exception as e:
        logger.warning("Verification gate classifier error (passing through): %s", e)
        return {"is_completion": False, "has_evidence": False}


async def check_gate(text: str, **_kwargs) -> str | None:
    """Check if a message should trigger verification protocol injection.

    Returns:
        - Protocol text to inject as a user message if verification needed
        - None if message should pass through normally
    """
    # Skip very short messages (single emoji reactions)
    if len(text.strip()) < 5:
        return None

    # Skip if gate is disabled
    if os.environ.get("VERIFICATION_GATE", "1").strip() in ("0", "false", "no"):
        return None

    result = await classify_message(text)

    if result.get("is_completion") and not result.get("has_evidence"):
        logger.warning(
            "VERIFICATION GATE TRIGGERED — completion without evidence: %s",
            text[:200],
        )
        protocol = _load_protocol()
        return (
            f"[VERIFICATION GATE ACTIVATED — your response was classified as a task "
            f"completion claim without verification evidence. You MUST follow the "
            f"verification protocol before responding to the user. Your previous "
            f"response has NOT been sent.]\n\n{protocol}\n\n"
            f"[Now execute the appropriate verification steps for your task and "
            f"respond with the actual evidence.]"
        )

    return None
