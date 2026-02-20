"""Main entry point for Lethe."""

import asyncio
import logging
import os
import signal
import sys

# Load .env file before anything else
from dotenv import load_dotenv
load_dotenv()

from rich.console import Console
from rich.logging import RichHandler

from lethe.agent import Agent
from lethe.config import get_settings
from lethe.conversation import ConversationManager
from lethe.telegram import TelegramBot
from lethe.heartbeat import Heartbeat
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

    console.print("[bold blue]Lethe[/bold blue] - Autonomous AI Assistant")
    console.print(f"Model: {settings.llm_model}")
    console.print(f"Memory: {settings.memory_dir}")
    console.print()
    
    # Initialize agent (tools auto-loaded)
    console.print("[dim]Initializing agent...[/dim]")
    agent = Agent(settings)
    await agent.initialize()  # Async init: load history with summarization
    agent.refresh_memory_context()
    
    # Initialize actor system (subagent support)
    actor_system = None
    if os.environ.get("ACTORS_ENABLED", "true").lower() == "true":
        from lethe.actor.integration import ActorSystem
        actor_system = ActorSystem(agent, settings=settings)
        await actor_system.setup()
        console.print("[cyan]Actor system[/cyan] initialized (brainstem + cortex + DMN + Amygdala)")
    
    stats = agent.get_stats()
    console.print(f"[green]Agent ready[/green] - {stats['memory_blocks']} blocks, {stats['archival_memories']} memories")

    # Initialize console (mind state visualization) if enabled
    console_enabled = os.environ.get("LETHE_CONSOLE", "false").lower() == "true"
    console_port = int(os.environ.get("LETHE_CONSOLE_PORT", 8080))
    
    if console_enabled:
        from lethe.console.ui import run_console
        await run_console(port=console_port)
        console.print(f"[cyan]Console[/cyan] running at http://localhost:{console_port}")
        
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

    # Message processing callback
    async def process_message(chat_id: int, user_id: int, message: str, metadata: dict, interrupt_check):
        """Process a message from Telegram."""
        from lethe.tools import set_telegram_context, set_last_message_id, clear_telegram_context
        
        logger.info(f"Processing message from {user_id}: {message[:50]}...")
        
        # Set telegram context for tools (reactions, sending messages)
        set_telegram_context(telegram_bot.bot, chat_id)
        if metadata.get("message_id"):
            set_last_message_id(metadata["message_id"])
        
        # Start typing indicator
        await telegram_bot.start_typing(chat_id)
        
        try:
            # Callback for intermediate messages (reasoning/thinking)
            async def on_intermediate(content: str):
                """Send intermediate updates while agent is working."""
                if not content or len(content) < 10:
                    return
                # Check for interrupt before sending
                if interrupt_check():
                    return
                # Send thinking/reasoning as-is (no emoji prefix)
                await telegram_bot.send_message(chat_id, content)
            
            # Callback for image attachments (screenshots, etc.)
            async def on_image(image_path: str):
                """Send image to user."""
                if interrupt_check():
                    return
                await telegram_bot.send_photo(chat_id, image_path)
            
            # Get response from agent
            response = await agent.chat(message, on_message=on_intermediate, on_image=on_image)
            
            # Check for interrupt
            if interrupt_check():
                logger.info("Processing interrupted")
                return
            
            # Send response
            logger.info(f"Sending response ({len(response)} chars): {response[:80]}...")
            await telegram_bot.send_message(chat_id, response)
            
        except Exception as e:
            logger.exception(f"Error processing message: {e}")
            await telegram_bot.send_message(chat_id, f"Error: {e}")
        finally:
            await telegram_bot.stop_typing(chat_id)
            clear_telegram_context()

    # Initialize Telegram bot
    telegram_bot = TelegramBot(
        settings,
        conversation_manager=conversation_manager,
        process_callback=process_message,
    )
    # heartbeat_callback will be set below after Heartbeat is created

    # Initialize heartbeat
    heartbeat_interval = int(os.environ.get("HEARTBEAT_INTERVAL", 15 * 60))  # Default 15 min
    heartbeat_enabled = os.environ.get("HEARTBEAT_ENABLED", "true").lower() == "true"
    
    # Get the first allowed user ID for heartbeat messages
    allowed_ids = settings.telegram_allowed_user_ids
    heartbeat_chat_id = int(allowed_ids.split(",")[0]) if allowed_ids else None
    
    async def heartbeat_process(message: str) -> str:
        """Process heartbeat — triggers background rounds if actor system is active."""
        if actor_system:
            await actor_system.brainstem_heartbeat(message)
            result = await actor_system.background_round()
            return result or "ok"
        return await agent.heartbeat(message)
    
    async def heartbeat_full_context(message: str) -> str:
        """Full context heartbeat — triggers supervision + background rounds."""
        if actor_system:
            await actor_system.brainstem_heartbeat(message)
            result = await actor_system.background_round()
            return result or "ok"
        return await agent.chat(message, use_hippocampus=False)
    
    async def heartbeat_send(response: str):
        """Send heartbeat response to user."""
        if heartbeat_chat_id:
            await telegram_bot.send_message(heartbeat_chat_id, response)
    
    async def heartbeat_summarize(prompt: str) -> str:
        """Summarize/evaluate heartbeat response before sending (uses aux model)."""
        return await agent.llm.complete(prompt, use_aux=True)

    async def heartbeat_idle(minutes_passed: int):
        """Record idle passage-of-time as a single user-role timeline block."""
        agent.llm.note_idle_interval(minutes_passed)
    
    async def get_active_reminders() -> str:
        """Get active reminders as formatted string."""
        from lethe.todos import TodoManager
        todo_manager = TodoManager(settings.db_path)
        todos = await todo_manager.list(status="pending")
        
        if not todos:
            return ""
        
        lines = []
        for todo in todos[:10]:  # Limit to 10
            priority = todo.get("priority", "normal")
            due = todo.get("due_at", "")
            due_str = f" (due: {due})" if due else ""
            lines.append(f"- [{priority}] {todo['title']}{due_str}")
        
        return "\n".join(lines)
    
    heartbeat = Heartbeat(
        process_callback=heartbeat_process,
        send_callback=heartbeat_send,
        summarize_callback=heartbeat_summarize,
        full_context_callback=heartbeat_full_context,
        get_reminders_callback=get_active_reminders,
        idle_callback=heartbeat_idle,
        interval=heartbeat_interval,
        enabled=heartbeat_enabled and heartbeat_chat_id is not None,
    )
    
    # Set heartbeat trigger on telegram bot for /heartbeat command
    telegram_bot.heartbeat_callback = heartbeat.trigger
    
    # Wire actor system into telegram bot for /status command
    if actor_system:
        telegram_bot.actor_system = actor_system
    
    # Wire DMN callbacks (send_to_user, get_reminders)
    if actor_system:
        actor_system.set_callbacks(
            send_to_user=heartbeat_send,
            get_reminders=get_active_reminders,
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
                        if actor_system.amygdala:
                            lethe_console.update_amygdala_context(actor_system.amygdala.get_context_view())
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

    # Start services
    console.print("[green]Starting services...[/green]")

    bot_task = asyncio.create_task(telegram_bot.start())
    heartbeat_task = asyncio.create_task(heartbeat.start())

    try:
        await shutdown_event.wait()
    except asyncio.CancelledError:
        pass
    finally:
        console.print("\n[yellow]Shutting down...[/yellow]")
        
        # Shutdown with timeout to avoid hanging on native threads
        try:
            async with asyncio.timeout(5):
                if console_monitor_task:
                    console_monitor_task.cancel()
                    try:
                        await console_monitor_task
                    except asyncio.CancelledError:
                        pass
                if actor_system:
                    await actor_system.shutdown()
                await heartbeat.stop()
                await telegram_bot.stop()
                await agent.close()
        except asyncio.TimeoutError:
            logger.warning("Shutdown timed out, forcing exit")
            os._exit(0)  # Force exit - LanceDB/OpenBLAS threads don't respect Python shutdown
        
        bot_task.cancel()
        heartbeat_task.cancel()
        try:
            await bot_task
        except asyncio.CancelledError:
            pass
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        
        console.print("[green]Shutdown complete.[/green]")


def main():
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Lethe - Autonomous AI Assistant")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging")
    
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("oauth-login", help="Login with Anthropic OAuth (Claude Max/Pro)")
    
    args = parser.parse_args()

    # Handle subcommands
    if args.command == "oauth-login":
        from lethe.tools.oauth_login import run_oauth_login
        run_oauth_login()
        return

    setup_logging(verbose=args.verbose)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
