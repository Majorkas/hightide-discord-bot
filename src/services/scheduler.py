import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from discord.ext import tasks

from src import config
from src.db.repositories import get_or_create_config
from src.db.session import get_session_factory
from src.services.board_service import upsert_board
from src.services.event_service import refresh_events

log = logging.getLogger("hightide.scheduler")

_RETRY_INTERVAL = timedelta(minutes=30)

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


def _now(tz) -> datetime:
    return datetime.now(tz)


def _in_run_window(daily_hhmm: str, now: datetime) -> bool:
    try:
        h, m = (int(p) for p in daily_hhmm.split(":"))
    except (ValueError, AttributeError):
        return False
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return False
    return (now.hour, now.minute) >= (h, m)


async def run_daily_refresh(bot, guild_id: int) -> dict:
    with _get_session_factory()() as session:
        cfg = get_or_create_config(session, guild_id)
        tz = ZoneInfo(cfg.timezone)
        try:
            events = await asyncio.to_thread(
                refresh_events, session, guild_id, "https://hightidetabletop.com.au", tz
            )
        except Exception as e:
            log.exception("event refresh failed")
            events = f"error:{type(e).__name__}"
        result = {"events": events}
        for board_type in ("this_week", "this_month"):
            try:
                result[board_type] = await upsert_board(bot, session, guild_id, board_type)
            except Exception as e:
                log.exception("board upsert failed for %s", board_type)
                result[board_type] = f"error:{type(e).__name__}"
        log.info("daily refresh: %s", result)
        return result


class RefreshScheduler:
    def __init__(self, bot):
        self.bot = bot
        self._last_run_key: str | None = None
        self._last_attempt: datetime | None = None

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
                now = _now(tz)
                today = now.strftime("%Y-%m-%d")
                if (_in_run_window(cfg.daily_refresh_time, now)
                        and self._last_run_key != today
                        and (self._last_attempt is None
                             or now - self._last_attempt >= _RETRY_INTERVAL)):
                    self._last_attempt = now
                    await run_daily_refresh(self.bot, cfg.guild_id)
                    self._last_run_key = today
            except Exception:
                log.exception("scheduler tick failed")
