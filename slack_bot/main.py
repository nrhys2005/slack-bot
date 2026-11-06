from __future__ import annotations

import logging
import os

from dotenv import load_dotenv
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from slack_bot.config import load_projects
from slack_bot.handlers import register_handlers
from slack_bot.orca_session import sweep_orphans
from slack_bot.task_manager import TaskManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


def _log_sweep_failure(task) -> None:
    if not task.cancelled() and task.exception():
        logging.getLogger(__name__).warning("orca 고아 세션 정리 실패: %r", task.exception())


def main() -> None:
    load_dotenv()

    app = AsyncApp(token=os.environ["SLACK_BOT_TOKEN"])
    task_manager = TaskManager()
    register_handlers(app, task_manager)

    import asyncio

    async def start():
        sweep = asyncio.create_task(sweep_orphans(load_projects().projects))
        sweep.add_done_callback(_log_sweep_failure)
        handler = AsyncSocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
        await handler.start_async()

    asyncio.run(start())


if __name__ == "__main__":
    main()
