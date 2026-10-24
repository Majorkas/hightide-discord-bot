import logging

import discord
from sqlalchemy.orm import Session

from src import config
from src.db.models import InterestRole
from src.db.session import get_session_factory

log = logging.getLogger("hightide.roles")

_session_factory = None


def _session() -> Session:
    global _session_factory
    if _session_factory is None:
        _session_factory = get_session_factory(config.DATABASE_URL)
    return _session_factory()


async def handle_role_toggle(interaction: discord.Interaction, role_row_id: int) -> None:
    with _session() as session:
        row = session.get(InterestRole, role_row_id)
        if row is None or not row.enabled or row.guild_id != interaction.guild_id:
            await interaction.response.send_message(
                "That role isn't available any more.", ephemeral=True)
            return
        role_id, label = row.role_id, row.label
    label = discord.utils.escape_mentions(label)
    role = interaction.guild.get_role(role_id)
    member = interaction.user
    if role is None:
        await interaction.response.send_message(
            f"The **{label}** role no longer exists — tell an admin.", ephemeral=True)
        return
    try:
        if role in member.roles:
            await member.remove_roles(role, reason="interest role toggle")
            await interaction.response.send_message(f"Removed **{label}**.", ephemeral=True)
        else:
            await member.add_roles(role, reason="interest role toggle")
            await interaction.response.send_message(
                f"Added **{label}** — you'll be pinged for new events.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message(
            "I can't manage that role (check my role sits above it). Tell an admin.",
            ephemeral=True)
    except discord.HTTPException:
        await interaction.response.send_message(
            "Something went wrong — try again.", ephemeral=True)
