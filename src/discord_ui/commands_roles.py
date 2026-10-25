import asyncio
import json
import logging
import re
from typing import Literal
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands
import httpx
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src import config
from src.db.models import InterestRole
from src.db.repositories import (
    add_interest_role, add_keyword, get_keywords_by_role, get_or_create_config,
    list_interest_roles, remove_interest_role, remove_keyword,
)
from src.db.session import get_session_factory
from src.discord_ui.embeds import _md_escape, _truncate_lines, build_wants_embed
from src.discord_ui.role_views import (
    MAX_PANEL_ROLES, RolesPanelView, build_roles_panel_embed,
)
from src.services.deck_service import (
    AmbiguousCardError, build_card_embed, build_deck_embed, fetch_deck, parse_deck_url,
    scryfall_autocomplete, scryfall_card, scryfall_printings,
)
from src.services.role_service import role_rejection_reason
from src.services.want_service import (
    WantLimitError, add_want_from_printing, list_wants, remove_want, update_wants_board,
)

log = logging.getLogger("hightide.discord_ui.commands_roles")

_MAX_LABEL_LEN = 60
_MAX_EMOJI_LEN = 64
_MAX_KEYWORD_LEN = 100
_MAX_MESSAGE_CHARS = 1900
_MAX_CARD_NAME_LEN = 100
_MAX_DECK_URL_LEN = 500
_LOOKUP_TIMEOUT = 2.6
_LOOKUP_COOLDOWN_SECONDS = 5.0
_NOT_MENTIONABLE_NOTE = (
    "\n⚠️ This role isn't mentionable — pings won't notify anyone until an admin enables "
    "'Allow anyone to @mention this role' in role settings.")
_PANEL_GONE_NOTE = " The panel message is gone — run /roles setup to post it again."
_PANEL_FORBIDDEN_NOTE = (" I couldn't update the panel (missing permission to view or edit "
                         "it) — check my channel permissions.")
_PANEL_FAILED_NOTE = " Discord rejected the panel update — check the role emoji."

# Created lazily; tests can monkeypatch or reset to None.
_session_factory = None


def _printing_image_url(card: dict) -> str | None:
    uris = card.get("image_uris")
    if isinstance(uris, dict) and isinstance(uris.get("normal"), str) and uris["normal"]:
        return uris["normal"]
    faces = card.get("card_faces")
    if isinstance(faces, list) and faces and isinstance(faces[0], dict):
        f = faces[0].get("image_uris")
        if isinstance(f, dict) and isinstance(f.get("normal"), str):
            return f["normal"]
    return None


def _printing_option_label(card: dict) -> str:
    set_code = str(card.get("set", "")).upper() or "?"
    cn = str(card.get("collector_number", ""))
    return f"{set_code} #{cn}".strip()


class _PrintingPickerView(discord.ui.View):
    """Ephemeral picker: choose the exact printing, then confirm to post the want."""

    def __init__(self, bot, user_id: int, printings: list[dict], note: str | None):
        super().__init__(timeout=120)
        self.bot = bot
        self.user_id = user_id
        self.note = note
        # Show up to 25 printings (Discord select cap), preferring the most recent.
        self._printings = printings[:25]
        self._by_value: dict[str, dict] = {}
        options = []
        for i, card in enumerate(self._printings):
            value = str(i)
            self._by_value[value] = card
            options.append(discord.SelectOption(
                label=_printing_option_label(card)[:100],
                description=str(card.get("name", ""))[:100],
                value=value))
        select = discord.ui.Select(placeholder="Choose a printing…", options=options)
        select.callback = self._on_select
        self.add_item(select)
        self._selected = self._printings[0] if self._printings else None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "This picker isn't yours — run /want add yourself.", ephemeral=True)
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction) -> None:
        value = interaction.data["values"][0]
        self._selected = self._by_value.get(value)
        await interaction.response.edit_message(embed=self.preview_embed(), view=self)

    def preview_embed(self) -> discord.Embed:
        card = self._selected or {}
        name = card.get("name", "Unknown")
        embed = discord.Embed(title=f"Add want: {_md_escape(str(name)[:100])}",
                              url=card.get("scryfall_uri"), color=0x1F8B4C)
        embed.description = f"Printing: **{_md_escape(_printing_option_label(card))}**"
        image = _printing_image_url(card)
        if image:
            embed.set_image(url=image)
        return embed

    @discord.ui.button(label="Add to wants board", style=discord.ButtonStyle.primary)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._do_confirm(interaction)

    async def _do_confirm(self, interaction: discord.Interaction) -> None:
        if self._selected is None:
            await interaction.response.send_message("Pick a printing first.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        with _session() as session:
            try:
                want = add_want_from_printing(session, interaction.guild_id,
                                              interaction.user.id, self._selected, self.note)
            except WantLimitError as e:
                await interaction.followup.send(str(e), ephemeral=True)
                return
            except ValueError as e:
                await interaction.followup.send(str(e), ephemeral=True)
                return
            shown = f"**{_md_escape(want.card_name)}**"
            if want.set_code:
                shown += f" — {_md_escape(want.set_code.upper())} #{_md_escape(want.collector_number or '')}"
            cfg = get_or_create_config(session, interaction.guild_id)
            if not cfg.wants_channel_id:
                msg = (f"Saved your want {shown}, but no wants channel is set yet — "
                       "an admin can run /config wants_channel.")
            else:
                try:
                    await update_wants_board(self.bot, session, interaction.guild_id)
                    msg = f"Added to the wants board: {shown}."
                except Exception:
                    log.exception("wants board update failed after add")
                    session.rollback()
                    msg = (f"Saved your want {shown}, but I couldn't update the board — "
                           "check my permissions in the wants channel.")
        for child in self.children:
            child.disabled = True
        await interaction.edit_original_response(embed=self.preview_embed(), view=self)
        await interaction.followup.send(msg, ephemeral=True)


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
        return bool(re.match(r'^<a?:[\w-]+:\d+>$', emoji))
    # Reject pure ASCII (catches "ab", "abc")
    if emoji.isascii():
        return False
    # Keycap sequences: digit/# /* + optional variation selector + combining enclosing keycap
    if re.match(r'^[0-9#*]\uFE0F?\u20E3$', emoji):
        return True
    # For Unicode, verify at least one char is from emoji blocks and no stray letters mixed in.
    # Allows emoji sequences with variation selectors, ZWJ, modifiers.
    emoji_codepoint_ranges = [
        (0x1F300, 0x1F9FF),  # Main emoji blocks (emoticons, symbols, transport, etc.)
        (0x2600, 0x27BF),    # Miscellaneous symbols and dingbats
        (0x2B00, 0x2BFF),    # Miscellaneous symbols and arrows (⭐, ⬅️)
        (0x1F1E6, 0x1F1FF),  # Regional indicators for flags
        (0x2300, 0x23FF),    # Miscellaneous technical
        (0x1FA00, 0x1FAFF),  # Newer emoji (🫡, 🩷)
    ]
    # Special symbol bases that may have optional variation selector
    special_chars = {0x00A9, 0x00AE, 0x203C, 0x2049, 0x2122, 0x2139}
    # Helper ranges (variation selectors, ZWJ, skin tone modifiers, combining marks)
    allowed_helper_ranges = [
        (0x20D0, 0x20FF),    # Combining diacritical marks
        (0xFE00, 0xFE0F),    # Variation selectors
        (0x200D, 0x200D),    # Zero-width joiner
        (0x1F3FB, 0x1F3FF),  # Emoji modifiers (skin tones)
    ]
    has_emoji_codepoint = False
    for char in emoji:
        ord_val = ord(char)
        # Check main emoji ranges
        if any(start <= ord_val <= end for start, end in emoji_codepoint_ranges):
            has_emoji_codepoint = True
        # Check special symbol bases
        elif ord_val in special_chars:
            has_emoji_codepoint = True
        # Check helper ranges
        elif not any(start <= ord_val <= end for start, end in allowed_helper_ranges):
            # Stray character not in emoji range and not a helper (e.g., regular letter)
            # Rejects é, 日本, Hi😀
            return False
    return has_emoji_codepoint


def _role_rejection(role: discord.Role) -> str | None:
    return role_rejection_reason(role)


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
        await interaction.response.defer(ephemeral=True)
        with _session() as session:
            cfg = get_or_create_config(session, interaction.guild_id)
            roles = list_interest_roles(session, interaction.guild_id)
            embed = build_roles_panel_embed(roles)
            view = RolesPanelView(roles)

            # Best-effort cleanup of the old panel when switching channels.
            old_panel_deleted = False
            if cfg.roles_message_id and cfg.roles_channel_id and cfg.roles_channel_id != channel.id:
                old_channel = interaction.guild.get_channel(cfg.roles_channel_id)
                if old_channel is not None:
                    try:
                        old_msg = await old_channel.fetch_message(cfg.roles_message_id)
                        await old_msg.delete()
                        old_panel_deleted = True
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        pass
            removed_note = " The old panel was removed." if old_panel_deleted else ""

            message = None
            if cfg.roles_message_id and cfg.roles_channel_id == channel.id:
                try:
                    message = await channel.fetch_message(cfg.roles_message_id)
                    await message.edit(embed=embed, view=view)
                except discord.NotFound:
                    message = None  # gone: fall through and post a fresh panel
                except discord.Forbidden:
                    await interaction.followup.send(
                        "I don't have permission to access the panel — check my channel "
                        "permissions.", ephemeral=True)
                    return
                except discord.HTTPException:
                    await interaction.followup.send(
                        "Discord rejected the panel update (check the role emoji)."
                        + removed_note, ephemeral=True)
                    return

            if message is None:
                try:
                    message = await channel.send(embed=embed, view=view)
                except discord.Forbidden:
                    cfg.roles_channel_id = None
                    cfg.roles_message_id = None
                    session.commit()
                    await interaction.followup.send(
                        f"I don't have permission to post in {channel.mention}."
                        + removed_note, ephemeral=True)
                    return
                except discord.HTTPException:
                    cfg.roles_channel_id = None
                    cfg.roles_message_id = None
                    session.commit()
                    await interaction.followup.send(
                        "Discord rejected the panel update (check the role emoji)."
                        + removed_note, ephemeral=True)
                    return

            cfg.roles_channel_id = channel.id
            cfg.roles_message_id = message.id
            session.commit()

        await interaction.followup.send(
            f"Roles panel is live in {channel.mention}.{removed_note}", ephemeral=True)

    @roles_group.command(name="add", description="Add an interest role to the panel")
    async def add(self, interaction: discord.Interaction, role: discord.Role,
                  label: str, emoji: str | None = None) -> None:
        await interaction.response.defer(ephemeral=True)
        label = label.strip()
        if not label or len(label) > _MAX_LABEL_LEN:
            await interaction.followup.send(
                f"Label must be 1-{_MAX_LABEL_LEN} characters.", ephemeral=True)
            return
        emoji = emoji.strip() if emoji else None
        if emoji and not _valid_emoji(emoji):
            await interaction.followup.send(
                "Emoji must be a single Unicode emoji or a custom emoji like <:name:id>.",
                ephemeral=True)
            return
        reason = _role_rejection(role)
        if reason:
            await interaction.followup.send(reason, ephemeral=True)
            return
        with _session() as session:
            if len(list_interest_roles(session, interaction.guild_id)) >= MAX_PANEL_ROLES:
                await interaction.followup.send(
                    f"The panel supports at most {MAX_PANEL_ROLES} roles.", ephemeral=True)
                return
            try:
                row = add_interest_role(session, interaction.guild_id, role.id, label, emoji)
            except IntegrityError:
                session.rollback()
                await interaction.followup.send(
                    "That role is already on the panel.", ephemeral=True)
                return
            row_id = row.id
            note = await _refresh_panel(interaction.guild, session, interaction.guild_id)
        if not role.mentionable \
                and not interaction.guild.me.guild_permissions.mention_everyone:
            note += _NOT_MENTIONABLE_NOTE
        await interaction.followup.send(
            f"Added **{_esc(label)}** (id {row_id}).{note}", ephemeral=True)

    @roles_group.command(name="remove", description="Remove an interest role (by id from /roles list)")
    async def remove(self, interaction: discord.Interaction, row_id: int) -> None:
        await interaction.response.defer(ephemeral=True)
        with _session() as session:
            if not remove_interest_role(session, interaction.guild_id, row_id):
                await interaction.followup.send(
                    f"No role with id {row_id}. See /roles list.", ephemeral=True)
                return
            note = await _refresh_panel(interaction.guild, session, interaction.guild_id)
        await interaction.followup.send(f"Removed role {row_id}.{note}", ephemeral=True)

    @roles_group.command(name="keyword", description="Add or remove a keyword that pings this role")
    async def keyword(self, interaction: discord.Interaction, row_id: int,
                      action: Literal["add", "remove"], keyword: str) -> None:
        await interaction.response.defer(ephemeral=True)
        if len(keyword.strip()) > _MAX_KEYWORD_LEN:
            await interaction.followup.send(
                f"Keyword must be at most {_MAX_KEYWORD_LEN} characters.", ephemeral=True)
            return
        with _session() as session:
            row = session.get(InterestRole, row_id)
            if row is None or row.guild_id != interaction.guild_id:
                await interaction.followup.send(
                    f"No role with id {row_id}. See /roles list.", ephemeral=True)
                return
            label = _esc(row.label)
            shown = _esc(keyword.strip().lower())
            if action == "add":
                try:
                    add_keyword(session, row_id, keyword)
                except ValueError:
                    await interaction.followup.send(
                        "Keyword can't be empty.", ephemeral=True)
                    return
                except IntegrityError:
                    session.rollback()
                    await interaction.followup.send(
                        f"**{label}** already has the keyword \"{shown}\".", ephemeral=True)
                    return
                msg = f"Added keyword \"{shown}\" to **{label}**."
            else:
                if not remove_keyword(session, row_id, keyword):
                    await interaction.followup.send(
                        f"No keyword \"{shown}\" on **{label}**.", ephemeral=True)
                    return
                msg = f"Removed keyword \"{shown}\" from **{label}**."
        await interaction.followup.send(msg, ephemeral=True)

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

    # Top-level /card command (public, non-ephemeral on success for sharing)
    @app_commands.command(
        name="card",
        description="Look up a Magic card on Scryfall"
    )
    @app_commands.guild_only()
    @app_commands.checks.cooldown(1, _LOOKUP_COOLDOWN_SECONDS, key=lambda i: i.user.id)
    async def card_lookup(self, interaction: discord.Interaction, name: str) -> None:
        """Look up a Magic card by name and display its details."""
        name = name.strip()
        if not name:
            await interaction.response.send_message("Give me a card name.", ephemeral=True)
            return
        if len(name) > _MAX_CARD_NAME_LEN:
            await interaction.response.send_message(
                f"Card name must be ≤{_MAX_CARD_NAME_LEN} characters.",
                ephemeral=True
            )
            return

        # Lookup with bounded timeout (2.5s) and retries disabled, enforced with wait_for
        try:
            card = await asyncio.wait_for(
                scryfall_card(name, retries=False, timeout=2.5),
                timeout=_LOOKUP_TIMEOUT
            )
        except asyncio.TimeoutError:
            await interaction.response.send_message(
                "Scryfall lookup is taking too long — try again shortly.",
                ephemeral=True
            )
            return
        except AmbiguousCardError:
            await interaction.response.send_message(
                f"Too many cards match '{_md_escape(name)}' — add more words.",
                ephemeral=True
            )
            return
        except httpx.TransportError as e:
            log.warning("Scryfall lookup transport error for %r: %r", name, e)
            await interaction.response.send_message(
                "Scryfall is having trouble — try again shortly.",
                ephemeral=True
            )
            return
        except Exception as e:
            if isinstance(e, httpx.HTTPStatusError):
                log.warning("Scryfall lookup HTTP error for %r: %r", name, e)
            else:
                log.error("Unexpected error in /card for %r: %r", name, e)
            await interaction.response.send_message(
                "Scryfall is having trouble — try again shortly.",
                ephemeral=True
            )
            return

        # Handle 404 (card not found)
        if card is None:
            await interaction.response.send_message(
                f"No card found for '{_md_escape(name)}'.",
                ephemeral=True
            )
            return

        # Success: build and send embed (public)
        embed = build_card_embed(card)
        await interaction.response.send_message(embed=embed, ephemeral=False)

    # Top-level /deck command (public on success, like /card)
    @app_commands.command(
        name="deck",
        description="Show a Moxfield or Archidekt deck"
    )
    @app_commands.guild_only()
    @app_commands.checks.cooldown(1, _LOOKUP_COOLDOWN_SECONDS, key=lambda i: i.user.id)
    async def deck_lookup(self, interaction: discord.Interaction, url: str) -> None:
        """Show a summary embed for a Moxfield or Archidekt deck link."""
        url = url.strip()
        if not url:
            await interaction.response.send_message("Give me a deck link.", ephemeral=True)
            return
        if len(url) > _MAX_DECK_URL_LEN:
            await interaction.response.send_message(
                f"Deck link must be ≤{_MAX_DECK_URL_LEN} characters.",
                ephemeral=True
            )
            return

        try:
            deck = await asyncio.wait_for(
                fetch_deck(url, retries=False, timeout=2.5),
                timeout=_LOOKUP_TIMEOUT
            )
        except asyncio.TimeoutError:
            await interaction.response.send_message(
                "Deck lookup is taking too long — try again shortly.",
                ephemeral=True
            )
            return
        except Exception as e:
            parsed = parse_deck_url(url)
            site = parsed[0] if parsed else "The deck site"
            log.warning("/deck lookup failed for %s: %r", site, e)
            await interaction.response.send_message(
                f"{site} is having trouble — try again shortly.",
                ephemeral=True
            )
            return

        if deck is None:
            await interaction.response.send_message(
                "Couldn't read that deck — check the link (or the site may be blocking us).",
                ephemeral=True
            )
            return

        await interaction.response.send_message(embed=build_deck_embed(deck), ephemeral=False)

    want_group = app_commands.Group(
        name="want", description="Card wants board", guild_only=True)

    @want_group.command(name="add",
                        description="Search for a card and add it to the wants board")
    @app_commands.describe(card="Card name — pick from the suggestions",
                           note="Optional note, e.g. condition or foil")
    @app_commands.checks.cooldown(1, _LOOKUP_COOLDOWN_SECONDS, key=lambda i: i.user.id)
    async def want_add(self, interaction: discord.Interaction, card: str,
                       note: str | None = None) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            printings = await asyncio.wait_for(
                scryfall_printings(card.strip(), timeout=_LOOKUP_TIMEOUT - 0.1),
                timeout=_LOOKUP_TIMEOUT)
        except asyncio.TimeoutError:
            await interaction.followup.send(
                "Scryfall lookup is taking too long — try again shortly.", ephemeral=True)
            return
        except Exception as e:
            log.warning("Scryfall printings lookup failed for %r: %r", card, e)
            await interaction.followup.send(
                "Scryfall is having trouble — try again shortly.", ephemeral=True)
            return
        if not printings:
            await interaction.followup.send(
                f"I couldn't find a card called **{_md_escape(card)}** on Scryfall. "
                "Pick a suggestion as you type, or check the spelling.", ephemeral=True)
            return

        view = _PrintingPickerView(self.bot, interaction.user.id, printings, note)
        embed = view.preview_embed()
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @want_add.autocomplete("card")
    async def want_add_autocomplete(self, interaction: discord.Interaction,
                                    current: str) -> list[app_commands.Choice[str]]:
        try:
            names = await scryfall_autocomplete(current, timeout=2.0)
        except Exception:
            return []
        return [app_commands.Choice(name=n[:100], value=n[:100]) for n in names]

    @want_group.command(name="browse", description="See what everyone is looking for")
    async def want_browse(self, interaction: discord.Interaction) -> None:
        with _session() as session:
            cfg = get_or_create_config(session, interaction.guild_id)
            embed = build_wants_embed(
                list_wants(session, interaction.guild_id), ZoneInfo(cfg.timezone))
        await interaction.response.send_message(
            embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @want_group.command(name="remove", description="Remove one of your wants (by id from /want list)")
    async def want_remove(self, interaction: discord.Interaction,
                          want_id: app_commands.Range[int, 1, 2**31 - 1]) -> None:
        await interaction.response.defer(ephemeral=True)
        is_admin = interaction.user.guild_permissions.manage_guild
        with _session() as session:
            if not remove_want(session, interaction.guild_id, want_id, interaction.user.id,
                               is_admin):
                await interaction.followup.send(
                    f"No want #{want_id} found — you can only remove your own wants "
                    "(see /want list).", ephemeral=True)
                return
            note = ""
            if get_or_create_config(session, interaction.guild_id).wants_channel_id:
                try:
                    await update_wants_board(self.bot, session, interaction.guild_id)
                except Exception:
                    log.exception("wants board update failed after remove")
                    session.rollback()
                    note = " I couldn't update the board — check my permissions."
        await interaction.followup.send(f"Removed want #{want_id}.{note}", ephemeral=True)

    @want_group.command(name="list", description="List your own wants")
    async def want_list(self, interaction: discord.Interaction) -> None:
        with _session() as session:
            tz = ZoneInfo(get_or_create_config(session, interaction.guild_id).timezone)
            wants = list_wants(session, interaction.guild_id, interaction.user.id)
            lines = [
                f"#{w.id} **{_md_escape(w.card_name)}**"
                + (f" · {_md_escape(w.note)}" if w.note else "")
                + f" · {w.created_at.astimezone(tz).strftime('%b %d')}"
                for w in wants
            ]
        if not lines:
            await interaction.response.send_message(
                "You have no active wants. Add one with /want add.", ephemeral=True)
            return
        text, _ = _truncate_lines(lines, _MAX_MESSAGE_CHARS)  # whole lines only: no cut markdown
        await interaction.response.send_message(text, ephemeral=True)
