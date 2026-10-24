import re
from datetime import datetime, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src import config
from src.db.models import EventSource, WeeklyRecurring
from src.db.repositories import get_or_create_config
from src.db.session import get_session_factory
from src.services.product_service import parse_category_slugs
from src.services.weekly_service import get_weekly_rows, seed_weekly_recurring

_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_MAX_URL_LEN = 500
_MAX_WEEKLY_TITLE_LEN = 200
_MAX_WEEKLY_TEXT_LEN = 40
_MAX_MESSAGE_CHARS = 1900
_WEEKLY_REFRESH_NOTE = "Shows on next refresh, or run /board refresh now."

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


def _chunk_embeds(embeds: list[discord.Embed]) -> list[list[discord.Embed]]:
    """Split into messages within Discord's 10-embed / 6000-char limits."""
    chunks: list[list[discord.Embed]] = [[]]
    total = 0
    for embed in embeds:
        size = len(embed)
        if chunks[-1] and (len(chunks[-1]) >= 10 or total + size > 6000):
            chunks.append([])
            total = 0
        chunks[-1].append(embed)
        total += size
    return chunks


def _md_title(title: str) -> str:
    return discord.utils.escape_markdown(discord.utils.escape_mentions(title))


_UNDATED_SORT = datetime.max.replace(tzinfo=timezone.utc)


def _event_choice_label(event, tz) -> str:
    label = event.title if event.starts_at is None \
        else f"{event.title} — {event.starts_at.astimezone(tz):%a %d %b}"
    return label[:100]


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

    @config_group.command(
        name="announce_channel",
        description="Channel for new-event pings and the daily Today at Hightide post")
    async def set_announce_channel(self, interaction: discord.Interaction,
                                   channel: discord.TextChannel) -> None:
        with _session() as session:
            cfg = get_or_create_config(session, interaction.guild_id)
            cfg.announce_channel_id = channel.id
            session.commit()
        await interaction.response.send_message(
            f"announce channel set to {channel.mention}", ephemeral=True)

    @config_group.command(
        name="wants_channel",
        description="Channel for the persistent card wants board")
    async def set_wants_channel(self, interaction: discord.Interaction,
                                channel: discord.TextChannel) -> None:
        with _session() as session:
            cfg = get_or_create_config(session, interaction.guild_id)
            cfg.wants_channel_id = channel.id
            session.commit()
        await interaction.response.send_message(
            f"wants channel set to {channel.mention}", ephemeral=True)

    @config_group.command(
        name="new_stock_channel",
        description="Channel for new-product alerts from watched shop categories")
    async def set_new_stock_channel(self, interaction: discord.Interaction,
                                    channel: discord.TextChannel) -> None:
        with _session() as session:
            cfg = get_or_create_config(session, interaction.guild_id)
            cfg.new_stock_channel_id = channel.id
            session.commit()
        await interaction.response.send_message(
            f"new-stock channel set to {channel.mention}", ephemeral=True)

    @config_group.command(
        name="new_stock_categories",
        description="Shop category slugs to watch for new products (comma-separated)")
    async def set_new_stock_categories(self, interaction: discord.Interaction,
                                       slugs: str) -> None:
        try:
            parsed = parse_category_slugs(slugs)
        except ValueError:
            await interaction.response.send_message(
                "Invalid categories. Use up to 10 comma-separated slugs "
                "(lowercase letters, digits, hyphens; max 50 chars each), e.g. pre-orders.",
                ephemeral=True)
            return
        with _session() as session:
            cfg = get_or_create_config(session, interaction.guild_id)
            cfg.new_stock_cat_slugs = ",".join(parsed)
            session.commit()
        await interaction.response.send_message(
            f"watching categories: {', '.join(parsed)}", ephemeral=True)

    @config_group.command(
        name="daily_post_time",
        description="Today-post time (v2: posts at the daily refresh time instead)")
    async def set_daily_post_time(self, interaction: discord.Interaction, hhmm: str) -> None:
        if not _HHMM_RE.match(hhmm):
            await interaction.response.send_message(
                "Invalid time. Use 24-hour HH:MM, e.g. 09:30.", ephemeral=True)
            return
        with _session() as session:
            cfg = get_or_create_config(session, interaction.guild_id)
            cfg.daily_post_time = hhmm
            session.commit()
        await interaction.response.send_message(
            f"daily post time set to {hhmm} (currently posts at the daily refresh time)",
            ephemeral=True)

    @config_group.command(name="include_tba")
    async def set_include_tba(self, interaction: discord.Interaction, value: bool) -> None:
        with _session() as session:
            cfg = get_or_create_config(session, interaction.guild_id)
            cfg.include_tba_section = value
            session.commit()
        await interaction.response.send_message(
            f"include_tba_section = {value}", ephemeral=True)

    @config_group.command(name="weekly_list")
    async def weekly_list(self, interaction: discord.Interaction) -> None:
        from src.discord_ui.embeds import _md_escape
        with _session() as session:
            seed_weekly_recurring(session, interaction.guild_id)
            rows = get_weekly_rows(session, interaction.guild_id)
            lines = []
            for n, r in enumerate(rows, 1):
                flags = ("" if r.enabled else " (disabled)") + (
                    f" (until {r.ends_on})" if r.ends_on else "")
                lines.append(f"{n}. `{r.id}` {_md_escape(r.days_text)} · {_md_escape(r.time_text)}"
                             f" — {_md_escape(r.title)}{flags}")
        text = "\n".join(lines)
        if len(text) > _MAX_MESSAGE_CHARS:
            text = text[:_MAX_MESSAGE_CHARS - 1] + "…"
        await interaction.response.send_message(text, ephemeral=True)

    @config_group.command(name="weekly_add")
    async def weekly_add(self, interaction: discord.Interaction,
                         title: str, days: str, time: str) -> None:
        from src.discord_ui.embeds import _md_escape
        title, days, time = title.strip(), days.strip(), time.strip()
        if not (title and days and time):
            await interaction.response.send_message(
                "Title, days and time must not be empty.", ephemeral=True)
            return
        if len(title) > _MAX_WEEKLY_TITLE_LEN or len(days) > _MAX_WEEKLY_TEXT_LEN \
                or len(time) > _MAX_WEEKLY_TEXT_LEN:
            await interaction.response.send_message(
                f"Too long: title max {_MAX_WEEKLY_TITLE_LEN}, days/time max "
                f"{_MAX_WEEKLY_TEXT_LEN} characters.", ephemeral=True)
            return
        with _session() as session:
            # Seeds only on a never-seeded guild, so adding doesn't suppress the default roster.
            seed_weekly_recurring(session, interaction.guild_id)
            top = session.execute(select(func.max(WeeklyRecurring.sort_order)).where(
                WeeklyRecurring.guild_id == interaction.guild_id)).scalar()
            row = WeeklyRecurring(guild_id=interaction.guild_id, title=title, days_text=days,
                                  time_text=time, sort_order=(top + 1) if top is not None else 0,
                                  enabled=True)
            session.add(row)
            session.commit()
            row_id = row.id
        await interaction.response.send_message(
            f"Added weekly event `{row_id}`: {_md_escape(days)} · {_md_escape(time)} — "
            f"{_md_escape(title)}. {_WEEKLY_REFRESH_NOTE}", ephemeral=True)

    @config_group.command(name="weekly_remove")
    async def weekly_remove(self, interaction: discord.Interaction, row_id: int) -> None:
        with _session() as session:
            row = session.get(WeeklyRecurring, row_id)
            removed = bool(row and row.guild_id == interaction.guild_id)
            if removed:
                session.delete(row)
                session.commit()
        if removed:
            await interaction.response.send_message(
                f"Removed weekly event {row_id}. {_WEEKLY_REFRESH_NOTE}", ephemeral=True)
        else:
            await interaction.response.send_message("Weekly event not found.", ephemeral=True)

    @config_group.command(
        name="weekly_image",
        description="Set the weekly section image URL (or 'none' to clear it)")
    async def weekly_image(self, interaction: discord.Interaction, url: str) -> None:
        if url.strip().lower() == "none":
            with _session() as session:
                cfg = get_or_create_config(session, interaction.guild_id)
                cfg.weekly_image_url = None
                session.commit()
            await interaction.response.send_message(
                f"Weekly image cleared. {_WEEKLY_REFRESH_NOTE}", ephemeral=True)
            return
        if not _valid_source_url(url):
            await interaction.response.send_message(
                f"Invalid URL. Use an http(s) URL up to {_MAX_URL_LEN} characters.",
                ephemeral=True)
            return
        with _session() as session:
            cfg = get_or_create_config(session, interaction.guild_id)
            cfg.weekly_image_url = url
            session.commit()
        await interaction.response.send_message(
            f"Weekly image set. {_WEEKLY_REFRESH_NOTE}", ephemeral=True)

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
        from src.discord_ui.embeds import build_board_embed, build_week_board_payload
        from src.services.board_service import filter_events_for_board

        followups: list[list[discord.Embed]] = []
        try:
            with _session() as session:
                cfg = get_or_create_config(session, interaction.guild_id)
                tz = ZoneInfo(cfg.timezone)
                events = filter_events_for_board(
                    get_active_events(session, interaction.guild_id), board_type, cfg, tz)
                preview_title = board_type.replace("_", " ").title()
                if board_type == "this_week":
                    seed_weekly_recurring(session, interaction.guild_id)
                    chunks = _chunk_embeds(build_week_board_payload(
                        events, preview_title, tz, cfg.include_tba_section,
                        weekly_rows=get_weekly_rows(session, interaction.guild_id),
                        weekly_image_url=cfg.weekly_image_url))
                    send_kwargs, followups = {"embeds": chunks[0]}, chunks[1:]
                else:
                    send_kwargs = {"embed": build_board_embed(
                        events, preview_title, tz, cfg.include_tba_section)}
        except Exception as e:
            import logging
            logging.getLogger("hightide").exception("board preview failed")
            await interaction.response.send_message(
                f"Preview failed: {type(e).__name__}", ephemeral=True)
            return
        await interaction.response.send_message(**send_kwargs, ephemeral=True)
        for chunk in followups:
            await interaction.followup.send(embeds=chunk, ephemeral=True)

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

    async def signups_event_autocomplete(self, interaction: discord.Interaction,
                                         current: str) -> list[app_commands.Choice[str]]:
        from src.db.repositories import get_active_events
        needle = current.strip().lower()
        with _session() as session:
            tz = ZoneInfo(get_or_create_config(session, interaction.guild_id).timezone)
            events = get_active_events(session, interaction.guild_id)
            matches = sorted(
                (e for e in events if needle in e.title.lower()),
                key=lambda e: (e.starts_at is None, e.starts_at or _UNDATED_SORT, e.title))
            choices = [(_event_choice_label(e, tz), e.id) for e in matches[:25]]
        return [app_commands.Choice(name=label, value=str(event_id))
                for label, event_id in choices]

    @board_group.command(name="signups", description="List who signed up for an event")
    @app_commands.autocomplete(event=signups_event_autocomplete)
    async def board_signups(self, interaction: discord.Interaction, event: str) -> None:
        await interaction.response.defer(ephemeral=True)
        from src.db.repositories import get_event_by_id
        from src.services.signup_service import list_signups
        with _session() as session:
            row = get_event_by_id(session, interaction.guild_id, int(event)) \
                if re.fullmatch(r"[0-9]{1,18}", event) else None
            if row is None:
                await interaction.followup.send("Event not found.", ephemeral=True)
                return
            title = row.title
            user_ids = [s.user_id for s in list_signups(session, interaction.guild_id, row.id)]
        if not user_ids:
            await interaction.followup.send("No signups yet.", ephemeral=True)
            return
        names = []
        for uid in user_ids:
            member = interaction.guild.get_member(uid)
            if member is None:
                try:
                    member = await interaction.guild.fetch_member(uid)
                except (discord.NotFound, discord.HTTPException):
                    member = None
            names.append(discord.utils.escape_markdown(
                discord.utils.escape_mentions(member.display_name)) if member else f"User {uid}")
        text = f"**{_md_title(title)}** — {len(names)} going\n" + "\n".join(f"• {n}" for n in names)
        if len(text) > _MAX_MESSAGE_CHARS:
            text = text[:_MAX_MESSAGE_CHARS - 1] + "…"
        await interaction.followup.send(
            text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

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
