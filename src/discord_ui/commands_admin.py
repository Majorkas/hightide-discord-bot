import re
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select
from sqlalchemy.orm import Session

from src import config
from src.db.models import EventSource
from src.db.repositories import get_or_create_config
from src.db.session import get_session_factory

_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_MAX_URL_LEN = 500

# Created lazily; tests can monkeypatch or reset to None.
_session_factory = None


def _session() -> Session:
    global _session_factory
    if _session_factory is None:
        _session_factory = get_session_factory(config.DATABASE_URL)
    return _session_factory()


def _valid_source_url(url: str) -> bool:
    if len(url) > _MAX_URL_LEN:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    config_group = app_commands.Group(
        name="config", description="Bot configuration",
        default_permissions=discord.Permissions(manage_guild=True),
        guild_only=True,
    )
    source_group = app_commands.Group(
        name="source", description="Event sources",
        default_permissions=discord.Permissions(manage_guild=True),
        guild_only=True,
    )
    board_group = app_commands.Group(
        name="board", description="Event boards",
        default_permissions=discord.Permissions(manage_guild=True),
        guild_only=True,
    )

    @config_group.command(name="this_week_channel")
    async def set_week_channel(self, interaction: discord.Interaction,
                               channel: discord.TextChannel) -> None:
        with _session() as session:
            cfg = get_or_create_config(session, interaction.guild_id)
            cfg.this_week_channel_id = channel.id
            session.commit()
        await interaction.response.send_message(
            f"this-week channel set to {channel.mention}", ephemeral=True)

    @config_group.command(name="this_month_channel")
    async def set_month_channel(self, interaction: discord.Interaction,
                                channel: discord.TextChannel) -> None:
        with _session() as session:
            cfg = get_or_create_config(session, interaction.guild_id)
            cfg.this_month_channel_id = channel.id
            session.commit()
        await interaction.response.send_message(
            f"this-month channel set to {channel.mention}", ephemeral=True)

    @config_group.command(name="timezone")
    async def set_timezone(self, interaction: discord.Interaction, tz: str) -> None:
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            await interaction.response.send_message(
                f"Unknown timezone: {tz}. Use an IANA name like Australia/Perth.",
                ephemeral=True)
            return
        with _session() as session:
            cfg = get_or_create_config(session, interaction.guild_id)
            cfg.timezone = tz
            session.commit()
        await interaction.response.send_message(f"timezone set to {tz}", ephemeral=True)

    @config_group.command(name="daily_refresh_time")
    async def set_refresh_time(self, interaction: discord.Interaction, hhmm: str) -> None:
        if not _HHMM_RE.match(hhmm):
            await interaction.response.send_message(
                "Invalid time. Use 24-hour HH:MM, e.g. 09:00.", ephemeral=True)
            return
        with _session() as session:
            cfg = get_or_create_config(session, interaction.guild_id)
            cfg.daily_refresh_time = hhmm
            session.commit()
        await interaction.response.send_message(
            f"daily refresh set to {hhmm}", ephemeral=True)

    @config_group.command(name="include_tba")
    async def set_include_tba(self, interaction: discord.Interaction, value: bool) -> None:
        with _session() as session:
            cfg = get_or_create_config(session, interaction.guild_id)
            cfg.include_tba_section = value
            session.commit()
        await interaction.response.send_message(
            f"include_tba_section = {value}", ephemeral=True)

    @source_group.command(name="list")
    async def source_list(self, interaction: discord.Interaction) -> None:
        with _session() as session:
            rows = session.execute(select(EventSource).where(
                EventSource.guild_id == interaction.guild_id)).scalars().all()
            lines = [f"`{r.id}` {'✅' if r.enabled else '⛔'} {r.url}" for r in rows]
        if not lines:
            await interaction.response.send_message("No sources configured.", ephemeral=True)
            return
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @source_group.command(name="add")
    async def source_add(self, interaction: discord.Interaction, url: str) -> None:
        if not _valid_source_url(url):
            await interaction.response.send_message(
                f"Invalid URL. Use an http(s) URL up to {_MAX_URL_LEN} characters.",
                ephemeral=True)
            return
        with _session() as session:
            session.add(EventSource(guild_id=interaction.guild_id, url=url, enabled=True))
            session.commit()
        await interaction.response.send_message(f"Added source: {url}", ephemeral=True)

    @source_group.command(name="remove")
    async def source_remove(self, interaction: discord.Interaction, source_id: int) -> None:
        with _session() as session:
            row = session.get(EventSource, source_id)
            removed = bool(row and row.guild_id == interaction.guild_id)
            if removed:
                session.delete(row)
                session.commit()
        if removed:
            await interaction.response.send_message(
                f"Removed source {source_id}", ephemeral=True)
        else:
            await interaction.response.send_message("Source not found.", ephemeral=True)

    @board_group.command(name="preview")
    @app_commands.choices(board_type=[
        app_commands.Choice(name="this_week", value="this_week"),
        app_commands.Choice(name="this_month", value="this_month"),
    ])
    async def board_preview(self, interaction: discord.Interaction,
                            board_type: str) -> None:
        from zoneinfo import ZoneInfo
        from src.db.repositories import get_active_events, get_or_create_config
        from src.discord_ui.embeds import build_board_embed
        from src.services.board_service import filter_events_for_board

        try:
            with _session() as session:
                cfg = get_or_create_config(session, interaction.guild_id)
                tz = ZoneInfo(cfg.timezone)
                events = filter_events_for_board(
                    get_active_events(session, interaction.guild_id), board_type, cfg, tz)
                embed = build_board_embed(events, board_type.replace("_", " ").title(),
                                          tz, cfg.include_tba_section)
        except Exception as e:
            import logging
            logging.getLogger("hightide").exception("board preview failed")
            await interaction.response.send_message(
                f"Preview failed: {type(e).__name__}", ephemeral=True)
            return
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @board_group.command(name="refresh")
    async def board_refresh(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        from src.services.scheduler import run_daily_refresh
        try:
            result = await run_daily_refresh(self.bot, interaction.guild_id)
            await interaction.followup.send(f"Refresh complete: `{result}`", ephemeral=True)
        except Exception as e:
            import logging
            logging.getLogger("hightide").exception("board refresh failed")
            await interaction.followup.send(
                f"Refresh failed: {type(e).__name__}",
                ephemeral=True
            )

    @board_group.command(name="recreate")
    @app_commands.choices(board_type=[
        app_commands.Choice(name="this_week", value="this_week"),
        app_commands.Choice(name="this_month", value="this_month"),
    ])
    async def board_recreate(self, interaction: discord.Interaction,
                             board_type: str) -> None:
        await interaction.response.defer(ephemeral=True)
        from src.services.board_service import delete_board, upsert_board
        try:
            with _session() as session:
                await delete_board(self.bot, session, interaction.guild_id, board_type)
                action = await upsert_board(self.bot, session, interaction.guild_id, board_type)
        except Exception as e:
            import logging
            logging.getLogger("hightide").exception("board recreate failed")
            await interaction.followup.send(
                f"Recreate failed: {type(e).__name__}", ephemeral=True)
            return
        await interaction.followup.send(
            f"{board_type} board action: {action}", ephemeral=True)
