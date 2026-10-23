import asyncio
import hashlib
import logging
from datetime import datetime, time, timedelta

import discord
from sqlalchemy.orm import Session

from src.db.models import Event
from src.db.repositories import (
    get_active_events, get_board_post, get_or_create_config, save_board_post,
)
from src.discord_ui.embeds import build_board_embed

log = logging.getLogger("hightide.board")


_board_locks: dict[tuple[int, str], asyncio.Lock] = {}


def _get_board_lock(guild_id: int, board_type: str) -> asyncio.Lock:
    return _board_locks.setdefault((guild_id, board_type), asyncio.Lock())


def _window(board_type: str, cfg, tz, *, now: datetime | None = None) -> tuple[datetime, datetime]:
    now = now or datetime.now(tz)
    if board_type == "this_week":
        if cfg.week_start_mode == "today_start":
            start = datetime.combine(now.date(), time.min, tzinfo=tz)
        else:
            start = now
        return start, start + timedelta(days=7)
    # this_month
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def period_key(board_type: str, tz, *, now: datetime | None = None) -> str:
    now = now or datetime.now(tz)
    if board_type == "this_month":
        return now.strftime("%Y-%m")
    iso = now.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def filter_events_for_board(events: list[Event], board_type: str, cfg, tz, *,
                            now: datetime | None = None) -> list[Event]:
    start, end = _window(board_type, cfg, tz, now=now)
    out = []
    for e in events:
        if e.starts_at is None:
            if cfg.include_tba_section:
                out.append(e)
            continue
        local = e.starts_at.astimezone(tz)
        if start <= local < end:
            out.append(e)
    return out


def render_board_hash(events: list[Event], board_type: str, cfg, tz) -> str:
    parts = [board_type, str(tz)]
    for e in sorted(events, key=lambda x: (x.starts_at or datetime.max.replace(tzinfo=tz), x.title, x.external_id)):
        parts.append(f"{e.external_id}|{e.title}|{e.starts_at}|{e.price_text}|{e.time_known}|{e.url}")
    return hashlib.sha1("||".join(parts).encode("utf-8")).hexdigest()


def is_new_period(board_type: str, stored_key: str | None, current_key: str) -> bool:
    """Check if the period has changed (e.g., month or week rollover)."""
    return stored_key != current_key


async def upsert_board(bot, session: Session, guild_id: int, board_type: str) -> str:
    async with _get_board_lock(guild_id, board_type):
        return await _upsert_board_locked(bot, session, guild_id, board_type)


async def _upsert_board_locked(bot, session: Session, guild_id: int, board_type: str) -> str:
    cfg = get_or_create_config(session, guild_id)
    tz_channel = cfg.this_week_channel_id if board_type == "this_week" else cfg.this_month_channel_id
    if not tz_channel:
        return "no_channel"
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(cfg.timezone)
    now = datetime.now(tz)
    events = filter_events_for_board(get_active_events(session, guild_id), board_type, cfg, tz, now=now)
    new_hash = render_board_hash(events, board_type, cfg, tz)
    key = period_key(board_type, tz, now=now)
    title = "This Week at Hightide" if board_type == "this_week" else "This Month at Hightide"
    embed = build_board_embed(events, title, tz, cfg.include_tba_section)

    channel = bot.get_channel(tz_channel) or await bot.fetch_channel(tz_channel)
    post = get_board_post(session, guild_id, board_type)

    # Fetch the existing message once (only if the channel hasn't changed).
    msg = None
    if post and post.channel_id == channel.id:
        try:
            msg = await channel.fetch_message(post.message_id)
        except discord.NotFound:
            log.info("board message missing for %s, recreating", board_type)

    # Check if this is a period rollover for this_month
    is_rollover = post and board_type == "this_month" and is_new_period(board_type, post.period_key, key)

    if msg is not None and not is_rollover:
        if post.render_hash == new_hash and post.period_key == key:
            return "skipped"

        try:
            await msg.edit(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.NotFound:
            log.info("board message missing for %s, recreating", board_type)
        else:
            save_board_post(session, guild_id, board_type, key,
                            channel.id, msg.id, new_hash)
            return "edited"

    # Handle rollover or send new message
    if is_rollover:
        # Month rollover: send new message, save immediately, then optionally delete old one
        prefetched_old = msg
        old_channel_id = post.channel_id
        old_message_id = post.message_id
        new_msg = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        # Save before delete to avoid orphaned message if delete fails
        try:
            save_board_post(session, guild_id, board_type, key,
                            channel.id, new_msg.id, new_hash)
        except Exception:
            log.exception("saving rolled-over board post failed for %s; removing new message", board_type)
            try:
                await new_msg.delete()
            except Exception:
                log.exception("cleanup of unsaved board message failed for %s", board_type)
            raise

        # Try to delete old message if configured (best effort)
        if cfg.delete_previous_month_message:
            try:
                if prefetched_old is not None:
                    old_msg = prefetched_old
                else:
                    old_channel = bot.get_channel(old_channel_id) or await bot.fetch_channel(old_channel_id)
                    old_msg = None
                    old_guild = getattr(old_channel, "guild", None)
                    if old_guild is not None and old_guild.id != guild_id:
                        log.warning("old board channel %s belongs to another guild; skipping delete",
                                    old_channel_id)
                    else:
                        old_msg = await old_channel.fetch_message(old_message_id)
                # Apply authorship guard: only delete if bot sent it
                if old_msg is not None and old_msg.author.id == bot.user.id:
                    await old_msg.delete()
            except Exception:
                log.exception("could not delete previous month message for %s", board_type)

        return "recreated"

    # Send new message (no existing post or edit failed)
    msg = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    try:
        save_board_post(session, guild_id, board_type, key, channel.id, msg.id, new_hash)
    except Exception:
        log.exception("saving new board post failed for %s; removing new message", board_type)
        try:
            await msg.delete()
        except Exception:
            log.exception("cleanup of unsaved board message failed for %s", board_type)
        raise
    return "created"


async def delete_board(bot, session: Session, guild_id: int, board_type: str) -> str:
    """Delete the stored board message (bot-authored only) and its row, under the board lock."""
    async with _get_board_lock(guild_id, board_type):
        post = get_board_post(session, guild_id, board_type)
        if post is None:
            return "not_found"
        try:
            channel = bot.get_channel(post.channel_id) or await bot.fetch_channel(post.channel_id)
            msg = await channel.fetch_message(post.message_id)
            if msg.author.id == bot.user.id:
                await msg.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass  # Already gone or no permission
        session.delete(post)
        session.commit()
        return "deleted"
