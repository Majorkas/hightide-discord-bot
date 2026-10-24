import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from discord.ext import tasks
from sqlalchemy import select

from src import config
from src.db.models import Event, GuildConfig
from src.db.repositories import get_active_events, get_or_create_config
from src.db.session import get_session_factory
from src.discord_ui.embeds import build_tonight_embed
from src.services.board_service import upsert_board
from src.services.event_service import refresh_events
from src.services.role_service import _resolve_channel, ping_new_events
from src.services.want_service import update_wants_board

log = logging.getLogger("hightide.scheduler")

_RETRY_INTERVAL = timedelta(minutes=30)
_MAX_RETRY_DELAY = timedelta(hours=2)

# Created lazily; tests can monkeypatch or reset to None.
_session_factory = None

_refresh_locks: dict[int, asyncio.Lock] = {}


def _get_refresh_lock(guild_id: int) -> asyncio.Lock:
    return _refresh_locks.setdefault(guild_id, asyncio.Lock())


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


async def run_daily_refresh(bot, guild_id: int, *, fetch: bool = True) -> dict:
    async with _get_refresh_lock(guild_id):
        return await _run_daily_refresh_locked(bot, guild_id, fetch=fetch)


async def _run_daily_refresh_locked(bot, guild_id: int, *, fetch: bool) -> dict:
    with _get_session_factory()() as session:
        cfg = get_or_create_config(session, guild_id)
        tz = ZoneInfo(cfg.timezone)
        had_prior = False
        if fetch:
            had_prior = session.query(Event).filter_by(guild_id=guild_id).first() is not None
            try:
                events = await asyncio.to_thread(
                    refresh_events, session, guild_id, "https://hightidetabletop.com.au", tz
                )
            except Exception as e:
                log.exception("event refresh failed")
                session.rollback()
                events = f"error:{type(e).__name__}"
        else:
            events = "cached"
        created_events = []
        if isinstance(events, dict):
            # Keep ORM rows out of the result dict that gets logged and returned.
            created_events = events.pop("created_events", [])
        result = {"events": events}
        if isinstance(events, dict):
            # Candidates come from the DB; a first-ever sync is seeded silently.
            try:
                result["pings"] = await ping_new_events(
                    bot, session, guild_id, tz, suppress=not had_prior)
            except Exception:
                log.exception("event pings failed")
                session.rollback()
                result["pings"] = 0
        for board_type in ("this_week", "this_month"):
            try:
                result[board_type] = await upsert_board(bot, session, guild_id, board_type)
            except Exception as e:
                log.exception("board upsert failed for %s", board_type)
                session.rollback()
                result[board_type] = f"error:{type(e).__name__}"
        if cfg.wants_channel_id:
            try:
                result["wants"] = await update_wants_board(bot, session, guild_id)
            except Exception as e:
                log.exception("wants board update failed")
                session.rollback()
                result["wants"] = f"error:{type(e).__name__}"
        log.info("daily refresh: %s", result)
        return result


async def _post_tonight(bot, session, cfg, tz, events, now: datetime) -> str:
    today = now.astimezone(tz).strftime("%Y-%m-%d")
    if cfg.announce_channel_id is None:
        return "skipped:no_channel"
    if cfg.last_daily_post_date == today:
        return "skipped:already_posted"
    # Every active event dated today in guild tz, including time-TBC ones.
    todays = [e for e in events if e.is_active and e.starts_at is not None
              and e.starts_at.astimezone(tz).strftime("%Y-%m-%d") == today]
    embed = build_tonight_embed(todays, tz)
    if embed is None:
        return "skipped:no_events"
    try:
        channel = await _resolve_channel(bot, cfg.announce_channel_id)
        if channel is None:
            return "error:ChannelUnavailable"
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as e:
        log.exception("tonight post failed for guild %s", cfg.guild_id)
        return f"error:{type(e).__name__}"
    cfg.last_daily_post_date = today
    session.commit()
    return "posted"


async def maybe_post_tonight(bot, session, cfg, tz, events, now: datetime) -> bool:
    return await _post_tonight(bot, session, cfg, tz, events, now) == "posted"


class RefreshScheduler:
    def __init__(self, bot):
        self.bot = bot
        # Per-guild state keyed by guild_id so one guild's failure can't affect another.
        self._last_run_key: dict[int, str] = {}
        self._last_attempt: dict[int, datetime] = {}
        self._events_done_key: dict[int, str] = {}
        self._retry_delay: dict[int, timedelta] = {}
        self._next_attempt: dict[int, datetime | None] = {}
        self._backoff_day: dict[int, str] = {}
        self._idle_logged = False

    def start(self):
        self.tick.start()

    def _guild_ids(self, session) -> list[int]:
        if config.GUILD_ID is not None:
            return [config.GUILD_ID]
        return list(session.scalars(select(GuildConfig.guild_id).order_by(GuildConfig.guild_id)))

    async def _tick_guild(self, session, guild_id: int) -> None:
        cfg = get_or_create_config(session, guild_id)
        tz = ZoneInfo(cfg.timezone)
        now = _now(tz)
        today = now.strftime("%Y-%m-%d")
        if self._backoff_day.get(guild_id) != today:
            # Backoff is per-day: a bad day must not delay the next day's run.
            self._backoff_day[guild_id] = today
            self._retry_delay[guild_id] = _RETRY_INTERVAL
            self._next_attempt[guild_id] = None
        next_attempt = self._next_attempt.get(guild_id)
        if not (_in_run_window(cfg.daily_refresh_time, now)
                and self._last_run_key.get(guild_id) != today
                and (next_attempt is None or now >= next_attempt)):
            return
        self._last_attempt[guild_id] = now
        fetch = self._events_done_key.get(guild_id) != today
        failed = True
        try:
            result = await run_daily_refresh(self.bot, cfg.guild_id, fetch=fetch)
            has_error = any(
                isinstance(v, str) and v.startswith("error:")
                for k, v in result.items() if k not in ("pings", "wants", "tonight")
            )
            if isinstance(result.get("events"), dict):
                self._events_done_key[guild_id] = today
            if not has_error:
                self._last_run_key[guild_id] = today
                self._retry_delay[guild_id] = _RETRY_INTERVAL
                self._next_attempt[guild_id] = None
                failed = False
            events_ok = isinstance(result.get("events"), dict) or result.get("events") == "cached"
            if events_ok:
                # Gated on the events step only (a board failure must not suppress it) and
                # kept out of the has_error scan, so a tonight failure never drives backoff.
                try:
                    session.expire_all()
                    result["tonight"] = await _post_tonight(
                        self.bot, session, cfg, tz, get_active_events(session, guild_id), now)
                except Exception as e:
                    log.exception("tonight post failed for guild %s", guild_id)
                    session.rollback()
                    result["tonight"] = f"error:{type(e).__name__}"
                log.info("tonight post: %s", result["tonight"])
        finally:
            if failed:
                delay = self._retry_delay.get(guild_id, _RETRY_INTERVAL)
                self._next_attempt[guild_id] = now + delay
                self._retry_delay[guild_id] = min(delay * 2, _MAX_RETRY_DELAY)

    @tasks.loop(seconds=60)
    async def tick(self):
        await self.bot.wait_until_ready()
        try:
            with _get_session_factory()() as session:
                guild_ids = self._guild_ids(session)
                if not guild_ids:
                    if not self._idle_logged:
                        log.info("scheduler idle — no guild configured yet")
                        self._idle_logged = True
                    return
                self._idle_logged = False
                for guild_id in guild_ids:
                    try:
                        await self._tick_guild(session, guild_id)
                    except Exception:
                        log.exception("scheduler tick failed for guild %s", guild_id)
                        session.rollback()
        except Exception:
            log.exception("scheduler tick failed")
