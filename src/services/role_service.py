import logging
import re
from datetime import datetime, timedelta, timezone

import discord
from sqlalchemy import select
from sqlalchemy.orm import Session

from src import config
from src.db.models import Event, EventRolePing, InterestRole
from src.db.repositories import (
    get_keywords_by_role, get_or_create_config, list_interest_roles, record_ping,
)
from src.db.session import get_session_factory
from src.discord_ui.embeds import build_ping_message

log = logging.getLogger("hightide.roles")

MAX_PINGS_PER_RUN = 5
MAX_ROLES_PER_EVENT = 2
PENDING_WINDOW = timedelta(hours=48)
MAX_MESSAGE_CHARS = 2000

_session_factory = None


def _session() -> Session:
    global _session_factory
    if _session_factory is None:
        _session_factory = get_session_factory(config.DATABASE_URL)
    return _session_factory()


_PRIVILEGED_PERMISSIONS = (
    "administrator", "manage_guild", "manage_roles", "manage_channels", "manage_webhooks",
    "kick_members", "ban_members", "mention_everyone", "moderate_members",
)


def role_rejection_reason(role: discord.Role) -> str | None:
    """Why this role must never be self-assignable from the panel, or None if it's safe."""
    if role.is_default():
        return "@everyone can't be added to the panel."
    if role.managed:
        return "That role is managed by an integration and can't be assigned."
    if not role.is_assignable():
        return "I can't assign that role — move my role above it and give me Manage Roles."
    perms = role.permissions
    risky = [n.replace("_", " ") for n in _PRIVILEGED_PERMISSIONS if getattr(perms, n)]
    if risky:
        return ("That role has privileged permissions (" + ", ".join(risky)
                + ") and can't be self-assigned from the panel.")
    return None


async def handle_role_toggle(interaction: discord.Interaction, role_row_id: int) -> None:
    # thinking=True is required for the ephemeral flag to apply to a component interaction.
    await interaction.response.defer(ephemeral=True, thinking=True)
    with _session() as session:
        row = session.get(InterestRole, role_row_id)
        if row is None or not row.enabled or row.guild_id != interaction.guild_id:
            await interaction.followup.send("That role isn't available any more.", ephemeral=True)
            return
        role_id, label = row.role_id, row.label
    label = discord.utils.escape_mentions(label)
    role = interaction.guild.get_role(role_id)
    member = interaction.user
    if role is None:
        await interaction.followup.send(
            f"The **{label}** role no longer exists — tell an admin.", ephemeral=True)
        return
    try:
        if role in member.roles:
            await member.remove_roles(role, reason="interest role toggle")
            msg = f"Removed **{label}**."
        else:
            if role_rejection_reason(role):
                await interaction.followup.send(
                    "This role can't be self-assigned — tell an admin.", ephemeral=True)
                return
            await member.add_roles(role, reason="interest role toggle")
            msg = f"Added **{label}** — you'll be pinged for new events."
    except discord.Forbidden:
        msg = "I can't manage that role (check my role sits above it). Tell an admin."
    except discord.HTTPException:
        msg = "Something went wrong — try again."
    await interaction.followup.send(msg, ephemeral=True)


def _now(tz) -> datetime:
    return datetime.now(tz)


def _ranked_matches(title: str, keywords_by_role: dict[int, list[str]],
                    roles_by_id: dict) -> list[tuple[int, object]]:
    """(longest matching keyword length, role) for each enabled role whose keyword hits."""
    folded = re.sub(r"\s+", " ", title)
    out = []
    for role_row_id, keywords in keywords_by_role.items():
        role = roles_by_id.get(role_row_id)
        if role is None or not role.enabled:
            continue
        best = 0
        for kw in keywords:
            kw = " ".join(kw.split())
            # Lookarounds instead of \b so keywords ending in punctuation still match.
            if kw and len(kw) > best and re.search(rf"(?<!\w){re.escape(kw)}(?!\w)", folded, re.I):
                best = len(kw)
        if best:
            out.append((best, role))
    return out


def match_roles_for_title(title: str, keywords_by_role: dict[int, list[str]],
                          roles_by_id: dict) -> list:
    return [role for _, role in _ranked_matches(title, keywords_by_role, roles_by_id)]


def _already_started(event, tz, now: datetime) -> bool:
    if event.starts_at is None:
        return False
    local = event.starts_at.astimezone(tz)
    if event.time_known:
        return local < now
    return local.date() < now.date()


async def _resolve_channel(bot, channel_id: int):
    channel = bot.get_channel(channel_id)
    if channel is not None:
        return channel
    try:
        return await bot.fetch_channel(channel_id)
    except discord.HTTPException:
        log.warning("announce channel %s unavailable", channel_id)
        return None


def _pending_pairs(session: Session, guild_id: int, tz, now: datetime,
                   keywords: dict, roles_by_id: dict) -> list[tuple[Event, object]]:
    """Unpinged (event, role) pairs for recent, not-yet-started events, most relevant first."""
    cutoff = now.astimezone(timezone.utc) - PENDING_WINDOW
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    events = session.execute(
        select(Event)
        .where(Event.guild_id == guild_id, Event.is_active.is_(True),
               Event.first_seen_at >= cutoff,
               (Event.starts_at.is_(None)) | (Event.starts_at >= day_start.astimezone(timezone.utc)))
        .order_by(Event.starts_at.is_(None), Event.starts_at, Event.id)
    ).scalars().all()
    events = [e for e in events if not _already_started(e, tz, now)]
    if not events:
        return []
    pinged = set(session.execute(
        select(EventRolePing.event_id, EventRolePing.interest_role_id)
        .where(EventRolePing.event_id.in_([e.id for e in events]))).all())
    pairs = []
    for event in events:
        ranked = sorted(_ranked_matches(event.title, keywords, roles_by_id),
                        key=lambda m: -m[0])[:MAX_ROLES_PER_EVENT]
        pairs.extend((event, role) for _, role in ranked if (event.id, role.id) not in pinged)
    return pairs


async def ping_new_events(bot, session: Session, guild_id: int, tz, *, suppress: bool = False) -> int:
    if suppress:
        return 0
    cfg = get_or_create_config(session, guild_id)
    if cfg.announce_channel_id is None:
        log.warning("no announce channel configured for guild %s; skipping pings", guild_id)
        return 0
    guild = bot.get_guild(guild_id)
    if guild is None:
        log.warning("guild %s not available; skipping pings", guild_id)
        return 0
    roles_by_id = {r.id: r for r in list_interest_roles(session, guild_id)}
    keywords = get_keywords_by_role(session, guild_id)
    pairs = _pending_pairs(session, guild_id, tz, _now(tz), keywords, roles_by_id)
    if not pairs:
        return 0
    channel = await _resolve_channel(bot, cfg.announce_channel_id)
    if channel is None:
        return 0
    sent = 0
    attempted = 0
    pinged_events: set[int] = set()
    overflow_events: set[int] = set()
    for event, role_row in pairs:
        if attempted >= MAX_PINGS_PER_RUN:
            overflow_events.add(event.id)
            continue
        role = guild.get_role(role_row.role_id)
        if role is None:
            log.warning("role %s for interest role %s no longer exists; skipping ping",
                        role_row.role_id, role_row.id)
            continue
        attempted += 1
        try:
            await channel.send(
                build_ping_message(event, role_row.role_id, tz)[:MAX_MESSAGE_CHARS],
                allowed_mentions=discord.AllowedMentions(
                    everyone=False, users=False, roles=[role], replied_user=False),
            )
        except discord.HTTPException:
            log.exception("failed to send ping for event %s role %s", event.id, role_row.id)
            continue
        record_ping(session, guild_id, event.id, role_row.id)
        pinged_events.add(event.id)
        sent += 1
    extra = len(overflow_events - pinged_events)
    if extra:
        try:
            await channel.send(f"➕ {extra} more new events this week — check the boards.",
                               allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.exception("failed to send ping overflow digest")
    return sent
