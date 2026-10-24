import logging
import re
from datetime import datetime

import discord
from sqlalchemy.orm import Session

from src import config
from src.db.models import InterestRole
from src.db.repositories import (
    get_keywords_by_role, get_or_create_config, has_ping, list_interest_roles, record_ping,
)
from src.db.session import get_session_factory
from src.discord_ui.embeds import build_ping_message

log = logging.getLogger("hightide.roles")

_session_factory = None


def _session() -> Session:
    global _session_factory
    if _session_factory is None:
        _session_factory = get_session_factory(config.DATABASE_URL)
    return _session_factory()


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
            await member.add_roles(role, reason="interest role toggle")
            msg = f"Added **{label}** — you'll be pinged for new events."
    except discord.Forbidden:
        msg = "I can't manage that role (check my role sits above it). Tell an admin."
    except discord.HTTPException:
        msg = "Something went wrong — try again."
    await interaction.followup.send(msg, ephemeral=True)


def match_roles_for_title(title: str, keywords_by_role: dict[int, list[str]],
                          roles_by_id: dict) -> list:
    matched = []
    for role_row_id, keywords in keywords_by_role.items():
        role = roles_by_id.get(role_row_id)
        if role is None or not role.enabled:
            continue
        # Lookarounds instead of \b so keywords ending in punctuation still match.
        if any(re.search(rf"(?<!\w){re.escape(kw)}(?!\w)", title, re.I) for kw in keywords if kw):
            matched.append(role)
    return matched


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


async def ping_new_events(bot, session: Session, guild_id: int, created_events: list, tz) -> int:
    if not created_events:
        return 0
    cfg = get_or_create_config(session, guild_id)
    if cfg.announce_channel_id is None:
        return 0
    guild = bot.get_guild(guild_id)
    if guild is None:
        log.warning("guild %s not available; skipping pings", guild_id)
        return 0
    roles_by_id = {r.id: r for r in list_interest_roles(session, guild_id)}
    keywords = get_keywords_by_role(session, guild_id)
    channel = await _resolve_channel(bot, cfg.announce_channel_id)
    if channel is None:
        return 0
    now = datetime.now(tz)
    sent = 0
    for event in created_events:
        if _already_started(event, tz, now):
            continue
        for role_row in match_roles_for_title(event.title, keywords, roles_by_id):
            if has_ping(session, event.id, role_row.id):
                continue
            role = guild.get_role(role_row.role_id)
            if role is None:
                log.warning("role %s for interest role %s no longer exists; skipping ping",
                            role_row.role_id, role_row.id)
                continue
            try:
                await channel.send(
                    build_ping_message(event, role_row.role_id, tz),
                    allowed_mentions=discord.AllowedMentions(
                        everyone=False, users=False, roles=[role], replied_user=False),
                )
            except discord.HTTPException:
                log.exception("failed to send ping for event %s role %s", event.id, role_row.id)
                continue
            record_ping(session, guild_id, event.id, role_row.id)
            sent += 1
    return sent
