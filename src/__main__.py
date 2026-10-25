"""Launcher: run the events bot and the decision bot in one process.

Usage: `python -m src`. The decision bot only starts when DECISION_BOT_TOKEN is set;
the events bot always starts (it exits if DISCORD_TOKEN is missing).
"""
import asyncio
import logging

from src import config


async def _run_events_bot() -> None:
    from src.bot import create_bot
    bot = create_bot()
    await bot.start(config.DISCORD_TOKEN)


async def _run_decision_bot() -> None:
    from src.decision_bot import run_decision_bot
    await run_decision_bot()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    if not config.DISCORD_TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set — copy .env.example to .env and fill it in")

    coros = [_run_events_bot()]
    if config.DECISION_BOT_TOKEN:
        coros.append(_run_decision_bot())
    else:
        logging.getLogger("hightide").info(
            "DECISION_BOT_TOKEN not set — running the events bot only")

    async def _gather():
        await asyncio.gather(*coros)

    asyncio.run(_gather())


if __name__ == "__main__":
    main()
