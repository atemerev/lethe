"""Architecture-level integration tests for actor/runtime behavior."""

import asyncio
from pathlib import Path
import pytest
from unittest.mock import AsyncMock

from lethe.actor import ActorConfig, ActorRegistry
from lethe.actor.brainstem import Brainstem
from lethe.actor.integration import ActorSystem
from lethe.config import Settings
from lethe.memory.llm import AsyncLLMClient, LLMConfig


@pytest.mark.asyncio
async def test_user_notify_event_emitted():
    registry = ActorRegistry()
    cortex = registry.spawn(ActorConfig(name="cortex", group="main", goals="serve"), is_principal=True)
    worker = registry.spawn(ActorConfig(name="worker", group="main", goals="work"), spawned_by=cortex.id)

    await worker.send_to(
        cortex.id,
        "Check the deadline now",
        metadata={"channel": "user_notify", "kind": "deadline"},
    )

    events = registry.events.query(event_type="user_notify", actor_id=worker.id)
    assert events
    assert events[-1].payload["message"] == "Check the deadline now"


@pytest.mark.asyncio
async def test_llm_circuit_breaker_forces_final_response(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    config = LLMConfig(
        provider="openrouter",
        model="openrouter/moonshotai/kimi-k2.5-0127",
        model_aux="openrouter/qwen/qwen3-coder-next",
    )
    client = AsyncLLMClient(config=config, system_prompt="test")

    call_counts = {"api": 0, "no_tools": 0}

    async def fake_call_api():
        call_counts["api"] += 1
        return {
            "choices": [{
                "message": {
                    "content": "",
                    "tool_calls": [{
                        "id": f"call-{call_counts['api']}",
                        "function": {"name": "noop", "arguments": "{}"},
                    }],
                }
            }]
        }

    async def fake_call_api_no_tools():
        call_counts["no_tools"] += 1
        return {"choices": [{"message": {"content": "partial report"}}]}

    client._call_api = fake_call_api
    client._call_api_no_tools = fake_call_api_no_tools

    result = await client.chat("Do the task", max_tool_iterations=10)
    assert result == "partial report"
    assert call_counts["no_tools"] == 1
    assert call_counts["api"] < 10  # Circuit breaker should stop early.


@pytest.mark.asyncio
async def test_principal_monitor_keeps_done_and_failed_updates_in_cortex(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    class DummyLLM:
        def __init__(self):
            self._tools = {}
            self.tools = []
            self.context = type("Ctx", (), {"_tool_reference": "", "_build_tool_reference": lambda self, tools: ""})()
        def add_tool(self, func, schema=None):
            self._tools[func.__name__] = (func, schema or {})
        def _update_tool_budget(self):
            return None

    class DummyAgent:
        def __init__(self):
            self.llm = DummyLLM()
        def add_tool(self, func):
            self.llm.add_tool(func)

    agent = DummyAgent()
    actor_system = ActorSystem(agent)
    await actor_system.setup()
    send_to_user = AsyncMock()
    actor_system.set_callbacks(send_to_user=send_to_user)

    principal = actor_system.principal
    worker = actor_system.registry.spawn(
        ActorConfig(name="worker", group="main", goals="Do work"),
        spawned_by=principal.id,
    )

    worker.terminate("finished result")
    await asyncio.sleep(1.2)
    send_to_user.assert_not_awaited()
    first_msg = None
    for _ in range(5):
        msg = await principal.wait_for_reply(timeout=0.2)
        if msg and msg.metadata.get("kind") == "done":
            first_msg = msg
            break
    assert first_msg is not None
    assert first_msg.metadata.get("channel") == "task_update"
    assert first_msg.metadata.get("kind") == "done"

    failed = actor_system.registry.spawn(
        ActorConfig(name="failed-worker", group="main", goals="Fail"),
        spawned_by=principal.id,
    )
    failed.terminate("Error: boom")
    await asyncio.sleep(1.2)
    send_to_user.assert_not_awaited()
    second_msg = None
    for _ in range(5):
        msg = await principal.wait_for_reply(timeout=0.2)
        if msg and msg.metadata.get("kind") == "failed":
            second_msg = msg
            break
    assert second_msg is not None
    assert second_msg.metadata.get("channel") == "task_update"
    assert second_msg.metadata.get("kind") == "failed"

    await actor_system.shutdown()


@pytest.mark.asyncio
async def test_brainstem_user_notify_is_deferred_to_cortex(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    class DummyLLM:
        def __init__(self):
            self._tools = {}
            self.tools = []
            self.context = type("Ctx", (), {"_tool_reference": "", "_build_tool_reference": lambda self, tools: ""})()
        def add_tool(self, func, schema=None):
            self._tools[func.__name__] = (func, schema or {})
        def _update_tool_budget(self):
            return None

    class DummyAgent:
        def __init__(self):
            self.llm = DummyLLM()
        def add_tool(self, func):
            self.llm.add_tool(func)

    agent = DummyAgent()
    actor_system = ActorSystem(agent)
    await actor_system.setup()
    send_to_user = AsyncMock()
    actor_system.set_callbacks(send_to_user=send_to_user)

    principal = actor_system.principal
    brainstem_actor = actor_system.registry.spawn(
        ActorConfig(name="brainstem", group="main", goals="background notify"),
        spawned_by=principal.id,
    )

    await brainstem_actor.send_to(
        principal.id,
        "Important insight",
        metadata={"channel": "user_notify", "kind": "insight"},
    )
    await asyncio.sleep(1.2)
    send_to_user.assert_not_awaited()
    events = actor_system.registry.events.query(event_type="background_notify_deferred_to_cortex", actor_id=principal.id)
    assert events
    assert events[-1].payload.get("from_actor_name") == "brainstem"

    await brainstem_actor.send_to(
        principal.id,
        "Important insight",
        metadata={"channel": "user_notify", "kind": "insight"},
    )
    await asyncio.sleep(1.2)
    send_to_user.assert_not_awaited()

    await actor_system.shutdown()


@pytest.mark.asyncio
async def test_dmn_user_notify_is_deferred_to_cortex(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    class DummyLLM:
        def __init__(self):
            self._tools = {}
            self.tools = []
            self.context = type("Ctx", (), {"_tool_reference": "", "_build_tool_reference": lambda self, tools: ""})()
        def add_tool(self, func, schema=None):
            self._tools[func.__name__] = (func, schema or {})
        def _update_tool_budget(self):
            return None

    class DummyAgent:
        def __init__(self):
            self.llm = DummyLLM()
        def add_tool(self, func):
            self.llm.add_tool(func)

    agent = DummyAgent()
    actor_system = ActorSystem(agent)
    await actor_system.setup()
    send_to_user = AsyncMock()
    actor_system.set_callbacks(send_to_user=send_to_user)

    principal = actor_system.principal
    dmn_actor = actor_system.registry.spawn(
        ActorConfig(name="dmn", group="main", goals="background notify"),
        spawned_by=principal.id,
    )

    await dmn_actor.send_to(
        principal.id,
        "Urgent deadline tomorrow",
        metadata={"channel": "user_notify", "kind": "deadline"},
    )
    await asyncio.sleep(1.2)

    send_to_user.assert_not_awaited()
    events = actor_system.registry.events.query(event_type="background_notify_deferred_to_cortex", actor_id=principal.id)
    assert events
    assert events[-1].payload.get("from_actor_name") == "dmn"

    await actor_system.shutdown()


@pytest.mark.asyncio
async def test_brainstem_starts_first_and_is_online(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("BRAINSTEM_RELEASE_CHECK_ENABLED", "false")

    class DummyLLM:
        def __init__(self):
            self._tools = {}
            self.tools = []
            self.context = type("Ctx", (), {"_tool_reference": "", "_build_tool_reference": lambda self, tools: ""})()
        def add_tool(self, func, schema=None):
            self._tools[func.__name__] = (func, schema or {})
        def _update_tool_budget(self):
            return None

    class DummyAgent:
        def __init__(self):
            self.llm = DummyLLM()
        def add_tool(self, func):
            self.llm.add_tool(func)

    actor_system = ActorSystem(DummyAgent())
    await actor_system.setup()

    status = actor_system.status
    assert actor_system.brainstem is not None
    assert status.get("brainstem", {}).get("state") == "online"
    assert any(a.get("name") == "brainstem" for a in status.get("actors", []))

    await actor_system.shutdown()


@pytest.mark.asyncio
async def test_view_image_tool_registered_and_available_to_cortex(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "1")

    class DummyBlocks:
        def get(self, label):
            if label == "identity":
                return {"value": "You are a test assistant."}
            return None
        def list_blocks(self):
            return []

    class DummyMessages:
        def get_recent(self, n):
            return []
        def add(self, role, content, metadata=None):
            return None
        def count(self):
            return 0
        def search(self, query, limit=10):
            return []
        def search_by_role(self, query, role, limit=10):
            return []

    class DummyArchival:
        def add(self, text):
            return "id"
        def search(self, query, limit=10, search_type="hybrid"):
            return []
        def count(self):
            return 0

    class DummyMemory:
        def __init__(self):
            self.blocks = DummyBlocks()
            self.messages = DummyMessages()
            self.archival = DummyArchival()
        def get_context_for_prompt(self):
            return ""

    class DummySettings:
        llm_model = ""
        llm_model_aux = ""
        llm_api_base = ""
        llm_context_limit = 8000
        memory_dir = Path(".")
        workspace_dir = Path(".")
        lethe_config_dir = Path(".")
        llm_messages_load = 0
        llm_messages_summarize = 0

    from lethe.agent import Agent
    from lethe.actor.integration import ActorSystem
    from unittest.mock import patch

    with patch("lethe.agent.get_settings", return_value=DummySettings()), patch("lethe.agent.MemoryStore", return_value=DummyMemory()):
        agent = Agent()
        assert "view_image" in agent.llm._tools
        assert "web_search" in agent.llm._tools
        assert "browser_open" in agent.llm._tools
        assert "browser_snapshot" in agent.llm._tools
        assert "browser_click" in agent.llm._tools
        assert "browser_fill" in agent.llm._tools
        actor_system = ActorSystem(agent)
        await actor_system.setup()
    assert "view_image" in agent.llm._tools  # Kept on cortex in hybrid mode.
    assert "web_search" in agent.llm._tools
    assert "browser_open" in agent.llm._tools
    assert "browser_snapshot" in agent.llm._tools
    assert "browser_click" in agent.llm._tools
    assert "browser_fill" in agent.llm._tools


def test_extract_anthropic_unified_ratelimit_headers():
    headers = {
        "anthropic-ratelimit-unified-status": "allowed",
        "anthropic-ratelimit-unified-5h-status": "allowed",
        "anthropic-ratelimit-unified-5h-reset": "1771243200",
        "anthropic-ratelimit-unified-5h-utilization": "0.32",
        "anthropic-ratelimit-unified-7d-status": "allowed",
        "anthropic-ratelimit-unified-7d-reset": "1771639200",
        "anthropic-ratelimit-unified-7d-utilization": "0.75",
        "anthropic-ratelimit-unified-representative-claim": "five_hour",
        "anthropic-ratelimit-unified-fallback-percentage": "0.5",
        "anthropic-ratelimit-unified-reset": "1771243200",
    }

    parsed = AsyncLLMClient._extract_anthropic_ratelimit(headers)
    assert parsed["unified_status"] == "allowed"
    assert parsed["representative_claim"] == "five_hour"
    assert parsed["fallback_percentage"] == pytest.approx(0.5)
    assert parsed["five_hour"]["utilization"] == pytest.approx(0.32)
    assert parsed["seven_day"]["utilization"] == pytest.approx(0.75)


@pytest.mark.asyncio
async def test_brainstem_escalates_anthropic_near_limit_to_cortex(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("BRAINSTEM_RELEASE_CHECK_ENABLED", "false")
    monkeypatch.setenv("BRAINSTEM_INTEGRITY_CHECK_ENABLED", "false")
    monkeypatch.setenv("BRAINSTEM_RESOURCE_CHECK_ENABLED", "true")
    monkeypatch.setattr("lethe.actor.brainstem.ANTHROPIC_WARN_5H_UTIL", 0.85)
    monkeypatch.setattr("lethe.actor.brainstem.ANTHROPIC_WARN_7D_UTIL", 0.80)

    workspace = tmp_path / "workspace"
    memory = tmp_path / "memory"
    config_dir = tmp_path / "config"
    db_parent = tmp_path / "data"
    workspace.mkdir()
    memory.mkdir()
    config_dir.mkdir()
    db_parent.mkdir()

    settings = Settings(
        telegram_bot_token="test-token",
        telegram_allowed_user_ids="1",
        workspace_dir=workspace,
        memory_dir=memory,
        lethe_config_dir=config_dir,
        db_path=db_parent / "lethe.db",
    )

    registry = ActorRegistry()
    cortex = registry.spawn(ActorConfig(name="cortex", group="main", goals="serve"), is_principal=True)
    brainstem = Brainstem(
        registry=registry,
        settings=settings,
        cortex_id=cortex.id,
        install_dir=str(tmp_path),
    )
    await brainstem.startup()

    brainstem._collect_resource_snapshot = lambda: {
        "tokens_today": 10,
        "tokens_per_hour": 1,
        "api_calls_per_hour": 1,
        "process_rss_mb": 100,
        "workspace_free_gb": 1.0,
        "auth_mode": "subscription_oauth",
        "anthropic_ratelimit": {
            "unified_status": "allowed",
            "five_hour": {"utilization": 0.90},
            "seven_day": {"utilization": 0.75},
        },
    }

    await brainstem.heartbeat("tick")

    near_limit_msg = None
    for _ in range(20):
        msg = await cortex.wait_for_reply(timeout=0.2)
        if not msg:
            continue
        if "anthropic ratelimit near cap" in (msg.content or "").lower():
            near_limit_msg = msg
            break
    assert near_limit_msg is not None
    assert near_limit_msg.metadata.get("channel") == "task_update"


@pytest.mark.asyncio
async def test_brainstem_auto_update_success_notifies_user_offer_restart(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    workspace = tmp_path / "workspace"
    memory = tmp_path / "memory"
    config_dir = tmp_path / "config"
    db_parent = tmp_path / "data"
    workspace.mkdir()
    memory.mkdir()
    config_dir.mkdir()
    db_parent.mkdir()

    settings = Settings(
        telegram_bot_token="test-token",
        telegram_allowed_user_ids="1",
        workspace_dir=workspace,
        memory_dir=memory,
        lethe_config_dir=config_dir,
        db_path=db_parent / "lethe.db",
    )

    registry = ActorRegistry()
    cortex = registry.spawn(ActorConfig(name="cortex", group="main", goals="serve"), is_principal=True)
    brainstem = Brainstem(
        registry=registry,
        settings=settings,
        cortex_id=cortex.id,
        install_dir=str(tmp_path),
    )
    brainstem.auto_update_enabled = True
    brainstem._seen_release_tag = ""
    brainstem._repo_dirty = lambda: False
    brainstem._run_update_script = AsyncMock(return_value=(True, "updated"))
    brainstem._send_task_update = AsyncMock()
    brainstem._send_user_notify = AsyncMock()

    # ensure update.sh exists so skip path is not taken
    (tmp_path / "update.sh").write_text("#!/usr/bin/env bash\necho ok\n", encoding="utf-8")

    await brainstem._maybe_auto_update("v9.9.9")

    brainstem._send_task_update.assert_awaited()
    brainstem._send_user_notify.assert_awaited_once()
    notify_text = brainstem._send_user_notify.await_args.args[0]
    assert "v9.9.9" in notify_text
    assert "restart" in notify_text.lower()


@pytest.mark.asyncio
async def test_brainstem_auto_update_dirty_repo_uses_backup_path(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    workspace = tmp_path / "workspace"
    memory = tmp_path / "memory"
    config_dir = tmp_path / "config"
    db_parent = tmp_path / "data"
    workspace.mkdir()
    memory.mkdir()
    config_dir.mkdir()
    db_parent.mkdir()

    settings = Settings(
        telegram_bot_token="test-token",
        telegram_allowed_user_ids="1",
        workspace_dir=workspace,
        memory_dir=memory,
        lethe_config_dir=config_dir,
        db_path=db_parent / "lethe.db",
    )

    registry = ActorRegistry()
    cortex = registry.spawn(ActorConfig(name="cortex", group="main", goals="serve"), is_principal=True)
    brainstem = Brainstem(
        registry=registry,
        settings=settings,
        cortex_id=cortex.id,
        install_dir=str(tmp_path),
    )
    brainstem.auto_update_enabled = True
    brainstem._seen_release_tag = ""
    brainstem._repo_dirty = lambda: True
    brainstem._run_update_script = AsyncMock(return_value=(True, "updated"))
    brainstem._send_task_update = AsyncMock()
    brainstem._send_user_notify = AsyncMock()

    (tmp_path / "update.sh").write_text("#!/usr/bin/env bash\necho ok\n", encoding="utf-8")

    await brainstem._maybe_auto_update("v9.9.9")

    brainstem._run_update_script.assert_awaited_once()
    texts = [call.args[0] for call in brainstem._send_task_update.await_args_list if call.args]
    assert any("safety backup" in str(t).lower() for t in texts)
