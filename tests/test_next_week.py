"""Tests for the 'See next week' button on an empty week board."""
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import discord
import pytest

from src.db.models import Event, GuildConfig
from src.db.repositories import get_board_post
from src.discord_ui.role_views import NextWeekButton, NextWeekView
from src.services import board_service
from src.services.board_service import _next_week_window, build_next_week_preview, upsert_board

TZ = ZoneInfo("Australia/Perth")
GUILD = 999


def _cfg(**kw):
    base = dict(guild_id=GUILD, timezone="Australia/Perth", this_week_channel_id=111,
                week_start_mode="monday", include_tba_section=True)
    base.update(kw)
    return GuildConfig(**base)


def _event(name, starts_at):
    return Event(guild_id=GUILD, external_id=f"wp:{name}", title=name, url="https://x",
                 starts_at=starts_at, time_known=True, is_active=True)


class _Msg:
    def __init__(self, channel, mid, embed, view=None):
        self.channel = channel
        self.id = mid
        self.author = SimpleNamespace(id=42)
        self.embed = embed
        self.view = view

    async def edit(self, **kw):
        self.embed = kw.get("embed", self.embed)
        if "view" in kw:
            self.view = kw["view"]

    async def delete(self):
        self.channel.messages.pop(self.id, None)


class _Channel:
    def __init__(self, cid=111):
        self.id = cid
        self.messages = {}
        self.sends = []
        self._next = 1000

    async def send(self, **kw):
        self._next += 1
        m = _Msg(self, self._next, kw["embed"], kw.get("view"))
        self.messages[m.id] = m
        self.sends.append(kw)
        return m

    async def fetch_message(self, mid):
        if mid not in self.messages:
            resp = MagicMock(); resp.status = 404
            raise discord.NotFound(resp, "gone")
        return self.messages[mid]


def _bot(channel):
    bot = MagicMock()
    bot.user = SimpleNamespace(id=42)
    bot.get_channel = MagicMock(return_value=channel)
    bot.fetch_channel = AsyncMock(return_value=channel)
    return bot


# --- window ---

def test_next_week_window_is_next_monday_to_sunday():
    # Wednesday 2026-10-28 -> next week is Mon Nov 2 to Mon Nov 9
    now = datetime(2026, 10, 28, 15, 0, tzinfo=TZ)
    start, end = _next_week_window(TZ, now=now)
    assert start == datetime(2026, 11, 2, 0, 0, tzinfo=TZ)
    assert end == datetime(2026, 11, 9, 0, 0, tzinfo=TZ)


def test_next_week_window_on_sunday():
    now = datetime(2026, 10, 25, 8, 0, tzinfo=TZ)  # Sunday
    start, end = _next_week_window(TZ, now=now)
    assert start == datetime(2026, 10, 26, 0, 0, tzinfo=TZ)
    assert end == datetime(2026, 11, 2, 0, 0, tzinfo=TZ)


# --- button wiring on the board ---

async def test_empty_week_board_header_has_next_week_button(session):
    session.add(_cfg())
    session.commit()
    channel = _Channel()
    await upsert_board(_bot(channel), session, GUILD, "this_week")
    header = channel.sends[0]
    assert "view" in header and isinstance(header["view"], NextWeekView)


async def test_nonempty_week_board_header_has_no_next_week_button(session):
    session.add(_cfg())
    session.add(_event("a", datetime.now(TZ) + timedelta(hours=3)))
    session.commit()
    channel = _Channel()
    await upsert_board(_bot(channel), session, GUILD, "this_week")
    header = channel.sends[0]
    assert "view" not in header or header.get("view") is None


# --- the ephemeral preview ---

def _interaction(guild_id=GUILD):
    i = AsyncMock(spec=discord.Interaction)
    i.guild_id = guild_id
    i.user = MagicMock(id=7)
    i.response = AsyncMock()
    i.response.is_done = MagicMock(return_value=False)
    i.followup = AsyncMock()
    return i


async def test_next_week_preview_shows_only_next_week_events(temp_db, monkeypatch):
    monkeypatch.setattr(board_service, "_session_factory", temp_db)
    now = datetime.now(TZ)
    # next Monday (within next week) and an event today (not in next week)
    days_to_next_mon = (7 - now.weekday()) % 7 or 7
    next_mon = datetime.combine((now + timedelta(days=days_to_next_mon)).date(),
                                datetime.min.time(), tzinfo=TZ).replace(hour=18)
    with temp_db() as s:
        s.add(GuildConfig(guild_id=GUILD, timezone="Australia/Perth",
                          this_week_channel_id=111))
        s.add(_event("Next Week Draft", next_mon))
        s.add(_event("Today Event", now + timedelta(hours=2)))
        s.commit()

    i = _interaction()
    await build_next_week_preview(i)
    i.response.send_message.assert_awaited_once()
    kw = i.response.send_message.call_args.kwargs
    assert kw["ephemeral"] is True
    embed = kw["embed"]
    assert embed.title == "Next Week at Hightide"
    # dated events render as fields, not the description
    all_text = (embed.description or "") + " ".join(
        f"{f.name} {f.value}" for f in embed.fields)
    assert "Next Week Draft" in all_text
    assert "Today Event" not in all_text


async def test_next_week_preview_empty_week(temp_db, monkeypatch):
    monkeypatch.setattr(board_service, "_session_factory", temp_db)
    with temp_db() as s:
        s.add(GuildConfig(guild_id=GUILD, timezone="Australia/Perth",
                          this_week_channel_id=111))
        s.commit()
    i = _interaction()
    await build_next_week_preview(i)
    kw = i.response.send_message.call_args.kwargs
    assert kw["ephemeral"] is True
    assert "No" in kw["embed"].description  # empty-state text


async def test_next_week_preview_uses_followup_when_response_done(temp_db, monkeypatch):
    monkeypatch.setattr(board_service, "_session_factory", temp_db)
    with temp_db() as s:
        s.add(GuildConfig(guild_id=GUILD, timezone="Australia/Perth",
                          this_week_channel_id=111))
        s.commit()
    i = _interaction()
    i.response.is_done = MagicMock(return_value=True)
    await build_next_week_preview(i)
    i.response.send_message.assert_not_awaited()
    i.followup.send.assert_awaited_once()
    assert i.followup.send.call_args.kwargs["ephemeral"] is True


# --- button object ---

def test_next_week_button_shape_and_custom_id():
    btn = NextWeekButton()
    assert btn.item.custom_id == "next_week:"
    assert "next week" in btn.item.label.lower()


async def test_next_week_button_dispatch_survives_restart():
    # from_custom_id reconstructs with no state, then callback runs the preview
    i = _interaction()
    with patch("src.services.board_service.build_next_week_preview",
               new=AsyncMock()) as mock_preview:
        btn = await NextWeekButton.from_custom_id(i, MagicMock(), MagicMock())
        await btn.callback(i)
    mock_preview.assert_awaited_once_with(i)
