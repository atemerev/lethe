"""Tools available to actors for inter-actor communication and lifecycle management.

These tools are registered with each actor's LLM client, giving the model
the ability to spawn subagents, communicate with other actors, discover
group members, and manage lifecycles.
"""

import json
import logging
from typing import List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from lethe.actor import Actor, ActorRegistry

logger = logging.getLogger(__name__)


def create_actor_tools(actor: "Actor", registry: "ActorRegistry") -> list:
    """Create tool functions bound to a specific actor.
    
    Returns list of (function, needs_approval) tuples.
    """
    
    async def send_message(actor_id: str, content: str, reply_to: str = "") -> str:
        """Send a message to another actor (parent, sibling, child, or group member).
        
        Args:
            actor_id: ID of the actor to send to
            content: Message content
            reply_to: Optional message ID to reply to
            
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
        
        msg = await actor.send_to(actor_id, content, reply_to=reply_to or None)
        return f"Message sent (id={msg.id}) to {target.config.name} ({actor_id})"

    async def wait_for_response(timeout: int = 60) -> str:
        """Wait for a message from another actor.
        
        Blocks until a message arrives or timeout. Use this after sending
        a message when you need the response before continuing.
        
        Args:
            timeout: Seconds to wait (default 60)
            
        Returns:
            The message content, or timeout notice
        """
        msg = await actor.wait_for_reply(timeout=float(timeout))
        if msg is None:
            return "Timed out waiting for response."
        sender = registry.get(msg.sender)
        sender_name = sender.config.name if sender else msg.sender
        return f"[From {sender_name}] {msg.content}"

    def discover_actors(group: str = "") -> str:
        """Discover actors in a group.
        
        Args:
            group: Group name to search. Empty = same group as you.
            
        Returns:
            List of actors with their IDs, names, goals, and state
        """
        search_group = group or actor.config.group
        actors = registry.discover(search_group)
        if not actors:
            return f"No active actors in group '{search_group}'."
        
        lines = [f"Actors in group '{search_group}':"]
        for info in actors:
            marker = " (you)" if info.id == actor.id else ""
            relationship = ""
            if info.spawned_by == actor.id:
                relationship = " [child]"
            elif info.id == actor.spawned_by:
                relationship = " [parent]"
            elif info.spawned_by == actor.spawned_by and actor.spawned_by:
                relationship = " [sibling]"
            lines.append(f"  {info.name} (id={info.id}, state={info.state.value}){marker}{relationship}: {info.goals}")
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
        return f"Terminated. Result sent to parent."

    # Tools available to all actors
    tools = [
        (send_message, False),
        (wait_for_response, False),
        (discover_actors, False),
        (terminate, False),
    ]

    # Principal and actors with explicit spawn permission
    if actor.is_principal or "spawn" in actor.config.tools:
        async def spawn_subagent(
            name: str,
            goals: str,
            group: str = "",
            tools: str = "",
            model: str = "",
            max_turns: int = 20,
        ) -> str:
            """Spawn a new subagent actor to handle a subtask.
            
            Before spawning, checks if an actor with this name already exists
            and is still running — returns the existing one instead of duplicating.
            
            The spawner chooses the model. Leave empty for the default aux model,
            or specify a model name (e.g., "openrouter/moonshotai/kimi-k2.5" for
            complex tasks that need the main model).
            
            Args:
                name: Short name for the actor (e.g., "researcher", "coder")
                goals: What this actor should accomplish (be specific)
                group: Actor group for discovery (default: same as yours)
                tools: Comma-separated tool names available to this actor (e.g., "read_file,bash,web_search")
                model: LLM model to use (empty = default aux model, or specify for complex tasks)
                max_turns: Max LLM turns before forced termination
                
            Returns:
                Actor ID and confirmation, or existing actor info if duplicate
            """
            from lethe.actor import ActorConfig
            
            target_group = group or actor.config.group
            
            # Check for existing actor with same name
            existing = registry.find_by_name(name, target_group)
            if existing:
                return (
                    f"Actor '{name}' already exists (id={existing.id}, state={existing.state.value}).\n"
                    f"Goals: {existing.config.goals}\n"
                    f"Use send_message({existing.id}, ...) to communicate with it."
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
            return (
                f"Spawned actor '{name}' (id={child.id}, group={target_group}{model_info}).\n"
                f"Goals: {goals}\n"
                f"Tools: {', '.join(tool_list) if tool_list else 'actor tools only'}\n"
                f"It will work autonomously and message you when done."
            )
        
        tools.append((spawn_subagent, False))

    # Parent can kill immediate children
    if actor.is_principal or actor.config.tools:  # Any actor with children can kill them
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

    return tools
