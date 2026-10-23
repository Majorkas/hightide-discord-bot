import discord
from discord import app_commands
from discord.ext import commands

from src import config
from src.db.repositories import get_or_create_config
from src.db.session import get_session_factory


def _session():
    return get_session_factory(config.DATABASE_URL)()


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    config_group = app_commands.Group(
        name="config", description="Bot configuration",
        default_permissions=discord.Permissions(manage_guild=True),
    )
    source_group = app_commands.Group(
        name="source", description="Event sources",
        default_permissions=discord.Permissions(manage_guild=True),
    )
    board_group = app_commands.Group(
        name="board", description="Event boards",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @config_group.command(name="this_week_channel")
    async def set_week_channel(self, interaction: discord.Interaction,
                               channel: discord.TextChannel) -> None:
        session = _session()
        cfg = get_or_create_config(session, interaction.guild_id)
        cfg.this_week_channel_id = channel.id
        session.commit()
        await interaction.response.send_message(
            f"this-week channel set to {channel.mention}", ephemeral=True)

    @config_group.command(name="this_month_channel")
    async def set_month_channel(self, interaction: discord.Interaction,
                                channel: discord.TextChannel) -> None:
        session = _session()
        cfg = get_or_create_config(session, interaction.guild_id)
        cfg.this_month_channel_id = channel.id
        session.commit()
        await interaction.response.send_message(
            f"this-month channel set to {channel.mention}", ephemeral=True)

    @config_group.command(name="timezone")
    async def set_timezone(self, interaction: discord.Interaction, tz: str) -> None:
        session = _session()
        cfg = get_or_create_config(session, interaction.guild_id)
        cfg.timezone = tz
        session.commit()
        await interaction.response.send_message(f"timezone set to {tz}", ephemeral=True)

    @config_group.command(name="daily_refresh_time")
    async def set_refresh_time(self, interaction: discord.Interaction, hhmm: str) -> None:
        session = _session()
        cfg = get_or_create_config(session, interaction.guild_id)
        cfg.daily_refresh_time = hhmm
        session.commit()
        await interaction.response.send_message(
            f"daily refresh set to {hhmm}", ephemeral=True)

    @config_group.command(name="include_tba")
    async def set_include_tba(self, interaction: discord.Interaction, value: bool) -> None:
        session = _session()
        cfg = get_or_create_config(session, interaction.guild_id)
        cfg.include_tba_section = value
        session.commit()
        await interaction.response.send_message(
            f"include_tba_section = {value}", ephemeral=True)

    @source_group.command(name="list")
    async def source_list(self, interaction: discord.Interaction) -> None:
        from sqlalchemy import select
        from src.db.models import EventSource
        session = _session()
        rows = session.execute(select(EventSource).where(
            EventSource.guild_id == interaction.guild_id)).scalars().all()
        if not rows:
            await interaction.response.send_message("No sources configured.", ephemeral=True)
            return
        lines = [f"`{r.id}` {'✅' if r.enabled else '⛔'} {r.url}" for r in rows]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @source_group.command(name="add")
    async def source_add(self, interaction: discord.Interaction, url: str) -> None:
        from src.db.models import EventSource
        session = _session()
        session.add(EventSource(guild_id=interaction.guild_id, url=url, enabled=True))
        session.commit()
        await interaction.response.send_message(f"Added source: {url}", ephemeral=True)

    @source_group.command(name="remove")
    async def source_remove(self, interaction: discord.Interaction, source_id: int) -> None:
        from src.db.models import EventSource
        session = _session()
        row = session.get(EventSource, source_id)
        if row and row.guild_id == interaction.guild_id:
            session.delete(row)
            session.commit()
            await interaction.response.send_message(f"Removed source {source_id}",
                                                    ephemeral=True)
        else:
            await interaction.response.send_message("Source not found.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))
