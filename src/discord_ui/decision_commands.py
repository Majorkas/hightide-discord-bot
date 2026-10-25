"""Admin /decision commands for the decision-tracking bot."""
import logging
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy.orm import Session

from src import config
from src.db.session import get_session_factory
from src.discord_ui.decision_views import _esc
from src.db.repositories import (
    add_decision_channel, get_or_create_decision_config, get_trello_member,
    list_decision_channels, list_decisions, remove_decision_channel,
    remove_trello_member, set_trello_member,
)
from src.services import decision_service
from src.services.decision_service import message_link

log = logging.getLogger("hightide.decision_commands")

_MAX_MESSAGE_CHARS = 1900

# Created lazily; tests can monkeypatch or reset to None.
_session_factory = None


def _session() -> Session:
    global _session_factory
    if _session_factory is None:
        _session_factory = get_session_factory(config.DATABASE_URL)
    return _session_factory()


def _decision_line(d, tz) -> str:
    when = d.created_at.astimezone(tz).strftime("%b %d %H:%M")
    return (f"#{d.id} [{d.status}] **{_esc(d.summary)}** — <@{d.user_id}> · {when}")


class DecisionCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    decision_group = app_commands.Group(
        name="decision", description="Decision tracking",
        default_permissions=discord.Permissions(manage_guild=True),
        guild_only=True,
    )

    @decision_group.command(name="monitor", description="Monitor a channel for decisions")
    async def monitor(self, interaction: discord.Interaction,
                      channel: discord.TextChannel) -> None:
        with _session() as session:
            add_decision_channel(session, interaction.guild_id, channel.id)
            cfg = get_or_create_decision_config(session, interaction.guild_id)
            state = "enabled" if cfg.enabled else "disabled (run /decision config on)"
        await interaction.response.send_message(
            f"Monitoring {channel.mention} for decisions — the bot is currently {state}.",
            ephemeral=True)

    @decision_group.command(name="unmonitor", description="Stop monitoring a channel")
    async def unmonitor(self, interaction: discord.Interaction,
                        channel: discord.TextChannel) -> None:
        with _session() as session:
            removed = remove_decision_channel(session, interaction.guild_id, channel.id)
        msg = (f"Stopped monitoring {channel.mention}." if removed
               else f"{channel.mention} wasn't being monitored.")
        await interaction.response.send_message(msg, ephemeral=True)

    @decision_group.command(name="config",
                            description="Set the Trello target or toggle the bot")
    async def decision_config(self, interaction: discord.Interaction,
                              setting: str,
                              board_id: str | None = None,
                              list_id: str | None = None) -> None:
        setting = (setting or "").strip().lower()
        if setting not in ("on", "off", "board"):
            await interaction.response.send_message(
                "setting must be on, off, or board.", ephemeral=True)
            return
        with _session() as session:
            cfg = get_or_create_decision_config(session, interaction.guild_id)
            if setting == "board":
                if not (board_id and list_id):
                    await interaction.response.send_message(
                        "Provide both board_id and list_id.", ephemeral=True)
                    return
                cfg.trello_board_id = board_id.strip()
                cfg.trello_list_id = list_id.strip()
                session.commit()
                await interaction.response.send_message(
                    "Trello target set.", ephemeral=True)
                return
            cfg.enabled = setting == "on"
            session.commit()
            await interaction.response.send_message(
                f"Decision tracking {'enabled' if cfg.enabled else 'disabled'}.",
                ephemeral=True)

    @decision_config.autocomplete("setting")
    async def _setting_ac(self, interaction, current: str):
        opts = ["on", "off", "board"]
        return [app_commands.Choice(name=o, value=o) for o in opts
                if current.lower() in o]

    @decision_group.command(name="map", description="Map a Discord user to a Trello member")
    async def map_member(self, interaction: discord.Interaction, user: discord.Member,
                         trello_member_id: str) -> None:
        with _session() as session:
            set_trello_member(session, interaction.guild_id, user.id,
                              trello_member_id.strip())
        await interaction.response.send_message(
            f"Mapped {user.mention} to Trello member `{_esc(trello_member_id.strip())}`.",
            ephemeral=True)

    @decision_group.command(name="unmap", description="Remove a Discord→Trello mapping")
    async def unmap_member(self, interaction: discord.Interaction,
                           user: discord.Member) -> None:
        with _session() as session:
            removed = remove_trello_member(session, interaction.guild_id, user.id)
        msg = (f"Removed the Trello mapping for {user.mention}." if removed
               else f"{user.mention} had no Trello mapping.")
        await interaction.response.send_message(msg, ephemeral=True)

    @decision_group.command(name="list", description="List decisions by status")
    async def list_cmd(self, interaction: discord.Interaction,
                       status: str | None = None) -> None:
        status = (status or "").strip().lower() or None
        if status is not None and status not in ("pending", "confirmed", "rejected"):
            await interaction.response.send_message(
                "status must be pending, confirmed, or rejected.", ephemeral=True)
            return
        with _session() as session:
            cfg = get_or_create_decision_config(session, interaction.guild_id)
            tz = ZoneInfo(config.DEFAULT_TIMEZONE)
            rows = list_decisions(session, interaction.guild_id, status)
        if not rows:
            await interaction.response.send_message("No decisions found.", ephemeral=True)
            return
        lines = [_decision_line(d, tz) for d in rows[:25]]
        text = "\n".join(lines)
        if len(text) > _MAX_MESSAGE_CHARS:
            text = text[:_MAX_MESSAGE_CHARS - 20].rsplit("\n", 1)[0] + "\n…"
        await interaction.response.send_message(text, ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    @list_cmd.autocomplete("status")
    async def _status_ac(self, interaction, current: str):
        return [app_commands.Choice(name=s, value=s) for s in
                ("pending", "confirmed", "rejected") if current.lower() in s]

    @decision_group.command(name="create", description="Manually log + sync a decision")
    async def create(self, interaction: discord.Interaction, summary: str,
                     channel: discord.TextChannel | None = None) -> None:
        await interaction.response.defer(ephemeral=True)
        chan = channel or interaction.channel
        with _session() as session:
            d = decision_service.create_pending_decision(
                session, interaction.guild_id, chan.id, interaction.id or 0,
                interaction.user.id, summary, summary)
            decision_service.confirm_decision(session, interaction.guild_id, d.id,
                                              confirmed_by=interaction.user.id)
            member = interaction.user
            channel_name = f"#{chan.name}" if chan else "a staff channel"
            synced = await decision_service.sync_decision_to_trello(
                session, d, decider_name=member.display_name, channel_name=channel_name)
        note = " and synced to Trello" if synced else " (Trello sync failed or not configured)"
        await interaction.followup.send(
            f"Logged decision #{d.id}: **{_esc(d.summary)}**{note}.", ephemeral=True)

    @decision_group.command(name="sync",
                            description="Retry syncing confirmed-but-unsynced decisions")
    async def sync(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        with _session() as session:
            pending = decision_service.unsynced_confirmed(session, interaction.guild_id)
            if not pending:
                await interaction.followup.send("Nothing to sync.", ephemeral=True)
                return
            synced = 0
            for d in pending:
                member = interaction.guild.get_member(d.user_id) if interaction.guild else None
                channel = interaction.guild.get_channel(d.channel_id) if interaction.guild else None
                ok = await decision_service.sync_decision_to_trello(
                    session, d,
                    decider_name=(member.display_name if member else f"User {d.user_id}"),
                    channel_name=(f"#{channel.name}" if channel else "a staff channel"))
                synced += 1 if ok else 0
        await interaction.followup.send(
            f"Synced {synced}/{len(pending)} decision(s) to Trello.", ephemeral=True)
