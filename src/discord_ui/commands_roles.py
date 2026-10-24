from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src import config
from src.db.models import InterestRole
from src.db.repositories import (
    add_interest_role, add_keyword, get_keywords_by_role, get_or_create_config,
    list_interest_roles, remove_interest_role, remove_keyword,
)
from src.db.session import get_session_factory
from src.discord_ui.role_views import (
    MAX_PANEL_ROLES, RolesPanelView, build_roles_panel_embed,
)

_MAX_LABEL_LEN = 60
_MAX_EMOJI_LEN = 64
_MAX_KEYWORD_LEN = 100
_MAX_MESSAGE_CHARS = 1900
_PANEL_GONE_NOTE = " The panel message is gone — run /roles setup to post it again."
_PANEL_FORBIDDEN_NOTE = (" I couldn't update the panel (missing permission to view or edit "
                         "it) — check my channel permissions.")
_PANEL_FAILED_NOTE = " Discord rejected the panel update — check the role emoji."

# Created lazily; tests can monkeypatch or reset to None.
_session_factory = None


def _session() -> Session:
    global _session_factory
    if _session_factory is None:
        _session_factory = get_session_factory(config.DATABASE_URL)
    return _session_factory()


def _esc(text: str) -> str:
    return discord.utils.escape_mentions(text)


def _valid_emoji(emoji: str) -> bool:
    """Accept only custom emoji (<:name:id> or <a:name:id>) or Unicode emoji grapheme(s).
    Reject non-emoji Unicode like é, 日本, mixed text like Hi😀, or plain ASCII like ab."""
    if len(emoji) > _MAX_EMOJI_LEN:
        return False
    # Custom emoji: <:name:id> or <a:name:id>
    if emoji.startswith('<') and emoji.endswith('>'):
        import re
        return bool(re.match(r'^<a?:[\w-]+:\d+>$', emoji))
    # Reject pure ASCII (catches "ab", "abc")
    if emoji.isascii():
        return False
    # For Unicode, verify at least one char is from emoji blocks and no stray letters mixed in.
    # Allows emoji sequences with variation selectors, ZWJ, modifiers.
    emoji_codepoint_ranges = [
        (0x1F300, 0x1F9FF),  # Main emoji blocks (emoticons, symbols, transport, etc.)
        (0x2600, 0x27BF),    # Miscellaneous symbols and dingbats
        (0x1F1E6, 0x1F1FF),  # Regional indicators for flags
        (0x2300, 0x23FF),    # Miscellaneous technical
    ]
    allowed_helper_ranges = [
        (0x20D0, 0x20FF),    # Combining diacritical marks
        (0xFE00, 0xFE0F),    # Variation selectors
        (0x200D, 0x200D),    # Zero-width joiner
        (0x1F3FB, 0x1F3FF),  # Emoji modifiers (skin tones)
    ]
    has_emoji_codepoint = False
    for char in emoji:
        ord_val = ord(char)
        if any(start <= ord_val <= end for start, end in emoji_codepoint_ranges):
            has_emoji_codepoint = True
        elif not any(start <= ord_val <= end for start, end in allowed_helper_ranges):
            # Stray character not in emoji range and not a helper (e.g., regular letter)
            # Rejects é, 日本, Hi😀
            return False
    return has_emoji_codepoint


async def _refresh_panel(guild: discord.Guild, session: Session, guild_id: int) -> str:
    """Edit the stored panel in place; returns a note to append to the reply ('' if fine)."""
    cfg = get_or_create_config(session, guild_id)
    if cfg.roles_channel_id is None or cfg.roles_message_id is None:
        return ""
    channel = guild.get_channel(cfg.roles_channel_id)
    message = None
    if channel is not None:
        try:
            message = await channel.fetch_message(cfg.roles_message_id)
        except discord.NotFound:
            message = None
        except discord.Forbidden:
            return _PANEL_FORBIDDEN_NOTE
        except discord.HTTPException:
            return _PANEL_FAILED_NOTE
    if message is None:
        cfg.roles_channel_id = None
        cfg.roles_message_id = None
        session.commit()
        return _PANEL_GONE_NOTE
    roles = list_interest_roles(session, guild_id)
    try:
        await message.edit(embed=build_roles_panel_embed(roles), view=RolesPanelView(roles))
    except discord.Forbidden:
        return _PANEL_FORBIDDEN_NOTE
    except discord.NotFound:
        cfg.roles_channel_id = None
        cfg.roles_message_id = None
        session.commit()
        return _PANEL_GONE_NOTE
    except discord.HTTPException:
        cfg.roles_channel_id = None
        cfg.roles_message_id = None
        session.commit()
        return _PANEL_FAILED_NOTE
    return ""


class RolesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    roles_group = app_commands.Group(
        name="roles", description="Interest roles and the roles panel",
        default_permissions=discord.Permissions(manage_guild=True),
        guild_only=True,
    )

    @roles_group.command(name="setup", description="Post (or update) the roles panel")
    async def setup(self, interaction: discord.Interaction,
                    channel: discord.TextChannel) -> None:
        with _session() as session:
            cfg = get_or_create_config(session, interaction.guild_id)
            roles = list_interest_roles(session, interaction.guild_id)
            embed = build_roles_panel_embed(roles)
            view = RolesPanelView(roles)
            
            # Delete old panel if switching to a different channel
            old_panel_deleted = False
            if cfg.roles_message_id and cfg.roles_channel_id and cfg.roles_channel_id != channel.id:
                old_channel = interaction.guild.get_channel(cfg.roles_channel_id)
                if old_channel is not None:
                    try:
                        old_msg = await old_channel.fetch_message(cfg.roles_message_id)
                        await old_msg.delete()
                        old_panel_deleted = True
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        pass  # Best-effort cleanup
            
            http_error = None  # Track HTTPException to handle separately from NotFound
            http_error_type = None
            try:
                message = None
                # Try to fetch existing message in the target channel
                if cfg.roles_message_id and cfg.roles_channel_id == channel.id:
                    try:
                        message = await channel.fetch_message(cfg.roles_message_id)
                    except discord.NotFound:
                        message = None
                    except discord.Forbidden as e:
                        http_error = e
                        http_error_type = "forbidden"
                    except discord.HTTPException as e:
                        http_error = e
                        http_error_type = "http"
                
                # Try to edit existing message if found and no HTTP error
                if message is not None and http_error is None:
                    try:
                        await message.edit(embed=embed, view=view)
                    except discord.NotFound:
                        message = None
                    except discord.Forbidden as e:
                        http_error = e
                        http_error_type = "forbidden"
                    except discord.HTTPException as e:
                        http_error = e
                        http_error_type = "http"
                
                # If we got an HTTP error during fetch/edit, clear IDs and error out
                if http_error:
                    cfg.roles_channel_id = None
                    cfg.roles_message_id = None
                    session.commit()
                    if http_error_type == "forbidden":
                        msg = f"I don't have permission to access the panel — check my channel permissions."
                    else:
                        msg = "Discord rejected the panel update (check the role emoji)."
                    if old_panel_deleted:
                        msg += " The old panel was removed."
                    await interaction.response.send_message(msg, ephemeral=True)
                    return
                
                # Send new message if edit failed or didn't exist
                if message is None:
                    message = await channel.send(embed=embed, view=view)
            except discord.Forbidden:
                cfg.roles_channel_id = None
                cfg.roles_message_id = None
                session.commit()
                await interaction.response.send_message(
                    f"I don't have permission to post in {channel.mention}.", ephemeral=True)
                return
            except discord.HTTPException:
                cfg.roles_channel_id = None
                cfg.roles_message_id = None
                session.commit()
                msg = "Discord rejected the panel update (check the role emoji)."
                if old_panel_deleted:
                    msg += " The old panel was removed."
                await interaction.response.send_message(msg, ephemeral=True)
                return
            
            cfg.roles_channel_id = channel.id
            cfg.roles_message_id = message.id
            session.commit()
        
        msg = f"Roles panel is live in {channel.mention}."
        if old_panel_deleted:
            msg += " The old panel was removed."
        await interaction.response.send_message(msg, ephemeral=True)

    @roles_group.command(name="add", description="Add an interest role to the panel")
    async def add(self, interaction: discord.Interaction, role: discord.Role,
                  label: str, emoji: str | None = None) -> None:
        label = label.strip()
        if not label or len(label) > _MAX_LABEL_LEN:
            await interaction.response.send_message(
                f"Label must be 1-{_MAX_LABEL_LEN} characters.", ephemeral=True)
            return
        emoji = emoji.strip() if emoji else None
        if emoji and not _valid_emoji(emoji):
            await interaction.response.send_message(
                "Emoji must be a single Unicode emoji or a custom emoji like <:name:id>.",
                ephemeral=True)
            return
        with _session() as session:
            if len(list_interest_roles(session, interaction.guild_id)) >= MAX_PANEL_ROLES:
                await interaction.response.send_message(
                    f"The panel supports at most {MAX_PANEL_ROLES} roles.", ephemeral=True)
                return
            try:
                row = add_interest_role(session, interaction.guild_id, role.id, label, emoji)
            except IntegrityError:
                session.rollback()
                await interaction.response.send_message(
                    "That role is already on the panel.", ephemeral=True)
                return
            row_id = row.id
            note = await _refresh_panel(interaction.guild, session, interaction.guild_id)
        await interaction.response.send_message(
            f"Added **{_esc(label)}** (id {row_id}).{note}", ephemeral=True)

    @roles_group.command(name="remove", description="Remove an interest role (by id from /roles list)")
    async def remove(self, interaction: discord.Interaction, row_id: int) -> None:
        with _session() as session:
            if not remove_interest_role(session, interaction.guild_id, row_id):
                await interaction.response.send_message(
                    f"No role with id {row_id}. See /roles list.", ephemeral=True)
                return
            note = await _refresh_panel(interaction.guild, session, interaction.guild_id)
        await interaction.response.send_message(
            f"Removed role {row_id}.{note}", ephemeral=True)

    @roles_group.command(name="keyword", description="Add or remove a keyword that pings this role")
    async def keyword(self, interaction: discord.Interaction, row_id: int,
                      action: Literal["add", "remove"], keyword: str) -> None:
        if len(keyword.strip()) > _MAX_KEYWORD_LEN:
            await interaction.response.send_message(
                f"Keyword must be at most {_MAX_KEYWORD_LEN} characters.", ephemeral=True)
            return
        with _session() as session:
            row = session.get(InterestRole, row_id)
            if row is None or row.guild_id != interaction.guild_id:
                await interaction.response.send_message(
                    f"No role with id {row_id}. See /roles list.", ephemeral=True)
                return
            label = _esc(row.label)
            shown = _esc(keyword.strip().lower())
            if action == "add":
                try:
                    add_keyword(session, row_id, keyword)
                except ValueError:
                    await interaction.response.send_message(
                        "Keyword can't be empty.", ephemeral=True)
                    return
                except IntegrityError:
                    session.rollback()
                    await interaction.response.send_message(
                        f"**{label}** already has the keyword \"{shown}\".", ephemeral=True)
                    return
                msg = f"Added keyword \"{shown}\" to **{label}**."
            else:
                if not remove_keyword(session, row_id, keyword):
                    await interaction.response.send_message(
                        f"No keyword \"{shown}\" on **{label}**.", ephemeral=True)
                    return
                msg = f"Removed keyword \"{shown}\" from **{label}**."
        await interaction.response.send_message(msg, ephemeral=True)

    @roles_group.command(name="list", description="List interest roles and their keywords")
    async def list_roles(self, interaction: discord.Interaction) -> None:
        with _session() as session:
            roles = list_interest_roles(session, interaction.guild_id)
            keywords = get_keywords_by_role(session, interaction.guild_id)
            lines = [
                f"{r.id}: {(r.emoji + ' ') if r.emoji else ''}{_esc(r.label)} — role {r.role_id}"
                f" — keywords: {', '.join(_esc(k) for k in keywords.get(r.id, [])) or 'none'}"
                f"{'' if r.enabled else ' (disabled)'}"
                for r in roles
            ]
        if not lines:
            await interaction.response.send_message(
                "No roles configured. Add one with /roles add.", ephemeral=True)
            return
        text = "\n".join(lines)
        if len(text) > _MAX_MESSAGE_CHARS:
            text = text[:_MAX_MESSAGE_CHARS] + "\n…"
        await interaction.response.send_message(text, ephemeral=True)
