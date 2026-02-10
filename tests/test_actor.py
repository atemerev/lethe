"""Tests for the actor model.

Tests cover:
- Actor lifecycle (create, run, terminate)
- Inter-actor messaging with relationship checks
- Group discovery with relationship labels
- Principal vs subagent roles
- Parent killing children
- Duplicate detection on spawn
- Model choice for subagents
- Actor tools (spawn, send, discover, terminate, kill)
- Runner execution loop
- Edge cases (timeout, max turns, orphaned actors)
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from lethe.actor import (
    Actor,
    ActorConfig,
    ActorInfo,
    ActorMessage,
    ActorRegistry,
    ActorState,
)
from lethe.actor.tools import create_actor_tools
from lethe.actor.runner import ActorRunner


# ── Fixtures ──────────────────────────────────────────────────


@pytest.fixture
def registry():
    return ActorRegistry()


@pytest.fixture
def principal_config():
    return ActorConfig(
        name="cortex",
        group="main",
        goals="Serve the user. Delegate subtasks to subagents.",
    )


@pytest.fixture
def worker_config():
    return ActorConfig(
        name="researcher",
        group="main",
        goals="Research the topic and report findings.",
        tools=["web_search", "read_file"],
        max_turns=5,
    )


@pytest.fixture
def principal(registry, principal_config):
    return registry.spawn(principal_config, is_principal=True)


@pytest.fixture
def worker(registry, principal, worker_config):
    return registry.spawn(worker_config, spawned_by=principal.id)


# ── Basic Lifecycle ───────────────────────────────────────────


class TestActorLifecycle:
    def test_create_actor(self, principal):
        assert principal.state == ActorState.RUNNING
        assert principal.is_principal is True
        assert principal.config.name == "cortex"
        assert len(principal.id) == 8

    def test_create_worker(self, worker, principal):
        assert worker.state == ActorState.RUNNING
        assert worker.is_principal is False
        assert worker.spawned_by == principal.id
        assert worker.config.name == "researcher"

    def test_terminate(self, worker):
        worker.terminate("Task complete: found 3 papers")
        assert worker.state == ActorState.TERMINATED
        assert worker._result == "Task complete: found 3 papers"

    def test_double_terminate_is_safe(self, worker):
        """Terminating an already-terminated actor is a no-op."""
        worker.terminate("first")
        worker.terminate("second")  # Should not error
        assert worker._result == "first"  # First result preserved

    def test_actor_info(self, worker, principal):
        info = worker.info
        assert isinstance(info, ActorInfo)
        assert info.name == "researcher"
        assert info.group == "main"
        assert info.spawned_by == principal.id
        assert info.state == ActorState.RUNNING


# ── Registry ──────────────────────────────────────────────────


class TestActorRegistry:
    def test_spawn_principal(self, registry, principal_config):
        principal = registry.spawn(principal_config, is_principal=True)
        assert registry.get_principal() is principal
        assert registry.active_count == 1

    def test_spawn_multiple(self, registry, principal):
        w1 = registry.spawn(ActorConfig(name="w1", group="main", goals="task1"), spawned_by=principal.id)
        w2 = registry.spawn(ActorConfig(name="w2", group="main", goals="task2"), spawned_by=principal.id)
        assert registry.active_count == 3

    def test_discover_group(self, registry, principal, worker):
        actors = registry.discover("main")
        assert len(actors) == 2
        names = {a.name for a in actors}
        assert names == {"cortex", "researcher"}

    def test_discover_empty_group(self, registry, principal):
        actors = registry.discover("nonexistent")
        assert len(actors) == 0

    def test_discover_includes_recently_terminated(self, registry, principal, worker):
        worker.terminate("done")
        actors = registry.discover("main")
        assert len(actors) == 2  # Both visible — terminated kept for 1 hour
        terminated = [a for a in actors if a.state == ActorState.TERMINATED]
        assert len(terminated) == 1
        assert terminated[0].name == "researcher"

    def test_get_children(self, registry, principal):
        w1 = registry.spawn(ActorConfig(name="w1", group="main", goals="t1"), spawned_by=principal.id)
        w2 = registry.spawn(ActorConfig(name="w2", group="main", goals="t2"), spawned_by=principal.id)
        children = registry.get_children(principal.id)
        assert len(children) == 2

    def test_cleanup_stale_actors(self, registry, principal, worker):
        """Terminated actors stay for 1 hour, then get cleaned up."""
        worker.terminate("done")
        assert len(registry._actors) == 2
        
        # Regular cleanup: too recent, should NOT remove
        registry.cleanup_terminated()
        assert len(registry._actors) == 2
        assert registry.get(worker.id) is not None
        
        # Simulate aging past the threshold
        from datetime import timedelta
        worker.terminated_at -= timedelta(seconds=registry.STALE_SECONDS + 1)
        registry.cleanup_terminated()
        assert len(registry._actors) == 1
        assert registry.get(worker.id) is None
    
    def test_cleanup_force(self, registry, principal, worker):
        """Force cleanup removes all terminated immediately."""
        worker.terminate("done")
        registry.cleanup_terminated(force=True)
        assert len(registry._actors) == 1

    def test_find_by_name(self, registry, principal, worker):
        found = registry.find_by_name("researcher")
        assert found is worker
        
        found = registry.find_by_name("researcher", "main")
        assert found is worker
        
        found = registry.find_by_name("researcher", "other_group")
        assert found is None

    def test_find_by_name_skips_terminated(self, registry, principal, worker):
        worker.terminate("done")
        found = registry.find_by_name("researcher")
        assert found is None


# ── Messaging ─────────────────────────────────────────────────


class TestActorMessaging:
    @pytest.mark.asyncio
    async def test_send_message(self, principal, worker):
        await principal.send_to(worker.id, "Hello worker!")
        msg = await worker.wait_for_reply(timeout=1.0)
        assert msg is not None
        assert msg.content == "Hello worker!"
        assert msg.sender == principal.id

    @pytest.mark.asyncio
    async def test_bidirectional_messaging(self, principal, worker):
        await principal.send_to(worker.id, "Do the task")
        msg = await worker.wait_for_reply(timeout=1.0)
        assert msg.content == "Do the task"
        
        await worker.send_to(principal.id, "Task done!")
        reply = await principal.wait_for_reply(timeout=1.0)
        assert reply.content == "Task done!"

    @pytest.mark.asyncio
    async def test_sibling_messaging(self, registry, principal):
        """Siblings (same parent) can message each other."""
        w1 = registry.spawn(ActorConfig(name="w1", group="main", goals="t1"), spawned_by=principal.id)
        w2 = registry.spawn(ActorConfig(name="w2", group="main", goals="t2"), spawned_by=principal.id)
        
        assert w1.can_message(w2.id)
        await w1.send_to(w2.id, "Hey sibling!")
        msg = await w2.wait_for_reply(timeout=1.0)
        assert msg.content == "Hey sibling!"

    @pytest.mark.asyncio
    async def test_child_to_parent_messaging(self, principal, worker):
        """Child can message parent."""
        assert worker.can_message(principal.id)
        await worker.send_to(principal.id, "Reporting results")
        msg = await principal.wait_for_reply(timeout=1.0)
        assert msg.content == "Reporting results"

    @pytest.mark.asyncio
    async def test_unrelated_actors_cannot_message(self, registry):
        """Actors in different groups with no relationship cannot message."""
        a1 = registry.spawn(ActorConfig(name="a1", group="team_a", goals="t1"))
        a2 = registry.spawn(ActorConfig(name="a2", group="team_b", goals="t2"))
        
        assert not a1.can_message(a2.id)
        with pytest.raises(PermissionError):
            await a1.send_to(a2.id, "You shouldn't get this")

    @pytest.mark.asyncio
    async def test_principal_can_message_anyone(self, registry, principal):
        """Principal can message any actor regardless of group."""
        remote = registry.spawn(ActorConfig(name="remote", group="other", goals="t1"))
        assert principal.can_message(remote.id)

    @pytest.mark.asyncio
    async def test_wait_timeout(self, principal):
        msg = await principal.wait_for_reply(timeout=0.1)
        assert msg is None

    @pytest.mark.asyncio
    async def test_message_history(self, principal, worker):
        await principal.send_to(worker.id, "msg1")
        await worker.send_to(principal.id, "msg2")
        await principal.send_to(worker.id, "msg3")
        
        assert len(principal._messages) == 3
        assert len(worker._messages) == 3

    @pytest.mark.asyncio
    async def test_send_to_nonexistent(self, principal):
        with pytest.raises(ValueError, match="not found"):
            await principal.send_to("nonexistent", "hello")


# ── Parent Killing Children ───────────────────────────────────


class TestKillChild:
    def test_parent_can_kill_child(self, principal, worker):
        assert principal.kill_child(worker.id) is True
        assert worker.state == ActorState.TERMINATED
        assert "Killed by parent" in worker._result

    def test_cannot_kill_non_child(self, registry, principal):
        stranger = registry.spawn(ActorConfig(name="stranger", group="other", goals="t1"))
        assert principal.kill_child(stranger.id) is False
        assert stranger.state == ActorState.RUNNING

    def test_cannot_kill_already_terminated(self, principal, worker):
        worker.terminate("done naturally")
        assert principal.kill_child(worker.id) is False

    @pytest.mark.asyncio
    async def test_parent_notified_on_kill(self, registry, principal, worker):
        """When parent kills child, parent receives termination notice."""
        principal.kill_child(worker.id)
        await asyncio.sleep(0.05)
        msg = await principal.wait_for_reply(timeout=1.0)
        assert msg is not None
        assert "TERMINATED" in msg.content


# ── Termination Notification ──────────────────────────────────


class TestTerminationNotification:
    @pytest.mark.asyncio
    async def test_parent_notified_on_child_termination(self, registry, principal, worker):
        worker.terminate("Found 5 results")
        await asyncio.sleep(0.1)
        msg = await principal.wait_for_reply(timeout=1.0)
        assert msg is not None
        assert "TERMINATED" in msg.content
        assert "Found 5 results" in msg.content


# ── Actor Tools ───────────────────────────────────────────────


class TestActorTools:
    def test_principal_gets_spawn_and_kill_tools(self, principal, registry):
        tools = create_actor_tools(principal, registry)
        tool_names = {func.__name__ for func, _ in tools}
        assert "spawn_actor" in tool_names
        assert "kill_actor" in tool_names
        assert "send_message" in tool_names
        assert "discover_actors" in tool_names
        assert "terminate" in tool_names

    def test_all_actors_can_spawn(self, worker, registry):
        """All actors can spawn subagents (delegation at any level)."""
        tools = create_actor_tools(worker, registry)
        tool_names = {func.__name__ for func, _ in tools}
        assert "spawn_actor" in tool_names
        assert "send_message" in tool_names

    def test_worker_gets_restart_self(self, worker, registry):
        """Workers (non-principal) get restart_self tool."""
        tools = create_actor_tools(worker, registry)
        tool_names = {func.__name__ for func, _ in tools}
        assert "restart_self" in tool_names

    def test_principal_no_restart_self(self, principal, registry):
        """Principal doesn't get restart_self."""
        tools = create_actor_tools(principal, registry)
        tool_names = {func.__name__ for func, _ in tools}
        assert "restart_self" not in tool_names

    @pytest.mark.asyncio
    async def test_send_message_tool(self, principal, worker, registry):
        tools = create_actor_tools(principal, registry)
        send_fn = next(func for func, _ in tools if func.__name__ == "send_message")
        
        result = await send_fn(actor_id=worker.id, content="Hello from tool")
        assert "Message sent" in result
        
        msg = await worker.wait_for_reply(timeout=1.0)
        assert msg.content == "Hello from tool"

    @pytest.mark.asyncio
    async def test_send_message_to_terminated(self, principal, worker, registry):
        worker.terminate("done")
        tools = create_actor_tools(principal, registry)
        send_fn = next(func for func, _ in tools if func.__name__ == "send_message")
        
        result = await send_fn(actor_id=worker.id, content="Are you there?")
        assert "terminated" in result

    @pytest.mark.asyncio
    async def test_send_message_unrelated(self, registry):
        """send_message tool returns error for unrelated actors."""
        a1 = registry.spawn(ActorConfig(name="a1", group="g1", goals="t1"))
        a2 = registry.spawn(ActorConfig(name="a2", group="g2", goals="t2"))
        tools = create_actor_tools(a1, registry)
        send_fn = next(func for func, _ in tools if func.__name__ == "send_message")
        result = await send_fn(actor_id=a2.id, content="hello")
        assert "cannot message" in result.lower() or "error" in result.lower()

    def test_discover_tool(self, principal, worker, registry):
        tools = create_actor_tools(principal, registry)
        discover_fn = next(func for func, _ in tools if func.__name__ == "discover_actors")
        
        result = discover_fn()
        assert "researcher" in result
        assert "cortex" in result

    def test_discover_tool_shows_relationships(self, registry, principal, worker):
        """Discover shows [child], [sibling], [parent] labels."""
        tools = create_actor_tools(principal, registry)
        discover_fn = next(func for func, _ in tools if func.__name__ == "discover_actors")
        result = discover_fn()
        assert "[child]" in result

    def test_terminate_tool(self, worker, registry):
        tools = create_actor_tools(worker, registry)
        terminate_fn = next(func for func, _ in tools if func.__name__ == "terminate")
        
        result = terminate_fn(result="All done")
        assert "Terminated" in result
        assert worker.state == ActorState.TERMINATED

    @pytest.mark.asyncio
    async def test_spawn_actor_tool(self, principal, registry):
        tools = create_actor_tools(principal, registry)
        spawn_fn = next(func for func, _ in tools if func.__name__ == "spawn_actor")
        
        result = await spawn_fn(
            name="coder",
            goals="Write the implementation",
            tools="read_file,write_file",
        )
        assert "Spawned actor 'coder'" in result
        assert registry.active_count == 2

    @pytest.mark.asyncio
    async def test_spawn_duplicate_returns_existing(self, principal, worker, registry):
        """Spawning actor with same name returns existing instead of duplicating."""
        tools = create_actor_tools(principal, registry)
        spawn_fn = next(func for func, _ in tools if func.__name__ == "spawn_actor")
        
        result = await spawn_fn(name="researcher", goals="same task")
        assert "already exists" in result
        assert worker.id in result
        assert registry.active_count == 2  # No new actor created

    @pytest.mark.asyncio
    async def test_spawn_with_model_choice(self, principal, registry):
        """Butler can specify model for subagent."""
        tools = create_actor_tools(principal, registry)
        spawn_fn = next(func for func, _ in tools if func.__name__ == "spawn_actor")
        
        result = await spawn_fn(
            name="thinker",
            goals="Deep analysis",
            model="openrouter/moonshotai/kimi-k2.5",
        )
        assert "kimi-k2.5" in result
        thinker = registry.find_by_name("thinker")
        assert thinker.config.model == "openrouter/moonshotai/kimi-k2.5"

    def test_kill_actor_tool(self, principal, worker, registry):
        tools = create_actor_tools(principal, registry)
        kill_fn = next(func for func, _ in tools if func.__name__ == "kill_actor")
        
        result = kill_fn(actor_id=worker.id)
        assert "Killed" in result
        assert worker.state == ActorState.TERMINATED

    def test_kill_non_child_tool(self, registry, principal):
        stranger = registry.spawn(ActorConfig(name="stranger", group="other", goals="t"))
        tools = create_actor_tools(principal, registry)
        kill_fn = next(func for func, _ in tools if func.__name__ == "kill_actor")
        
        result = kill_fn(actor_id=stranger.id)
        assert "not your child" in result
        assert stranger.state == ActorState.RUNNING

    @pytest.mark.asyncio
    async def test_ping_actor_tool(self, principal, worker, registry):
        """Ping shows actor status."""
        tools = create_actor_tools(principal, registry)
        ping_fn = next(func for func, _ in tools if func.__name__ == "ping_actor")
        
        result = await ping_fn(actor_id=worker.id)
        assert "researcher" in result
        assert "running" in result
        assert worker.config.goals in result

    @pytest.mark.asyncio
    async def test_ping_terminated_actor(self, principal, worker, registry):
        worker.terminate("All done")
        tools = create_actor_tools(principal, registry)
        ping_fn = next(func for func, _ in tools if func.__name__ == "ping_actor")
        
        result = await ping_fn(actor_id=worker.id)
        assert "terminated" in result
        assert "All done" in result

    def test_restart_self_tool(self, worker, registry):
        """restart_self terminates and sends restart request to parent."""
        tools = create_actor_tools(worker, registry)
        restart_fn = next(func for func, _ in tools if func.__name__ == "restart_self")
        
        result = restart_fn(new_goals="Better goals here")
        assert "restart" in result.lower()
        assert worker.state == ActorState.TERMINATED
        assert "Restart requested" in worker._result


# ── System Prompt Building ────────────────────────────────────


class TestSystemPrompt:
    def test_principal_prompt(self, principal, worker):
        prompt = principal.build_system_prompt()
        assert "cortex" in prompt
        assert "ONLY actor" in prompt
        assert "quick tasks" in prompt.lower()  # Handle quick tasks directly
        assert "subagent" in prompt.lower()  # Spawn subagent for long tasks
        assert "spawn" in prompt.lower()
        assert "ping_actor" in prompt
        assert "kill_actor" in prompt

    def test_worker_prompt(self, worker, principal):
        prompt = worker.build_system_prompt()
        assert "subagent" in prompt
        assert "researcher" in prompt
        assert worker.config.goals in prompt
        assert "CANNOT talk to the user" in prompt
        assert "cortex" in prompt  # Parent name shown

    def test_group_awareness_in_prompt(self, principal, worker):
        prompt = worker.build_system_prompt()
        assert "cortex" in prompt
        assert "visible_actors" in prompt

    def test_relationship_labels_in_prompt(self, registry, principal, worker):
        """Prompt shows [child], [parent], [sibling] labels."""
        prompt = principal.build_system_prompt()
        assert "[child]" in prompt
        
        prompt = worker.build_system_prompt()
        assert "[parent]" in prompt

    @pytest.mark.asyncio
    async def test_inbox_in_prompt(self, principal, worker):
        await principal.send_to(worker.id, "Check the database")
        prompt = worker.build_system_prompt()
        assert "Check the database" in prompt
        assert "inbox" in prompt
        assert "cortex" in prompt  # Sender name shown


# ── Context Messages ──────────────────────────────────────────


class TestContextMessages:
    @pytest.mark.asyncio
    async def test_get_context_messages(self, principal, worker):
        await principal.send_to(worker.id, "Do task")
        await worker.send_to(principal.id, "Done")
        
        ctx = worker.get_context_messages()
        assert len(ctx) == 2
        assert ctx[0]["role"] == "user"
        assert "Do task" in ctx[0]["content"]
        assert ctx[1]["role"] == "assistant"
        assert "Done" in ctx[1]["content"]


# ── Runner ────────────────────────────────────────────────────


class TestActorRunner:
    @pytest.mark.asyncio
    async def test_runner_basic(self, registry, principal):
        worker = registry.spawn(
            ActorConfig(name="test_worker", group="main", goals="Say hello and terminate"),
            spawned_by=principal.id,
        )
        
        mock_llm = MagicMock()
        mock_llm.add_tool = MagicMock()
        mock_llm.context = MagicMock()
        
        call_count = 0
        async def fake_chat(message):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                worker.terminate("Task complete")
            return "Working..."
        mock_llm.chat = fake_chat
        
        async def mock_factory(actor):
            return mock_llm
        
        runner = ActorRunner(worker, registry, mock_factory)
        result = await runner.run()
        
        assert worker.state == ActorState.TERMINATED
        assert "Task complete" in result

    @pytest.mark.asyncio
    async def test_runner_max_turns(self, registry, principal):
        worker = registry.spawn(
            ActorConfig(name="slow_worker", group="main", goals="Take forever", max_turns=3),
            spawned_by=principal.id,
        )
        
        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(return_value="Still working...")
        mock_llm.add_tool = MagicMock()
        mock_llm.context = MagicMock()
        
        async def mock_factory(actor):
            return mock_llm
        
        runner = ActorRunner(worker, registry, mock_factory)
        result = await runner.run()
        
        assert worker.state == ActorState.TERMINATED
        assert "Max turns" in result


# ── Multi-Group Isolation ─────────────────────────────────────


class TestGroupIsolation:
    def test_groups_are_isolated(self, registry):
        a1 = registry.spawn(ActorConfig(name="a1", group="team_a", goals="task_a"))
        a2 = registry.spawn(ActorConfig(name="a2", group="team_b", goals="task_b"))
        
        group_a = registry.discover("team_a")
        group_b = registry.discover("team_b")
        
        assert len(group_a) == 1
        assert group_a[0].name == "a1"
        assert len(group_b) == 1
        assert group_b[0].name == "a2"


# ── Example Scenarios ─────────────────────────────────────────


class TestExampleScenarios:
    @pytest.mark.asyncio
    async def test_research_delegation(self, registry):
        butler = registry.spawn(
            ActorConfig(name="cortex", group="research", goals="Help user with research"),
            is_principal=True,
        )
        
        researcher = registry.spawn(
            ActorConfig(name="researcher", group="research", goals="Find papers about transformers",
                       tools=["web_search"]),
            spawned_by=butler.id,
        )
        
        await researcher.send_to(butler.id, "Found 5 papers on transformer architectures")
        researcher.terminate("Found 5 papers: Attention Is All You Need, ...")
        
        await asyncio.sleep(0.1)
        assert len(butler._messages) > 0
        assert researcher.state == ActorState.TERMINATED
        assert registry.active_count == 1

    @pytest.mark.asyncio
    async def test_multi_actor_collaboration(self, registry):
        butler = registry.spawn(
            ActorConfig(name="cortex", group="project", goals="Coordinate the project"),
            is_principal=True,
        )
        
        coder = registry.spawn(
            ActorConfig(name="coder", group="project", goals="Write the code"),
            spawned_by=butler.id,
        )
        reviewer = registry.spawn(
            ActorConfig(name="reviewer", group="project", goals="Review the code"),
            spawned_by=butler.id,
        )
        
        # All three can discover each other
        assert len(registry.discover("project")) == 3
        
        # Coder → reviewer (siblings)
        assert coder.can_message(reviewer.id)
        await coder.send_to(reviewer.id, "PR ready")
        msg = await reviewer.wait_for_reply(timeout=1.0)
        assert "PR ready" in msg.content
        
        # Reviewer → butler (parent)
        await reviewer.send_to(butler.id, "LGTM")
        reviewer.terminate("Review complete")
        coder.terminate("Code written")
        
        await asyncio.sleep(0.1)
        assert registry.active_count == 1

    @pytest.mark.asyncio
    async def test_butler_kills_stuck_worker(self, registry):
        """Butler spawns worker, kills it when stuck."""
        butler = registry.spawn(
            ActorConfig(name="cortex", group="main", goals="Manage"),
            is_principal=True,
        )
        worker = registry.spawn(
            ActorConfig(name="stuck_worker", group="main", goals="Hang forever"),
            spawned_by=butler.id,
        )
        
        assert registry.active_count == 2
        butler.kill_child(worker.id)
        assert worker.state == ActorState.TERMINATED
        assert registry.active_count == 1
