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


def _window(board_type: str, cfg, tz) -> tuple[datetime, datetime]:
    now = datetime.now(tz)
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


def period_key(board_type: str, tz) -> str:
    now = datetime.now(tz)
    if board_type == "this_month":
        return now.strftime("%Y-%m")
    iso = now.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def filter_events_for_board(events: list[Event], board_type: str, cfg, tz) -> list[Event]:
    start, end = _window(board_type, cfg, tz)
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
    for e in sorted(events, key=lambda x: (x.starts_at or datetime.max.replace(tzinfo=tz), x.title)):
        parts.append(f"{e.external_id}|{e.title}|{e.starts_at}|{e.price_text}|{e.time_known}|{e.url}")
    return hashlib.sha1("||".join(parts).encode("utf-8")).hexdigest()


async def upsert_board(bot, session: Session, guild_id: int, board_type: str) -> str:
    cfg = get_or_create_config(session, guild_id)
    tz_channel = cfg.this_week_channel_id if board_type == "this_week" else cfg.this_month_channel_id
    if not tz_channel:
        return "no_channel"
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(cfg.timezone)
    events = filter_events_for_board(get_active_events(session, guild_id), board_type, cfg, tz)
    new_hash = render_board_hash(events, board_type, cfg, tz)
    key = period_key(board_type, tz)
    title = "This Week at Hightide" if board_type == "this_week" else "This Month at Hightide"
    embed = build_board_embed(events, title, tz, cfg.include_tba_section)

    channel = bot.get_channel(tz_channel) or await bot.fetch_channel(tz_channel)
    post = get_board_post(session, guild_id, board_type)

    if post and post.render_hash == new_hash and post.period_key == key and post.channel_id == channel.id:
        try:
            await channel.fetch_message(post.message_id)
        except discord.NotFound:
            log.info("board message missing for %s, recreating", board_type)
        else:
            return "skipped"

    # try to edit existing
    if post:
        try:
            msg = await channel.fetch_message(post.message_id)
            await msg.edit(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            save_board_post(session, guild_id, board_type, key,
                            channel.id, msg.id, new_hash)
            return "edited"
        except discord.NotFound:
            log.info("board message missing for %s, recreating", board_type)

    msg = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    save_board_post(session, guild_id, board_type, key, channel.id, msg.id, new_hash)
    return "created"
