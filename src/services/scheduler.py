import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from discord.ext import tasks

from src import config
from src.db.repositories import get_or_create_config
from src.db.session import get_session_factory
from src.services.board_service import upsert_board
from src.services.event_service import refresh_events

log = logging.getLogger("hightide.scheduler")

# Created lazily; tests can monkeypatch or reset to None.
_session_factory = None


def _get_session_factory():
    global _session_factory
    if _session_factory is None:
        _session_factory = get_session_factory(config.DATABASE_URL)
    return _session_factory


def should_run_now(hhmm: str, hour: int, minute: int) -> bool:
    try:
        h, m = (int(p) for p in hhmm.split(":"))
    except ValueError:
        return False
    return (hour, minute) == (h, m)


async def run_daily_refresh(bot, guild_id: int) -> dict:
    with _get_session_factory()() as session:
        cfg = get_or_create_config(session, guild_id)
        tz = ZoneInfo(cfg.timezone)
        stats = await asyncio.to_thread(
            refresh_events, session, guild_id, "https://hightidetabletop.com.au", tz
        )
        week = await upsert_board(bot, session, guild_id, "this_week")
        month = await upsert_board(bot, session, guild_id, "this_month")
        result = {"events": stats, "this_week": week, "this_month": month}
        log.info("daily refresh: %s", result)
        return result


class RefreshScheduler:
    def __init__(self, bot):
        self.bot = bot
        self._last_run_key: str | None = None

    def start(self):
        self.tick.start()

    @tasks.loop(seconds=60)
    async def tick(self):
        await self.bot.wait_until_ready()
        if config.GUILD_ID is None:
            log.debug("GUILD_ID not configured, skipping scheduler tick")
            return
        with _get_session_factory()() as session:
            try:
                cfg = get_or_create_config(session, config.GUILD_ID)
                tz = ZoneInfo(cfg.timezone)
                now = datetime.now(tz)
                run_key = now.strftime("%Y-%m-%d")
                if (should_run_now(cfg.daily_refresh_time, now.hour, now.minute)
                        and self._last_run_key != run_key):
                    self._last_run_key = run_key
                    await run_daily_refresh(self.bot, cfg.guild_id)
            except Exception:
                log.exception("scheduler tick failed")
