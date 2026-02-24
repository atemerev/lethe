import json

import pytest

from lethe.memory import openai_oauth
from lethe.memory.llm import AsyncLLMClient, LLMConfig, Message
from lethe.memory.openai_oauth import OpenAIOAuth, is_oauth_available_openai
from lethe.tools import oauth_login_openai


def test_is_oauth_available_openai_from_env(monkeypatch):
    monkeypatch.setenv("OPENAI_AUTH_TOKEN", "test-openai-oauth-token")
    assert is_oauth_available_openai()


def test_is_oauth_available_openai_from_token_file(monkeypatch, tmp_path):
    token_file = tmp_path / "openai_tokens.json"
    token_file.write_text(json.dumps({"access_token": "token-from-file"}))

    monkeypatch.delenv("OPENAI_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(openai_oauth, "TOKEN_FILE", token_file)

    assert is_oauth_available_openai()


def test_openai_oauth_headers_use_openclaw_identity_profile():
    oauth = OpenAIOAuth(access_token="access-token", account_id="acct_123")
    headers = oauth._build_headers()

    assert headers["authorization"] == "Bearer access-token"
    assert headers["chatgpt-account-id"] == "acct_123"
    assert headers["originator"] == "pi"
    assert headers["OpenAI-Beta"] == "responses=experimental"
    assert headers["user-agent"].startswith("pi (")


def test_openai_oauth_parse_response_maps_tools_and_usage():
    oauth = OpenAIOAuth(access_token="access-token")
    payload = {
        "id": "resp_abc",
        "model": "gpt-5.3-codex",
        "output": [
            {
                "type": "message",
                "content": [
                    {"type": "output_text", "text": "hello "},
                    {"type": "output_text", "text": "world"},
                ],
            },
            {
                "type": "function_call",
                "id": "call_123",
                "name": "bash",
                "arguments": "{\"cmd\":\"ls\"}",
            },
        ],
        "stop_reason": "tool_call",
        "usage": {
            "input_tokens": 12,
            "output_tokens": 7,
            "input_tokens_details": {"cached_tokens": 3},
        },
    }

    parsed = oauth._parse_response(payload)

    assert parsed["choices"][0]["message"]["content"] == "hello world"
    assert parsed["choices"][0]["finish_reason"] == "tool_calls"
    assert parsed["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "bash"
    assert parsed["usage"]["prompt_tokens"] == 12
    assert parsed["usage"]["completion_tokens"] == 7
    assert parsed["usage"]["prompt_tokens_details"]["cached_tokens"] == 3


def test_openai_oauth_normalize_model_maps_unsupported_mini_variant():
    oauth = OpenAIOAuth(access_token="access-token")
    assert oauth._normalize_model("gpt-5.2-mini") == "gpt-5.2"
    assert oauth._normalize_model("gpt-5.1-codex-mini") == "gpt-5.1-codex-mini"


def test_openai_oauth_extract_instructions_from_system_messages():
    oauth = OpenAIOAuth(access_token="access-token")
    instructions, non_system = oauth._extract_instructions(
        [
            {"role": "system", "content": "System A"},
            {"role": "user", "content": "hello"},
            {"role": "system", "content": [{"type": "text", "text": "System B"}]},
        ]
    )
    assert instructions == "System A\n\nSystem B"
    assert non_system == [{"role": "user", "content": "hello"}]


def test_openai_oauth_normalize_messages_converts_image_url_object_to_string():
    oauth = OpenAIOAuth(access_token="access-token")
    normalized = oauth._normalize_messages(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this?"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/jpeg;base64,AAA"},
                    },
                ],
            }
        ]
    )

    assert normalized[0]["role"] == "user"
    assert normalized[0]["content"][0] == {"type": "input_text", "text": "what is this?"}
    assert normalized[0]["content"][1] == {
        "type": "input_image",
        "image_url": "data:image/jpeg;base64,AAA",
    }


def test_openai_oauth_parse_streamed_response_prefers_completed_event():
    oauth = OpenAIOAuth(access_token="access-token")
    sse = (
        'event: response.created\n'
        'data: {"type":"response.created","response":{"id":"resp_1","model":"gpt-5.2","output":[]}}\n\n'
        'event: response.completed\n'
        'data: {"type":"response.completed","response":{"id":"resp_1","model":"gpt-5.2","output":[{"type":"message","content":[{"type":"output_text","text":"ok"}]}]}}\n\n'
    )
    parsed = oauth._parse_streamed_response(sse)
    assert parsed["id"] == "resp_1"
    assert parsed["output"][0]["type"] == "message"


@pytest.mark.asyncio
async def test_openai_oauth_call_messages_uses_stream_and_instructions(monkeypatch):
    class _Resp:
        status_code = 200
        headers = {"content-type": "text/event-stream"}

        text = (
            'event: response.completed\n'
            'data: {"type":"response.completed","response":{"id":"resp_1","model":"gpt-5.2","output":[{"type":"message","content":[{"type":"output_text","text":"ok"}]}],"stop_reason":"stop","usage":{"input_tokens":1,"output_tokens":1}}}\n\n'
        )

        def json(self):
            return {}

    class _Client:
        def __init__(self):
            self.calls = []

        async def post(self, url, headers=None, json=None):
            self.calls.append({"url": url, "headers": headers, "json": json})
            return _Resp()

    oauth = OpenAIOAuth(access_token="access-token")
    fake_client = _Client()

    async def _noop():
        return None

    async def _get_client():
        return fake_client

    monkeypatch.setattr(oauth, "ensure_access", _noop)
    monkeypatch.setattr(oauth, "_get_client", _get_client)

    await oauth.call_messages(
        model="gpt-5.2-mini",
        messages=[
            {"role": "system", "content": "sys prompt"},
            {"role": "user", "content": "hello"},
        ],
    )

    payload = fake_client.calls[0]["json"]
    assert payload["model"] == "gpt-5.2"
    assert payload["instructions"] == "sys prompt"
    assert payload["stream"] is True
    assert "max_output_tokens" not in payload


@pytest.mark.asyncio
async def test_openai_device_poll_times_out(monkeypatch):
    class _Resp:
        status_code = 404
        text = "pending"

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, headers=None, json=None):
            return _Resp()

    fake_clock = {"now": 0.0}

    def _monotonic():
        fake_clock["now"] += 0.6
        return fake_clock["now"]

    async def _noop_sleep(seconds):
        return None

    monkeypatch.setattr(oauth_login_openai.httpx, "AsyncClient", lambda timeout=30.0: _Client())
    monkeypatch.setattr(oauth_login_openai.time, "monotonic", _monotonic)
    monkeypatch.setattr(oauth_login_openai.asyncio, "sleep", _noop_sleep)

    with pytest.raises(RuntimeError, match="Timed out waiting for OpenAI device authorization"):
        await oauth_login_openai._openai_poll_for_authorization_code(
            device_auth_id="dev_123",
            user_code="code_abc",
            interval_seconds=1,
            timeout_seconds=1,
        )


def test_llm_config_accepts_openai_oauth_without_api_key(monkeypatch):
    monkeypatch.setenv("OPENAI_AUTH_TOKEN", "test-openai-oauth-token")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)

    config = LLMConfig(provider="openai")
    assert config.provider == "openai"


def test_llm_config_auto_detects_openai_oauth_provider(monkeypatch):
    monkeypatch.setenv("OPENAI_AUTH_TOKEN", "test-openai-oauth-token")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)

    config = LLMConfig()
    assert config.provider == "openai"


def test_async_llm_client_prefers_openai_oauth_over_api_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-api-key")
    monkeypatch.setenv("OPENAI_AUTH_TOKEN", "test-openai-oauth-token")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)

    config = LLMConfig(provider="openai", model="gpt-5.3-codex")
    client = AsyncLLMClient(config=config, system_prompt="test")
    assert isinstance(client._oauth, OpenAIOAuth)


def test_context_build_messages_preserves_large_multimodal_image_payload(monkeypatch):
    monkeypatch.setenv("OPENAI_AUTH_TOKEN", "test-openai-oauth-token")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    config = LLMConfig(provider="openai", model="gpt-5.2")
    client = AsyncLLMClient(config=config, system_prompt="test")

    large_b64 = "A" * 60000
    client.context.add_message(
        Message(
            role="user",
            content=[
                {"type": "text", "text": "look"},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{large_b64}"},
                },
            ],
        )
    )

    messages = client.context.build_messages()
    user_messages = [m for m in messages if m.get("role") == "user"]
    assert user_messages, "Expected at least one user message in built context"
    latest_user = user_messages[-1]
    assert isinstance(latest_user.get("content"), list)
    assert latest_user["content"][1]["type"] == "image_url"
    assert latest_user["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
