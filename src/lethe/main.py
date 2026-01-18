"""Main entry point for Lethe."""

import asyncio
import logging
import signal
import sys


from rich.console import Console
from rich.logging import RichHandler

from lethe.agent import AgentManager
from lethe.config import get_settings
from lethe.queue import TaskQueue
from lethe.telegram import TelegramBot
from lethe.worker import HeartbeatWorker, Worker

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


async def run():
    """Run the Lethe application."""
    logger = logging.getLogger(__name__)

    try:
        settings = get_settings()
    except Exception as e:
        console.print(f"[red]Configuration error:[/red] {e}")
        console.print("\nMake sure you have a .env file with TELEGRAM_BOT_TOKEN set.")
        sys.exit(1)

    console.print("[bold blue]Lethe[/bold blue] - Autonomous Executive Assistant")
    console.print(f"Letta server: {settings.letta_base_url}")
    console.print(f"Agent name: {settings.lethe_agent_name}")
    console.print()

    # Initialize components
    task_queue = TaskQueue(settings.db_path)
    await task_queue.initialize()
    logger.info("Task queue initialized")

    agent_manager = AgentManager(settings)
    telegram_bot = TelegramBot(settings, task_queue)

    # Create worker
    worker = Worker(task_queue, agent_manager, telegram_bot, settings)

    # Create heartbeat worker if we have a primary user
    heartbeat = None
    if settings.allowed_user_ids:
        primary_user_id = settings.allowed_user_ids[0]
        heartbeat = HeartbeatWorker(
            agent_manager=agent_manager,
            telegram_bot=telegram_bot,
            chat_id=primary_user_id,
            interval_minutes=60,
            enabled=True,
        )
        logger.info(f"Heartbeat enabled for user {primary_user_id}")
    else:
        logger.info("Heartbeat disabled (no allowed_user_ids configured)")

    # Set up shutdown handling
    shutdown_event = asyncio.Event()

    def signal_handler():
        logger.info("Received shutdown signal...")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)

    # Start all components
    console.print("[green]Starting services...[/green]")

    # Create tasks
    worker_task = asyncio.create_task(worker.start())
    bot_task = asyncio.create_task(telegram_bot.start())
    heartbeat_task = asyncio.create_task(heartbeat.start()) if heartbeat else None

    try:
        # Wait for shutdown signal
        await shutdown_event.wait()
    except asyncio.CancelledError:
        pass
    finally:
        # Cleanup
        console.print("\n[yellow]Shutting down...[/yellow]")
        
        # Stop components
        await worker.stop()
        await telegram_bot.stop()
        if heartbeat:
            await heartbeat.stop()
        
        # Cancel tasks
        worker_task.cancel()
        bot_task.cancel()
        if heartbeat_task:
            heartbeat_task.cancel()
        
        # Wait for tasks to finish
        tasks_to_wait = [worker_task, bot_task]
        if heartbeat_task:
            tasks_to_wait.append(heartbeat_task)
        for task in tasks_to_wait:
            try:
                await task
            except asyncio.CancelledError:
                pass
        
        await task_queue.close()
        console.print("[green]Shutdown complete.[/green]")


def main():
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Lethe - Autonomous Executive Assistant")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging")
    args = parser.parse_args()

    setup_logging(verbose=args.verbose)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
