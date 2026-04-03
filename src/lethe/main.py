"""Main entry point for Lethe."""

import asyncio
import json
import logging
import os
import signal
import sys
from typing import Optional

# Load .env file before anything else
from dotenv import load_dotenv
load_dotenv()

from rich.console import Console
from rich.logging import RichHandler

from lethe.agent import Agent
from lethe.config import get_settings
from lethe.conversation import ConversationManager
from lethe.telegram import TelegramBot
from lethe.drives import DriveSystem
from lethe.cognition import CognitionLoop
from lethe.relationships import RelationshipManager
from lethe.experiments import ExperimentRunner
from lethe.tension import TensionRegistry
from lethe import console as lethe_console

console = Console()


def setup_logging(verbose: bool = False):
    """Configure logging with rich output."""
    level = logging.DEBUG if verbose else logging.INFO

    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True, console=console)],
    )

    # Reduce noise from libraries
    logging.getLogger("aiogram").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("sentence_transformers").setLevel(logging.WARNING)


async def run():
    """Run the Lethe application."""
    logger = logging.getLogger(__name__)

    try:
        settings = get_settings()
    except Exception as e:
        console.print(f"[red]Configuration error:[/red] {e}")
        console.print("\nMake sure you have a .env file with TELEGRAM_BOT_TOKEN set.")
        console.print("Also ensure OPENROUTER_API_KEY is set in your environment.")
        sys.exit(1)

    console.print("[bold blue]Lethe[/bold blue] - Autonomous Living Entity")
    console.print(f"Model: {settings.llm_model}")
    console.print(f"Memory: {settings.memory_dir}")
    console.print()

    # Initialize agent (tools auto-loaded)
    console.print("[dim]Initializing agent...[/dim]")
    agent = Agent(settings)
    await agent.initialize()  # Async init: load history with summarization
    agent.refresh_memory_context()

    # Initialize drive system
    workspace_dir = str(settings.workspace_dir)
    drives = DriveSystem()
    drives_state_path = os.path.join(workspace_dir, "drives_state.json")
    drives.load(drives_state_path)
    console.print(f"[cyan]Drives[/cyan] initialized (dominant: {drives.dominant()})")

    # Initialize tension registry (unresolved business drives initiative)
    tension = TensionRegistry(workspace_dir)
    above = tension.get_above_threshold()
    if above:
        console.print(f"[cyan]Tensions[/cyan] {len(above)} items above threshold")

    # Initialize relationship manager (unified memory, social wisdom for privacy)
    relationships = RelationshipManager(workspace_dir)
    rel_count = len(relationships.get_all())
    console.print(f"[cyan]Relationships[/cyan] initialized ({rel_count} known people)")

    # Initialize experiment runner
    experiments = ExperimentRunner(workspace_dir)
    active_count = len(experiments.get_active())
    if active_count:
        console.print(f"[cyan]Experiments[/cyan] {active_count} active")

    # Initialize actor system (subagent support)
    actor_system = None
    if os.environ.get("ACTORS_ENABLED", "true").lower() == "true":
        from lethe.actor.integration import ActorSystem
        actor_system = ActorSystem(agent, settings=settings)
        await actor_system.setup()
        console.print("[cyan]Actor system[/cyan] initialized (brainstem + cortex + DMN)")

    stats = agent.get_stats()
    console.print(f"[green]Entity ready[/green] - {stats['memory_blocks']} blocks, {stats['archival_memories']} memories")

    # Wire autonomy context into agent (drives, tensions, relationships, deep identity, experiments)
    from lethe.deep_identity import get_inclination_hints

    # Hard cap: autonomy context should never exceed ~600 tokens (~2400 chars)
    AUTONOMY_CONTEXT_MAX_CHARS = 2400

    def build_autonomy_context() -> str:
        """Build per-turn context from autonomy systems. Budget-capped."""
        parts = []

        # Deep identity — vague inclination hints (capped at 500 chars internally)
        hints = get_inclination_hints(workspace_dir)
        if hints:
            parts.append(hints)

        # Drive state — compact summary (~200 chars)
        parts.append(drives.get_state_summary())

        # Tension registry — top items only (~300 chars)
        tension_summary = tension.get_summary()
        if tension_summary and "No unresolved" not in tension_summary:
            parts.append(tension_summary)

        # Relationships — who the entity knows (~200 chars)
        rel_summary = relationships.get_summary()
        if rel_summary and "No relationships" not in rel_summary:
            parts.append(rel_summary)

        # Active relationship notes for current dialog (capped at 500 chars)
        active_rel = relationships.get_active_context()
        if active_rel:
            notes_path = active_rel.notes_path(workspace_dir)
            if os.path.exists(notes_path):
                try:
                    with open(notes_path, "r") as f:
                        notes = f.read().strip()
                    if notes and len(notes) > 20:
                        parts.append(f"Relationship notes ({active_rel.display_name}):\n{notes[:500]}")
                except Exception:
                    pass

        # Active experiments (~200 chars)
        exp_summary = experiments.get_summary()
        if exp_summary and "No active" not in exp_summary:
            parts.append(exp_summary)

        result = "\n\n".join(parts) if parts else ""

        # Hard cap — truncate at last complete section if over budget
        if len(result) > AUTONOMY_CONTEXT_MAX_CHARS:
            result = result[:AUTONOMY_CONTEXT_MAX_CHARS].rsplit("\n\n", 1)[0]

        return result

    agent._autonomy_context_provider = build_autonomy_context

    # Initialize console (mind state visualization) if enabled
    console_enabled = os.environ.get("LETHE_CONSOLE", "false").lower() == "true"
    console_port = int(os.environ.get("LETHE_CONSOLE_PORT", 8777))
    console_host = os.environ.get("LETHE_CONSOLE_HOST", "127.0.0.1")

    if console_enabled:
        from lethe.console.ui import run_console
        await run_console(port=console_port, host=console_host)
        console.print(f"[cyan]Console[/cyan] running at http://{console_host}:{console_port}")
        
        # Initialize console state with current data
        lethe_console.update_stats(stats['total_messages'], stats['archival_memories'])
        
        # Load identity
        identity_block = agent.memory.blocks.get("identity")
        lethe_console.update_identity(identity_block.get("value", "") if identity_block else "")
        
        # Load all memory blocks
        all_blocks = agent.memory.blocks.list_blocks()
        lethe_console.update_memory_blocks(all_blocks)
        
        # Load recent messages from context
        lethe_console.update_messages(agent.llm.context.messages)
        
        # Load summary if available
        if agent.llm.context.summary:
            lethe_console.update_summary(agent.llm.context.summary)
        
        # Capture initial context (what would be sent to LLM)
        initial_context = agent.llm.context.build_messages()
        token_estimate = agent.llm.context.count_tokens(str(initial_context))
        lethe_console.update_context(initial_context, token_estimate)
        
        # Model info
        lethe_console.update_model_info(settings.llm_model, settings.llm_model_aux)
        
        # Hook into agent for state updates
        agent.set_console_hooks(
            on_context_build=lambda ctx, tokens: lethe_console.update_context(ctx, tokens),
            on_status_change=lambda status, tool: lethe_console.update_status(status, tool),
            on_memory_change=lambda blocks: lethe_console.update_memory_blocks(blocks),
            on_token_usage=None,
        )

    # Initialize conversation manager
    conversation_manager = ConversationManager(debounce_seconds=settings.debounce_seconds)
    logger.info(f"Conversation manager initialized (debounce: {settings.debounce_seconds}s)")

    def mark_user_visible_activity(reason: str) -> None:
        """Note user-visible activity for logging."""
        logger.debug("Activity: %s", reason)

    # Resolve default chat_id for proactive messaging
    allowed_ids = settings.telegram_allowed_user_ids
    default_chat_id = int(allowed_ids.split(",")[0]) if allowed_ids else None

    # Message processing callback
    async def process_message(chat_id: int, user_id: int, message: str, metadata: dict, interrupt_check):
        """Process a message from Telegram."""
        from lethe.tools import set_telegram_context, set_last_message_id, clear_telegram_context

        logger.info(f"Processing message from {user_id}: {message[:50]}...")
        mark_user_visible_activity("incoming user message")

        # Track relationship and fire drive events
        display_name = metadata.get("display_name", "")
        relationships.get_or_create(str(user_id), chat_id, display_name)
        relationships.set_active(str(user_id))
        relationships.record_interaction(str(user_id))
        drives.on_event("message_received", {"user_id": str(user_id)})

        # Set telegram context for tools (reactions, sending messages)
        set_telegram_context(telegram_bot.bot, chat_id)
        if metadata.get("message_id"):
            set_last_message_id(metadata["message_id"])

        # Start typing indicator
        await telegram_bot.start_typing(chat_id)

        try:
            async def on_intermediate(content: str):
                if not content or len(content) < 10:
                    return
                if interrupt_check():
                    return
                await telegram_bot.send_message(chat_id, content)
                mark_user_visible_activity("intermediate assistant update")

            async def on_image(image_path: str):
                if interrupt_check():
                    return
                await telegram_bot.send_photo(chat_id, image_path)
                mark_user_visible_activity("assistant image update")

            response = await agent.chat(message, on_message=on_intermediate, on_image=on_image)

            if interrupt_check():
                logger.info("Processing interrupted")
                return

            logger.info(f"Sending response ({len(response)} chars): {response[:80]}...")
            await telegram_bot.send_message(chat_id, response)
            mark_user_visible_activity("assistant final response")
            drives.on_event("message_sent", {"user_id": str(user_id)})

        except Exception as e:
            logger.exception(f"Error processing message: {e}")
            await telegram_bot.send_message(chat_id, f"Error: {e}")
        finally:
            await telegram_bot.stop_typing(chat_id)
            clear_telegram_context()
            relationships.clear_active()

    # Initialize Telegram bot
    telegram_bot = TelegramBot(
        settings,
        conversation_manager=conversation_manager,
        process_callback=process_message,
    )
    telegram_bot.agent = agent  # For /model, /aux commands

    # Wire actor system into telegram bot for /status command
    if actor_system:
        telegram_bot.actor_system = actor_system

    # --- Proactive messaging (used by cognition loop and actor system) ---

    async def send_to_user(response: str):
        """Send a proactive message to a user."""
        if default_chat_id:
            await telegram_bot.send_message(default_chat_id, response)
            mark_user_visible_activity("proactive outbound message")

    async def run_cortex_turn(synthetic_message: str):
        """Trigger a full cortex LLM turn with a synthetic system message."""
        if not default_chat_id:
            logger.warning("run_cortex_turn: no chat_id configured")
            return
        from lethe.tools import set_telegram_context, clear_telegram_context
        set_telegram_context(telegram_bot.bot, default_chat_id)
        try:
            await telegram_bot.start_typing(default_chat_id)
            response = await agent.chat(synthetic_message)
            if response and response.strip():
                await telegram_bot.send_message(default_chat_id, response)
                mark_user_visible_activity("cortex followup")
        except Exception as e:
            logger.exception("run_cortex_turn failed: %s", e)
        finally:
            await telegram_bot.stop_typing(default_chat_id)
            clear_telegram_context()

    async def get_active_reminders() -> str:
        """Get active reminders as formatted string."""
        from lethe.todos import TodoManager
        todo_manager = TodoManager(settings.db_path)
        todos = await todo_manager.list(status="pending")
        if not todos:
            return ""
        lines = []
        for todo in todos[:10]:
            priority = todo.get("priority", "normal")
            due = todo.get("due_at", "")
            due_str = f" (due: {due})" if due else ""
            lines.append(f"- [{priority}] {todo['title']}{due_str}")
        return "\n".join(lines)

    # Wire actor system callbacks
    if actor_system:
        actor_system.set_callbacks(
            send_to_user=send_to_user,
            get_reminders=get_active_reminders,
            run_cortex_turn=run_cortex_turn,
        )

    # Console monitoring pump for dynamic runtime subsystems.
    console_monitor_task = None
    if console_enabled:
        async def monitor_console_state():
            while True:
                try:
                    stats = agent.get_stats()
                    lethe_console.update_stats(stats['total_messages'], stats['archival_memories'])
                    lethe_console.update_messages(agent.llm.context.messages)
                    lethe_console.update_summary(agent.llm.context.summary or "")
                    lethe_console.update_hippocampus(agent.hippocampus.get_stats())
                    lethe_console.update_hippocampus_context(agent.hippocampus.get_context_view())
                    if actor_system:
                        lethe_console.update_actor_status(actor_system.status)
                        if actor_system.brainstem:
                            lethe_console.update_stem_context(actor_system.brainstem.get_context_view())
                        if actor_system.dmn:
                            lethe_console.update_dmn_context(actor_system.dmn.get_context_view())
                        # Amygdala removed: salience stats now in hippocampus context view
                        if actor_system.consolidation:
                            lethe_console.update_consolidation_context(actor_system.consolidation.get_context_view())
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"Console monitor update failed: {e}")
                await asyncio.sleep(2.0)

        console_monitor_task = asyncio.create_task(
            monitor_console_state(),
            name="console-monitor",
        )

    # Set up shutdown handling
    shutdown_event = asyncio.Event()

    def signal_handler():
        logger.info("Received shutdown signal...")
        shutdown_event.set()
        # Force exit after 3 seconds using a thread (not event loop)
        # This ensures exit even if event loop is blocked
        import threading
        def force_exit():
            import time
            time.sleep(3)
            logger.warning("Graceful shutdown timed out, forcing exit")
            os._exit(0)
        threading.Thread(target=force_exit, daemon=True).start()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)

    # --- Cognition loop: the sole background system ---

    async def cognition_think(topic: str) -> str:
        """Think/reflect — runs brainstem supervision + DMN + consolidation."""
        if actor_system:
            await actor_system.brainstem_heartbeat("")
            result = await actor_system.background_round()
            drives.on_event("reflection_done")
            tension.tick(elapsed_hours=drives.get_rest_interval() / 3600.0)
            return result or "reflected"
        return await agent.chat(
            f"[Internal reflection: {topic}]", use_hippocampus=False,
        )

    async def cognition_respond(user_id: str, chat_id_unused: int, detail: str) -> str:
        """Respond to a pending message (handled by normal telegram flow)."""
        drives.on_event("message_sent")
        return "response handled by conversation manager"

    async def cognition_message(user_id: str, chat_id_unused: int, detail: str) -> str:
        """Proactively reach out to someone."""
        rel = relationships.get(user_id)
        if not rel:
            candidates = relationships.get_candidates_for_social(max_count=1)
            rel = candidates[0] if candidates else None
        if not rel or not rel.chat_id:
            return "no one to message"
        # Trigger a cortex turn to compose and send the message
        await run_cortex_turn(
            f"[Your social drive prompted you to reach out to {rel.display_name}. "
            f"Context: {detail}. Say something genuine — or decide not to.]"
        )
        drives.on_event("message_sent")
        relationships.record_interaction(rel.user_id)
        return f"reached out to {rel.display_name}"

    async def cognition_consolidate() -> str:
        """Consolidate memory — runs DMN with creative reinterpretation."""
        return await cognition_think("consolidate memory, update deep identity")

    cognition = CognitionLoop(
        drives=drives,
        on_think=cognition_think,
        on_respond=cognition_respond,
        on_message=cognition_message,
        on_consolidate=cognition_consolidate,
        get_reminders=get_active_reminders,
        get_tensions_above_threshold=tension.get_above_threshold,
        drives_state_path=drives_state_path,
    )

    # /heartbeat command triggers a cognition cycle manually
    async def manual_trigger():
        """Manual trigger — runs one cognition think cycle."""
        await cognition_think("manual trigger")
    telegram_bot.heartbeat_callback = manual_trigger

    # Start services
    console.print("[green]Starting services...[/green]")

    bot_task = asyncio.create_task(telegram_bot.start())
    cognition_task = asyncio.create_task(cognition.run())

    try:
        await shutdown_event.wait()
    except asyncio.CancelledError:
        pass
    finally:
        console.print("\n[yellow]Shutting down...[/yellow]")

        try:
            async with asyncio.timeout(5):
                if console_monitor_task:
                    console_monitor_task.cancel()
                    try:
                        await console_monitor_task
                    except asyncio.CancelledError:
                        pass
                cognition.stop()
                if actor_system:
                    await actor_system.shutdown()
                await telegram_bot.stop()
                await agent.close()
        except asyncio.TimeoutError:
            logger.warning("Shutdown timed out, forcing exit")
            os._exit(0)

        bot_task.cancel()
        cognition_task.cancel()
        for task in (bot_task, cognition_task):
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Persist final state
        drives.persist(drives_state_path)
        tension.save()
        console.print("[green]Shutdown complete.[/green]")


async def run_api(port: int = 8080):
    """Run Lethe in HTTP API mode for gateway architecture."""
    logger = logging.getLogger(__name__)

    try:
        settings = get_settings()
    except Exception as e:
        console.print(f"[red]Configuration error:[/red] {e}")
        sys.exit(1)

    console.print("[bold blue]Lethe[/bold blue] - API Mode")
    console.print(f"Model: {settings.llm_model}")
    console.print(f"Memory: {settings.memory_dir}")
    console.print()

    # Initialize agent
    console.print("[dim]Initializing agent...[/dim]")
    agent = Agent(settings)
    await agent.initialize()
    agent.refresh_memory_context()

    # Initialize actor system
    actor_system = None
    if os.environ.get("ACTORS_ENABLED", "true").lower() == "true":
        from lethe.actor.integration import ActorSystem
        actor_system = ActorSystem(agent, settings=settings)
        await actor_system.setup()
        console.print("[cyan]Actor system[/cyan] initialized")

    stats = agent.get_stats()
    console.print(f"[green]Agent ready[/green] - {stats['memory_blocks']} blocks, {stats['archival_memories']} memories")

    # Initialize conversation manager
    conversation_manager = ConversationManager(debounce_seconds=settings.debounce_seconds)

    # Set up the API module globals
    from lethe import api as api_module
    api_module._agent = agent
    api_module._conversation_manager = conversation_manager
    api_module._actor_system = actor_system
    api_module._settings = settings

    # Proactive messaging for API mode
    async def send_to_user(response: str):
        await api_module.send_proactive(response)

    async def get_active_reminders() -> str:
        from lethe.todos import TodoManager
        todo_manager = TodoManager(settings.db_path)
        todos = await todo_manager.list(status="pending")
        if not todos:
            return ""
        lines = []
        for todo in todos[:10]:
            priority = todo.get("priority", "normal")
            due = todo.get("due_at", "")
            due_str = f" (due: {due})" if due else ""
            lines.append(f"- [{priority}] {todo['title']}{due_str}")
        return "\n".join(lines)

    # Wire actor system callbacks
    if actor_system:
        async def run_cortex_turn(synthetic_message: str):
            from lethe.tools import set_telegram_context, clear_telegram_context
            from lethe.proxy_bot import ProxyBot
            proxy = ProxyBot(api_module._proactive_queue)
            set_telegram_context(proxy, 0)
            try:
                response = await agent.chat(synthetic_message)
                if response and response.strip():
                    await api_module.send_proactive(response)
            except Exception as e:
                logger.exception("run_cortex_turn failed: %s", e)
            finally:
                clear_telegram_context()

        actor_system.set_callbacks(
            send_to_user=send_to_user,
            get_reminders=get_active_reminders,
            run_cortex_turn=run_cortex_turn,
        )

    # Cognition loop for API mode
    workspace_dir = str(settings.workspace_dir)
    drives = DriveSystem()
    drives.load(os.path.join(workspace_dir, "drives_state.json"))

    async def api_cognition_think(topic: str) -> str:
        if actor_system:
            await actor_system.brainstem_heartbeat("")
            result = await actor_system.background_round()
            return result or "reflected"
        return "no actor system"

    cognition = CognitionLoop(
        drives=drives,
        on_think=api_cognition_think,
        on_consolidate=api_cognition_think,
        get_reminders=get_active_reminders,
        drives_state_path=os.path.join(workspace_dir, "drives_state.json"),
    )
    api_module._cognition = cognition

    # Set up shutdown handling
    shutdown_event = asyncio.Event()

    def signal_handler():
        logger.info("Received shutdown signal...")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)

    # Start uvicorn
    import uvicorn
    config = uvicorn.Config(
        api_module.app,
        host="0.0.0.0",
        port=port,
        log_level="info",
    )
    server = uvicorn.Server(config)

    console.print(f"[green]API server starting on port {port}[/green]")

    cognition_task = asyncio.create_task(cognition.run())
    server_task = asyncio.create_task(server.serve())

    try:
        await shutdown_event.wait()
    except asyncio.CancelledError:
        pass
    finally:
        console.print("\n[yellow]Shutting down...[/yellow]")
        server.should_exit = True
        try:
            async with asyncio.timeout(5):
                cognition.stop()
                if actor_system:
                    await actor_system.shutdown()
                await agent.close()
        except asyncio.TimeoutError:
            logger.warning("Shutdown timed out, forcing exit")
            os._exit(0)

        cognition_task.cancel()
        server_task.cancel()
        for t in (cognition_task, server_task):
            try:
                await t
            except asyncio.CancelledError:
                pass

        console.print("[green]Shutdown complete.[/green]")


def main():
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Lethe - Autonomous AI Assistant")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging")
    parser.add_argument("--api", action="store_true", help="Run in HTTP API mode (for gateway)")
    parser.add_argument("--api-port", type=int, default=8080, help="HTTP API port (default: 8080)")

    subparsers = parser.add_subparsers(dest="command")
    oauth_parser = subparsers.add_parser(
        "oauth-login",
        help="Login with OAuth (anthropic or openai)",
    )
    oauth_parser.add_argument(
        "provider",
        nargs="?",
        choices=["anthropic", "openai"],
        default="anthropic",
        help="OAuth provider (default: anthropic)",
    )

    args = parser.parse_args()

    # Handle subcommands
    if args.command == "oauth-login":
        from lethe.tools.oauth_login import run_oauth_login
        run_oauth_login(args.provider)
        return

    setup_logging(verbose=args.verbose)

    # Check for API mode (CLI flag or env var)
    api_mode = args.api or os.environ.get("LETHE_MODE", "").lower() == "api"

    try:
        if api_mode:
            asyncio.run(run_api(port=args.api_port))
        else:
            asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
