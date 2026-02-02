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
    agent.refresh_memory_context()
    
    stats = agent.get_stats()
    console.print(f"[green]Agent ready[/green] - {stats['memory_blocks']} blocks, {stats['archival_memories']} memories")

    # Initialize conversation manager
    conversation_manager = ConversationManager(debounce_seconds=settings.debounce_seconds)
    logger.info(f"Conversation manager initialized (debounce: {settings.debounce_seconds}s)")

    # Message processing callback
    async def process_message(chat_id: int, user_id: int, message: str, metadata: dict, interrupt_check):
        """Process a message from Telegram."""
        logger.info(f"Processing message from {user_id}: {message[:50]}...")
        
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
            await telegram_bot.send_message(chat_id, response)
            
        except Exception as e:
            logger.exception(f"Error processing message: {e}")
            await telegram_bot.send_message(chat_id, f"Error: {e}")
        finally:
            await telegram_bot.stop_typing(chat_id)

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
        """Process heartbeat with minimal context and aux model."""
        return await agent.heartbeat(message)
    
    async def heartbeat_send(response: str):
        """Send heartbeat response to user."""
        if heartbeat_chat_id:
            await telegram_bot.send_message(heartbeat_chat_id, response)
    
    async def heartbeat_summarize(prompt: str) -> str:
        """Summarize/evaluate heartbeat response before sending (uses aux model)."""
        return await agent.llm.complete(prompt, use_aux=True)
    
    heartbeat = Heartbeat(
        process_callback=heartbeat_process,
        send_callback=heartbeat_send,
        summarize_callback=heartbeat_summarize,
        interval=heartbeat_interval,
        enabled=heartbeat_enabled and heartbeat_chat_id is not None,
    )
    
    # Set heartbeat trigger on telegram bot for /heartbeat command
    telegram_bot.heartbeat_callback = heartbeat.trigger

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
    args = parser.parse_args()

    setup_logging(verbose=args.verbose)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
