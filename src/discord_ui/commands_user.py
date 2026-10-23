import logging
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy.orm import Session

from src import config
from src.db.repositories import get_active_events, get_or_create_config
from src.db.session import get_session_factory
from src.discord_ui.embeds import build_board_embed
from src.services.board_service import filter_events_for_board

log = logging.getLogger("hightide")

# Created lazily; tests can monkeypatch or reset to None.
_session_factory = None


def _session() -> Session:
    global _session_factory
    if _session_factory is None:
        _session_factory = get_session_factory(config.DATABASE_URL)
    return _session_factory()


class UserCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    events_group = app_commands.Group(
        name="events", description="Hightide events", guild_only=True,
    )

    @events_group.command(name="upcoming", description="Events happening this week")
    async def events_upcoming(self, interaction: discord.Interaction) -> None:
        try:
            with _session() as session:
                cfg = get_or_create_config(session, interaction.guild_id)
                tz = ZoneInfo(cfg.timezone)
                events = filter_events_for_board(
                    get_active_events(session, interaction.guild_id),
                    "this_week", cfg, tz)
                embed = build_board_embed(events, "Upcoming Events", tz,
                                          cfg.include_tba_section)
        except Exception as e:
            log.exception("events upcoming failed")
            await interaction.response.send_message(
                f"Could not load events: {type(e).__name__}", ephemeral=True)
            return
        await interaction.response.send_message(embed=embed, ephemeral=True)
