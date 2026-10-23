import asyncio
import hashlib
import json
import logging
from datetime import datetime, time, timedelta

import discord
from sqlalchemy.orm import Session

from src.db.models import Event
from src.db.repositories import (
    get_active_events, get_board_post, get_or_create_config, save_board_post,
)
from src.discord_ui.embeds import build_board_embed, build_week_board_items

log = logging.getLogger("hightide.board")


_board_locks: dict[tuple[int, str], asyncio.Lock] = {}


def _get_board_lock(guild_id: int, board_type: str) -> asyncio.Lock:
    return _board_locks.setdefault((guild_id, board_type), asyncio.Lock())


def _window(board_type: str, cfg, tz, *, now: datetime | None = None) -> tuple[datetime, datetime]:
    now = now or datetime.now(tz)
    if board_type == "this_week":
        if cfg.week_start_mode == "monday":
            today = datetime.combine(now.date(), time.min, tzinfo=tz)
            monday = datetime.combine(now.date() - timedelta(days=now.weekday()), time.min, tzinfo=tz)
            # Wall-clock arithmetic: next Monday is 00:00 local even across DST changes.
            return max(today, monday), monday + timedelta(days=7)
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
        parts.append(f"{e.external_id}|{e.title}|{e.starts_at}|{e.price_text}|{e.time_known}|{e.url}"
                     f"|{e.summary}|{e.image_url}")
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
    key = period_key(board_type, tz, now=now)

    if board_type == "this_week":
        channel = bot.get_channel(tz_channel) or await bot.fetch_channel(tz_channel)
        post = get_board_post(session, guild_id, board_type)
        return await _upsert_week_set(bot, session, guild_id, cfg, tz, events, key, channel, post)

    new_hash = render_board_hash(events, board_type, cfg, tz)
    title = "This Month at Hightide"
    payload = {"embed": build_board_embed(events, title, tz, cfg.include_tba_section)}

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
            await msg.edit(**payload, allowed_mentions=discord.AllowedMentions.none())
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
        new_msg = await channel.send(**payload, allowed_mentions=discord.AllowedMentions.none())
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
    msg = await channel.send(**payload, allowed_mentions=discord.AllowedMentions.none())
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
    """Delete every stored board message (bot-authored only) and the row, under the board lock."""
    async with _get_board_lock(guild_id, board_type):
        post = get_board_post(session, guild_id, board_type)
        if post is None:
            return "not_found"
        try:
            channel = bot.get_channel(post.channel_id) or await bot.fetch_channel(post.channel_id)
        except discord.HTTPException:
            channel = None  # Already gone or no permission
        if channel is not None:
            await _delete_stored_messages(
                bot, channel, [post.message_id, *_loads_ids(post.message_ids)])
        session.delete(post)
        session.commit()
        return "deleted"


def _loads_ids(raw: str | None) -> list[int]:
    try:
        ids = json.loads(raw or "[]")
    except ValueError:
        return []
    return [i for i in ids if isinstance(i, int)] if isinstance(ids, list) else []


async def _delete_stored_messages(bot, channel, message_ids: list[int]) -> None:
    """Best-effort delete of bot-authored messages; each failure is tolerated independently."""
    for message_id in message_ids:
        try:
            msg = await channel.fetch_message(message_id)
            if msg.author.id == bot.user.id:
                await msg.delete()
        except discord.HTTPException:
            log.info("could not delete board message %s", message_id)


def _card_hash(e: Event, tz) -> str:
    parts = [str(tz), e.title, str(e.starts_at), str(e.price_text), str(e.time_known),
             str(e.url), str(e.summary), str(e.image_url)]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def _embed_hash(embed: discord.Embed) -> str:
    parts = [embed.title or "", embed.description or ""]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def _stored_week_set(post) -> tuple[dict[str, tuple[int, str | None]], list[int]]:
    """Map key -> (message_id, hash) for the stored set, plus unmappable extra ids."""
    ids = [post.message_id, *_loads_ids(post.message_ids)]
    try:
        data = json.loads(post.render_hash)
        keys, hashes = data["keys"], data["hashes"]
        if (isinstance(keys, list) and isinstance(hashes, dict)
                and len(keys) == len(ids) and keys[0] == "header"):
            return {k: (mid, hashes.get(k)) for k, mid in zip(keys, ids)}, []
    except (ValueError, TypeError, KeyError):
        pass
    # Legacy single multi-embed message (or corrupt state): keep the header, drop the rest.
    return {"header": (ids[0], None)}, ids[1:]


async def _upsert_week_set(bot, session: Session, guild_id: int, cfg, tz, events: list[Event],
                           period: str, channel, post) -> str:
    items = build_week_board_items(events, "This Week at Hightide", tz, cfg.include_tba_section)
    by_id = {e.external_id: e for e in events}
    keys = [k for k, _ in items]
    hashes = {k: _card_hash(by_id[k], tz) if k in by_id else _embed_hash(embed)
              for k, embed in items}
    no_mentions = discord.AllowedMentions.none()

    stored: dict[str, tuple[int, str | None]] = {}
    extras: list[int] = []
    header_msg = None
    if post and post.channel_id == channel.id:
        stored, extras = _stored_week_set(post)
        try:
            header_msg = await channel.fetch_message(stored["header"][0])
        except discord.NotFound:
            log.info("board header message missing for this_week, recreating set")

    changed = False
    if header_msg is not None and (stored["header"][1] != hashes["header"]
                                   or post.period_key != period):
        try:
            await header_msg.edit(embed=items[0][1], allowed_mentions=no_mentions)
            changed = True
        except discord.NotFound:
            log.info("board header message missing for this_week, recreating set")
            header_msg = None

    if header_msg is None:
        if stored:
            leftovers = [mid for k, (mid, _) in stored.items() if k != "header"] + extras
            await _delete_stored_messages(bot, channel, leftovers)
        stored = {}

    sent: list = []
    new_ids: dict[str, int] = {}
    stale: list[int] = []
    try:
        for key, embed in items:
            old = stored.get(key)
            if key == "header" and old is not None:
                new_ids[key] = old[0]
            elif old is not None and old[1] == hashes[key]:
                new_ids[key] = old[0]
            else:
                if old is not None:
                    try:
                        card_msg = await channel.fetch_message(old[0])
                        await card_msg.edit(embed=embed, allowed_mentions=no_mentions)
                        new_ids[key] = old[0]
                        changed = True
                        continue
                    except discord.NotFound:
                        log.info("board message for %s missing, resending", key)
                msg = await channel.send(embed=embed, allowed_mentions=no_mentions)
                sent.append(msg)
                new_ids[key] = msg.id
                changed = True

        if stored:
            if not changed and list(stored) == keys:
                return "skipped"
            stale = [mid for k, (mid, _) in stored.items() if k not in new_ids] + extras
        save_board_post(session, guild_id, "this_week", period, channel.id, new_ids["header"],
                        json.dumps({"keys": keys, "hashes": hashes}),
                        json.dumps([new_ids[k] for k in keys[1:]]))
    except Exception:
        log.exception("this_week board update failed; removing %d newly sent message(s)", len(sent))
        for msg in sent:
            try:
                await msg.delete()
            except Exception:
                log.exception("cleanup of unsaved board message failed for this_week")
        raise

    await _delete_stored_messages(bot, channel, stale)
    return "edited" if stored else "created"
