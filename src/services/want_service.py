import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.db.models import CardWant
from src.db.repositories import get_board_post, get_or_create_config, save_board_post
from src.discord_ui.embeds import _safe_url, build_wants_embed, want_sort_key
from src.services.board_service import _get_board_lock

log = logging.getLogger("hightide.wants")

MAX_CARD_NAME_LEN = 200
MAX_NOTE_LEN = 120
MAX_ACTIVE_PER_USER = 10
EXPIRY_DAYS = 30
BOARD_TYPE = "wants"
# Zero-width/bidi controls and markdown chars that can make a name render as nothing.
_INVISIBLE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2069*_`~|]")


class WantLimitError(ValueError):
    pass


def _active_count(session: Session, guild_id: int, user_id: int) -> int:
    return session.execute(
        select(func.count()).select_from(CardWant).where(
            CardWant.guild_id == guild_id, CardWant.user_id == user_id,
            CardWant.active.is_(True))).scalar_one()


def _clean_note(note: str | None) -> str | None:
    note = (note or "").strip() or None
    if note is not None:
        if "://" in note:
            raise ValueError("Links aren't allowed in notes.")
        if len(note) > MAX_NOTE_LEN:
            raise ValueError(f"Note must be at most {MAX_NOTE_LEN} characters.")
    return note


def add_want_from_printing(session: Session, guild_id: int, user_id: int,
                           card: dict, note: str | None = None,
                           *, now: datetime | None = None) -> CardWant:
    """Insert a want from a chosen Scryfall printing (card dict from the printings search).

    The name/link/set/image all come from Scryfall, never from free text.
    """
    now = now or datetime.now(timezone.utc)
    note = _clean_note(note)

    name = card.get("name")
    uri = card.get("scryfall_uri")
    set_code = card.get("set")
    cn = card.get("collector_number")
    if not (isinstance(name, str) and name.strip()):
        raise ValueError("That card has no name — pick a printing from the list.")
    if not (isinstance(uri, str) and _safe_url(uri)):
        raise ValueError("That card has no Scryfall link — pick a printing from the list.")
    card_name = name.strip()[:MAX_CARD_NAME_LEN]
    image = _printing_image(card)

    expire_wants(session, guild_id, now)
    limit_msg = f"You already have {MAX_ACTIVE_PER_USER} active wants — remove one first."
    if _active_count(session, guild_id, user_id) >= MAX_ACTIVE_PER_USER:
        raise WantLimitError(limit_msg)
    want = CardWant(guild_id=guild_id, user_id=user_id, card_name=card_name,
                    scryfall_uri=uri,
                    set_code=set_code if isinstance(set_code, str) else None,
                    collector_number=cn if isinstance(cn, str) else None,
                    image_url=image, note=note, created_at=now, active=True)
    session.add(want)
    session.commit()
    return want


def _printing_image(card: dict) -> str | None:
    """Best image URL for a printing: normal, falling back to the front face of a DFC."""
    uris = card.get("image_uris")
    if isinstance(uris, dict) and isinstance(uris.get("normal"), str) and uris["normal"]:
        url = _safe_url(uris["normal"])
        if url:
            return url
    faces = card.get("card_faces")
    if isinstance(faces, list) and faces and isinstance(faces[0], dict):
        furis = faces[0].get("image_uris")
        if isinstance(furis, dict) and isinstance(furis.get("normal"), str):
            return _safe_url(furis["normal"])
    return None


def remove_want(session: Session, guild_id: int, want_id: int, user_id: int,
                is_admin: bool) -> bool:
    want = session.get(CardWant, want_id)
    if want is None or want.guild_id != guild_id or not want.active:
        return False
    if want.user_id != user_id and not is_admin:
        return False
    session.delete(want)
    session.commit()
    return True


def expire_wants(session: Session, guild_id: int, now: datetime) -> int:
    """Deactivate wants strictly older than EXPIRY_DAYS."""
    cutoff = now - timedelta(days=EXPIRY_DAYS)
    rows = session.execute(select(CardWant).where(
        CardWant.guild_id == guild_id, CardWant.active.is_(True),
        CardWant.created_at < cutoff)).scalars().all()
    for row in rows:
        row.active = False
    if rows:
        session.commit()
    return len(rows)


def list_wants(session: Session, guild_id: int, user_id: int | None = None, *,
               now: datetime | None = None) -> list[CardWant]:
    """Active wants, alphabetical by card name (case-insensitive); expires stale rows first."""
    expire_wants(session, guild_id, now or datetime.now(timezone.utc))
    stmt = select(CardWant).where(CardWant.guild_id == guild_id, CardWant.active.is_(True))
    if user_id is not None:
        stmt = stmt.where(CardWant.user_id == user_id)
    return sorted(session.execute(stmt).scalars(), key=want_sort_key)


async def update_wants_board(bot, session: Session, guild_id: int, *,
                             now: datetime | None = None) -> str:
    async with _get_board_lock(guild_id, BOARD_TYPE):
        return await _update_wants_board_locked(bot, session, guild_id, now)


async def _update_wants_board_locked(bot, session: Session, guild_id: int,
                                     now: datetime | None) -> str:
    cfg = get_or_create_config(session, guild_id)
    if not cfg.wants_channel_id:
        return "no_channel"
    tz = ZoneInfo(cfg.timezone)
    embed = build_wants_embed(list_wants(session, guild_id, now=now), tz)
    new_hash = hashlib.sha1((embed.description or "").encode("utf-8")).hexdigest()
    no_mentions = discord.AllowedMentions.none()

    channel = bot.get_channel(cfg.wants_channel_id) or await bot.fetch_channel(cfg.wants_channel_id)
    post = get_board_post(session, guild_id, BOARD_TYPE)
    moved_from = (post.channel_id, post.message_id) if post and post.channel_id != channel.id else None
    msg = None
    if post and post.channel_id == channel.id:
        try:
            msg = await channel.fetch_message(post.message_id)
        except discord.NotFound:
            log.info("wants board message missing, recreating")

    if msg is not None:
        if post.render_hash == new_hash:
            return "skipped"
        try:
            await msg.edit(embed=embed, allowed_mentions=no_mentions)
        except discord.NotFound:
            log.info("wants board message missing, recreating")
        else:
            save_board_post(session, guild_id, BOARD_TYPE, BOARD_TYPE, channel.id, msg.id, new_hash)
            return "edited"

    new_msg = await channel.send(embed=embed, allowed_mentions=no_mentions)
    try:
        save_board_post(session, guild_id, BOARD_TYPE, BOARD_TYPE, channel.id, new_msg.id, new_hash)
    except Exception:
        log.exception("saving wants board post failed; removing new message")
        try:
            await new_msg.delete()
        except Exception:
            log.exception("cleanup of unsaved wants board message failed")
        raise
    if moved_from is not None:
        await _delete_old_board_message(bot, *moved_from)
    return "created"


async def _delete_old_board_message(bot, channel_id: int, message_id: int) -> None:
    """Best-effort removal of the board left behind in the previous wants channel."""
    try:
        old_channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        old_msg = await old_channel.fetch_message(message_id)
        if old_msg.author.id == bot.user.id:
            await old_msg.delete()
    except discord.HTTPException:  # NotFound/Forbidden included
        log.info("could not delete old wants board message %s in %s", message_id, channel_id)
