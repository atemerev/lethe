"""Tools available to actors for inter-actor communication and lifecycle management.

Butler (principal) tools: spawn, kill, send, discover, ping, wait, terminate
Subagent tools: send, discover, wait, terminate, restart_self, spawn (always)
"""

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lethe.actor import Actor, ActorRegistry

logger = logging.getLogger(__name__)


def create_actor_tools(actor: "Actor", registry: "ActorRegistry") -> list:
    """Create tool functions bound to a specific actor.
    
    Returns list of (function, needs_approval) tuples.
    """
    
    async def send_message(
        actor_id: str,
        content: str,
        reply_to: str = "",
        channel: str = "",
        kind: str = "",
    ) -> str:
        """Send a message to another actor (parent, sibling, child, or group member).
        
        Args:
            actor_id: ID of the actor to send to
            content: Message content
            reply_to: Optional message ID to reply to
            channel: Optional semantic channel (e.g. user_notify, task_update)
            kind: Optional subtype within the channel (e.g. info, warning, done, failed)
            
        Returns:
            Confirmation with message ID, or error
        """
        target = registry.get(actor_id)
        if target is None:
            return f"Error: actor {actor_id} not found. Use discover_actors() to find available actors."
        if target.state.value == "terminated":
            return f"Error: actor {actor_id} ({target.config.name}) is terminated."
        if not actor.can_message(actor_id):
            return f"Error: cannot message {actor_id} — not a parent, sibling, child, or group member."
        
        metadata = {}
        if channel.strip():
            metadata["channel"] = channel.strip()
        if kind.strip():
            metadata["kind"] = kind.strip()
        msg = await actor.send_to(
            actor_id,
            content,
            reply_to=reply_to or None,
            metadata=metadata or None,
        )
        return f"Message sent (id={msg.id}) to {target.config.name} ({actor_id})"

    async def wait_for_response(timeout: int = 120) -> str:
        """Wait for a message from another actor.
        
        Blocks until a message arrives or timeout. Use this after spawning
        a subagent or sending a message when you need the response.
        
        IMPORTANT: Use a generous timeout (120-300s) for subagents doing
        complex work. Do NOT call ping_actor in a loop — just wait here.
        The subagent will message you when done.
        
        Args:
            timeout: Seconds to wait (default 120, use 300 for complex tasks)
            
        Returns:
            The message content, or timeout notice
        """
        msg = await actor.wait_for_reply(timeout=float(timeout))
        if msg is None:
            return "Timed out waiting for response."
        sender = registry.get(msg.sender)
        sender_name = sender.config.name if sender else msg.sender
        return f"[From {sender_name}] {msg.content}"

    def discover_actors(group: str = "", include_terminated: bool = False) -> str:
        """Discover actors in a group.
        
        Args:
            group: Group name to search. Empty = same group as you.
            include_terminated: Include recently terminated actors (default False).
            
        Returns:
            List of actors with their IDs, names, goals, state, and relationship
        """
        search_group = group or actor.config.group
        actors = registry.discover(search_group) if include_terminated else registry.discover_active(search_group)
        if not actors:
            scope = " (including terminated)" if include_terminated else ""
            return f"No actors in group '{search_group}'{scope}."
        
        scope = " (including terminated)" if include_terminated else " (active only)"
        lines = [f"Actors in group '{search_group}'{scope}:"]
        for info in actors:
            marker = " (you)" if info.id == actor.id else ""
            relationship = ""
            if info.spawned_by == actor.id:
                relationship = " [child]"
            elif info.id == actor.spawned_by:
                relationship = " [parent]"
            elif info.spawned_by == actor.spawned_by and actor.spawned_by:
                relationship = " [sibling]"
            result_info = ""
            if info.state == ActorState.TERMINATED:
                target = registry.get(info.id)
                if target and target._result:
                    result_info = f" result: {target._result[:100]}"
            lines.append(
                f"  {info.name} (id={info.id}, state={info.state.value}, task={info.task_state.value})"
                f"{marker}{relationship}: {info.goals}{result_info}"
            )
        return "\n".join(lines)

    def discover_recently_finished(group: str = "", limit: int = 5) -> str:
        """Show recently completed actors and their results.

        Args:
            group: Group name to search. Empty = same group as you.
            limit: Max actors to show (default 5)

        Returns:
            Formatted list of recently terminated actors with summary results.
        """
        search_group = group or actor.config.group
        finished = registry.discover_recently_finished(search_group, limit=limit)
        if not finished:
            return f"No recently finished actors in group '{search_group}'."

        lines = [f"Recently finished in '{search_group}':"]
        for a in finished:
            result = (a._result or "no result").strip()
            if len(result) > 160:
                result = result[:160] + "...[truncated]"
            when = a.terminated_at.strftime("%H:%M:%S") if a.terminated_at else "unknown"
            lines.append(
                f"  {a.config.name} (id={a.id}, task={a.task_state.value}, at={when}): {result}"
            )
        return "\n".join(lines)

    def terminate(result: str = "") -> str:
        """Terminate this actor and report results.
        
        Call this when your task is complete. Include a summary of what
        you accomplished — this will be sent to the actor that spawned you.
        
        Args:
            result: Summary of what was accomplished
            
        Returns:
            Confirmation
        """
        actor.terminate(result)
        # Signal LLM client to stop — prevents wasted API call after terminate
        if hasattr(actor, '_llm') and actor._llm:
            actor._llm._stop_after_tool = True
        return "Terminated. Result sent to parent."

    # --- Tools available to ALL actors ---
    tools = [
        (send_message, False),
        (wait_for_response, False),
        (discover_actors, False),
        (discover_recently_finished, False),
        (terminate, False),
    ]

    # --- spawn_actor: available to ALL actors (subagents can delegate too) ---
    # Max concurrent active children per actor (prevent runaway spawning)
    MAX_ACTIVE_CHILDREN = 5

    async def spawn_actor(
        name: str,
        goals: str,
        group: str = "",
        tools: str = "",
        model: str = "",
        max_turns: int = 50,
    ) -> str:
        """Spawn a new subagent actor to handle a subtask.
        
        IMPORTANT: Before spawning, check if an existing actor can handle this.
        Use discover_actors() first to see who's already running.
        
        Args:
            name: Short name for the actor (e.g., "researcher", "coder")
            goals: Detailed description of what to accomplish. Include all context the subagent needs.
            group: Actor group for discovery (default: same as yours)
            tools: Comma-separated EXTRA tool names beyond the defaults. All subagents always get: bash, read_file, write_file, edit_file, list_directory, grep_search, view_image. Specify extras like: "web_search,fetch_webpage,browser_open" etc.
            model: LLM model override (empty = default aux model). Use main model for complex reasoning.
            max_turns: Max LLM turns before forced termination (default 50)
            
        Returns:
            Actor ID and confirmation, or existing actor info if duplicate
        """
        import re
        from lethe.actor import ActorConfig, ActorState
        
        ACTIVE_STATES = (ActorState.RUNNING, ActorState.INITIALIZING, ActorState.WAITING)
        target_group = group or actor.config.group
        
        # Normalize name: lowercase, strip whitespace, replace spaces/underscores with hyphens
        name = re.sub(r'[\s_]+', '-', name.strip().lower())
        
        # Get ALL active children
        children = registry.get_children(actor.id)
        active_children = [c for c in children if c.state in ACTIVE_STATES]
        
        def _format_children(children_list):
            if not children_list:
                return ""
            lines = [f"  - {c.config.name} (id={c.id}, state={c.state.value}): {c.config.goals[:80]}" for c in children_list]
            return f"\n\nActive children ({len(children_list)}):\n" + "\n".join(lines)
        
        # Check for existing actor with same name
        existing = registry.find_by_name(name, target_group)
        if existing and existing.state in ACTIVE_STATES:
            return (
                f"DUPLICATE BLOCKED: Actor '{name}' already exists (id={existing.id}, state={existing.state.value}).\n"
                f"Goals: {existing.config.goals}\n"
                f"Use send_message({existing.id}, ...) to communicate with it, or kill_actor({existing.id}) first."
                f"{_format_children(active_children)}"
            )
        
        # Check for similar goals among active children (fuzzy dedup)
        # Warn if spawning when children with overlapping goals exist
        goals_lower = goals.lower()
        similar = []
        for c in active_children:
            # Simple keyword overlap check
            child_words = set(c.config.goals.lower().split())
            goal_words = set(goals_lower.split())
            overlap = child_words & goal_words - {'the', 'a', 'an', 'to', 'and', 'or', 'in', 'for', 'of', 'is', 'it', 'on', 'at', 'by', 'with'}
            if len(overlap) >= 3:
                similar.append(c)
        
        if similar:
            lines = [f"  - {c.config.name} (id={c.id}): {c.config.goals[:80]}" for c in similar]
            return (
                f"WARNING: These active children have similar goals:\n" + "\n".join(lines) + "\n\n"
                f"If this is a duplicate, use send_message() to the existing actor instead.\n"
                f"If you still need a new actor, kill the old one first with kill_actor(), then spawn again."
                f"{_format_children(active_children)}"
            )
        
        # Enforce max concurrent children
        if len(active_children) >= MAX_ACTIVE_CHILDREN:
            return (
                f"TOO MANY CHILDREN: {len(active_children)} active (max {MAX_ACTIVE_CHILDREN}).\n"
                f"Kill or wait for existing actors before spawning new ones."
                f"{_format_children(active_children)}"
            )
        
        tool_list = [t.strip() for t in tools.split(",") if t.strip()] if tools else []
        
        config = ActorConfig(
            name=name,
            group=target_group,
            goals=goals,
            tools=tool_list,
            model=model,
            max_turns=max_turns,
        )
        
        child = registry.spawn(config, spawned_by=actor.id)
        
        model_info = f", model={model}" if model else ", model=aux (default)"
        # Refresh active children (now includes new child)
        active_children = [c for c in registry.get_children(actor.id) if c.state in ACTIVE_STATES]
        
        return (
            f"Spawned actor '{name}' (id={child.id}, group={target_group}{model_info}).\n"
            f"Goals: {goals[:200]}\n"
            f"Tools: default (bash, file I/O, grep, view_image){' + ' + ', '.join(tool_list) if tool_list else ''} + actor tools\n"
            f"It will work autonomously and message you when done."
            f"{_format_children(active_children)}"
        )
    
    tools.append((spawn_actor, False))

    # --- ping_actor: check what a child/group member is doing ---
    async def ping_actor(actor_id: str) -> str:
        """Ping an actor to check its status and progress.
        
        Args:
            actor_id: ID of the actor to ping
            
        Returns:
            Status report: state, turn count, recent messages
        """
        target = registry.get(actor_id)
        if target is None:
            return f"Error: actor {actor_id} not found."
        
        lines = [
            f"Actor: {target.config.name} (id={target.id})",
            f"State: {target.state.value}",
            f"Task: {target.task_state.value}",
            f"Goals: {target.config.goals}",
            f"Turns: {target._turns}/{target.config.max_turns}",
            f"Messages: {len(target._messages)}",
        ]
        
        if target.state == ActorState.TERMINATED:
            lines.append(f"Result: {target._result or 'none'}")
        
        # Show last 3 messages
        recent = target._messages[-3:]
        if recent:
            lines.append("Recent activity:")
            for m in recent:
                sender = registry.get(m.sender)
                sender_name = sender.config.name if sender else m.sender
                lines.append(f"  [{sender_name}]: {m.content[:100]}")
        
        return "\n".join(lines)
    
    tools.append((ping_actor, False))

    # --- kill_actor: parent can kill immediate children ---
    def kill_actor(actor_id: str) -> str:
        """Kill an immediate child actor.
        
        You can only kill actors that YOU spawned. This immediately
        terminates the actor and you'll receive a termination notice.
        
        Args:
            actor_id: ID of the child actor to kill
            
        Returns:
            Confirmation or error
        """
        target = registry.get(actor_id)
        if target is None:
            return f"Error: actor {actor_id} not found."
        if target.spawned_by != actor.id:
            return f"Error: {target.config.name} ({actor_id}) is not your child. You can only kill actors you spawned."
        if target.state.value == "terminated":
            return f"Actor {target.config.name} ({actor_id}) is already terminated."
        
        actor.kill_child(actor_id)
        return f"Killed actor {target.config.name} ({actor_id})."
    
    tools.append((kill_actor, False))

    def update_task_state(state: str, note: str = "") -> str:
        """Update your own task state checkpoint.

        Args:
            state: One of planned, running, blocked, done
            note: Optional note describing why

        Returns:
            Confirmation or error
        """
        ok, message = actor.set_task_state(state=state, note=note)
        return message if ok else f"Error: {message}"

    def get_task_state() -> str:
        """Get your current task state."""
        return f"Task state: {actor.task_state.value}"

    tools.append((update_task_state, False))
    tools.append((get_task_state, False))

    # --- restart_self: subagent can restart with a better prompt ---
    if not actor.is_principal:
        def restart_self(new_goals: str) -> str:
            """Restart yourself with a better prompt/goals.
            
            Use this if you realize your original goals were unclear or you need
            a different approach. This terminates you and asks your parent to
            respawn you with the new goals.
            
            Args:
                new_goals: The improved goals/prompt for the new instance
                
            Returns:
                Confirmation (this actor will terminate after this)
            """
            # Send restart request to parent
            parent = registry.get(actor.spawned_by)
            if parent and parent.state != ActorState.TERMINATED:
                from lethe.actor import ActorMessage
                msg = ActorMessage(
                    sender=actor.id,
                    recipient=actor.spawned_by,
                    content=(
                        f"[RESTART REQUEST] {actor.config.name} wants to restart with new goals:\n"
                        f"{new_goals}\n"
                        f"Tools needed: {','.join(actor.config.tools)}"
                    ),
                )
                try:
                    import asyncio
                    loop = asyncio.get_running_loop()
                    loop.create_task(parent.send(msg))
                except RuntimeError:
                    parent._messages.append(msg)
                    parent._inbox.put_nowait(msg)
            
            actor.terminate(f"Restart requested with new goals: {new_goals[:200]}")
            return "Terminating for restart. Parent will respawn with new goals."
        
        tools.append((restart_self, False))

    return tools


# Import for type checking in restart_self
from lethe.actor import ActorState
