"""Regression tests for API-mode worker routing and gateway chat mapping."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from starlette.testclient import TestClient

from gateway.pool import ContainerInfo
from gateway.router import Router
from lethe import api


def _auth_headers() -> dict[str, str]:
    return {"X-Lethe-Token": "test-token"}


def _reset_api_state():
    api._agent = None
    api._conversation_manager = None
    api._actor_system = None
    api._heartbeat = None
    api._settings = None
    api._proactive_queue = api.asyncio.Queue()
    api._api_sessions.clear()
    api._chat_sessions.clear()


def test_file_route_requires_auth_and_stays_in_workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("LETHE_API_TOKEN", "test-token")
    _reset_api_state()

    inside = tmp_path / "inside.txt"
    inside.write_text("inside")
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("outside")
    api._settings = SimpleNamespace(workspace_dir=tmp_path)

    client = TestClient(api.app)

    unauthorized = client.get("/file", params={"path": str(inside)})
    assert unauthorized.status_code == 401

    allowed = client.get("/file", params={"path": str(inside)}, headers=_auth_headers())
    assert allowed.status_code == 200
    assert allowed.text == "inside"

    denied = client.get("/file", params={"path": str(outside)}, headers=_auth_headers())
    assert denied.status_code == 403


def test_chat_route_uses_conversation_manager_session(monkeypatch):
    monkeypatch.setenv("LETHE_API_TOKEN", "test-token")
    _reset_api_state()

    class FakeConversationManager:
        def __init__(self):
            self.calls = []

        async def add_message(self, *, chat_id, user_id, content, metadata, process_callback):
            self.calls.append(
                {
                    "chat_id": chat_id,
                    "user_id": user_id,
                    "content": content,
                    "metadata": dict(metadata),
                    "process_callback": process_callback,
                }
            )
            await api._close_session(metadata["_api_session_id"], remove=False)
            return True

    fake_agent = SimpleNamespace(chat=AsyncMock())
    manager = FakeConversationManager()
    api._agent = fake_agent
    api._conversation_manager = manager

    client = TestClient(api.app)
    with client.stream(
        "POST",
        "/chat",
        headers=_auth_headers(),
        json={"message": "hello", "user_id": 7, "chat_id": 9, "metadata": {"message_id": 42}},
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert "event: done" in body
    assert len(manager.calls) == 1
    call = manager.calls[0]
    assert call["chat_id"] == 9
    assert call["user_id"] == 7
    assert call["content"] == "hello"
    assert call["process_callback"] is api._process_chat_message
    assert call["metadata"]["message_id"] == 42
    assert call["metadata"]["_api_session_id"]
    assert fake_agent.chat.await_count == 0


def test_model_route_rebuilds_runtime_state(monkeypatch):
    monkeypatch.setenv("LETHE_API_TOKEN", "test-token")
    _reset_api_state()
    monkeypatch.setattr("lethe.models.provider_for_model", lambda model: "anthropic")

    class FakeAgent:
        def __init__(self):
            self.calls = []
            self.llm = SimpleNamespace(
                config=SimpleNamespace(model="old-main", model_aux="old-aux", provider="openrouter"),
                _force_oauth=None,
                _oauth=None,
                _oauth_provider="",
            )

        async def reconfigure_models(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs.get("provider"):
                self.llm.config.provider = kwargs["provider"]
            if kwargs.get("model"):
                self.llm.config.model = kwargs["model"]
            if kwargs.get("model_aux"):
                self.llm.config.model_aux = kwargs["model_aux"]
            self.llm._force_oauth = kwargs.get("force_oauth")
            changed = {}
            if kwargs.get("provider"):
                changed["provider"] = {"old": "openrouter", "new": kwargs["provider"]}
            if kwargs.get("model"):
                changed["model"] = {"old": "old-main", "new": kwargs["model"]}
            return changed

    api._agent = FakeAgent()
    client = TestClient(api.app)

    response = client.post(
        "/model",
        headers=_auth_headers(),
        json={"model": "claude-test", "auth": "sub"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider"] == "anthropic"
    assert payload["model"] == "claude-test"
    assert payload["changed"]["provider"]["new"] == "anthropic"
    assert api._agent.calls == [
        {
            "provider": "anthropic",
            "model": "claude-test",
            "model_aux": None,
            "force_oauth": True,
        }
    ]


def test_router_updates_chat_target_without_duplicate_listener(monkeypatch):
    def fake_create_task(coro, *, name=None):
        coro.close()
        return SimpleNamespace(cancel=lambda: None, name=name)

    create_task = Mock(side_effect=fake_create_task)
    monkeypatch.setattr("gateway.router.asyncio.create_task", create_task)

    router = Router(bot=AsyncMock(), api_token="test-token")
    container = ContainerInfo(
        container_id="cid-1",
        container_name="worker-1",
        state="assigned",
        port=9000,
        workspace_path="/tmp/worker-1",
    )

    router.start_event_listener(container, 111)
    router.start_event_listener(container, 222)

    assert router._event_chat_ids[container.container_id] == 222
    assert create_task.call_count == 1
