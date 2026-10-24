"""Signup buttons on week cards + /board signups."""
import json
import re
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import discord
import pytest

from src.bot import create_bot
from src.db.models import Base, Event, GuildConfig
from src.db.repositories import get_board_post
from src.db.session import get_engine, get_session_factory
from src.discord_ui import commands_admin
from src.discord_ui.commands_admin import AdminCog
from src.discord_ui.role_views import SignupButton, SignupView
from src.services import signup_service
from src.services.board_service import upsert_board
from src.services.signup_service import count_signups, handle_signup_toggle, toggle_signup

GUILD = 999
TZ = ZoneInfo("Australia/Perth")


@pytest.fixture
def db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'test.db'}"
    engine = get_engine(url)
    Base.metadata.create_all(engine)
    factory = get_session_factory(url)
    monkeypatch.setattr(signup_service, "_session_factory", factory)
    monkeypatch.setattr(commands_admin, "_session_factory", factory)
    yield factory
    engine.dispose()
    factory.kw["bind"].dispose()


def _add_event(db, title="Draft Night", guild_id=GUILD, active=True, ext="wp:1"):
    with db() as s:
        e = Event(guild_id=guild_id, external_id=ext, title=title, url="https://x/e",
                  starts_at=datetime.now(TZ) + timedelta(days=1), time_known=True,
                  is_active=active)
        s.add(e)
        s.commit()
        return e.id


@pytest.fixture
def interaction():
    i = MagicMock()
    i.guild_id = GUILD
    i.user.id = 100
    i.response.send_message = AsyncMock()
    i.response.edit_message = AsyncMock()
    i.response.defer = AsyncMock()
    i.followup.send = AsyncMock()
    return i


# ---- view shape ----

def test_view_button_custom_id_and_label():
    view = SignupView(event_id=5, count=3)
    assert view.timeout is None
    assert [c.custom_id for c in view.children] == ["signup:5"]
    assert view.children[0].label == "✅ Going (3)"


def test_view_default_count_zero():
    assert SignupView(event_id=1).children[0].label == "✅ Going (0)"


async def test_dynamic_button_parses_event_id_from_custom_id():
    match = re.fullmatch(SignupButton.__discord_ui_compiled_template__, "signup:42")
    button = await SignupButton.from_custom_id(MagicMock(), MagicMock(), match)
    assert button.event_id == 42


async def test_button_callback_routes_to_handler(monkeypatch):
    seen = []

    async def fake(interaction, event_id):
        seen.append((interaction, event_id))

    monkeypatch.setattr(signup_service, "handle_signup_toggle", fake)
    marker = object()
    await SignupView(event_id=9).children[0].callback(marker)
    assert seen == [(marker, 9)]


async def test_setup_hook_registers_dynamic_signup_button(tmp_path, monkeypatch):
    from src import bot as bot_module, config
    from src.services import scheduler
    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{tmp_path / 'hook.db'}")
    monkeypatch.setattr(config, "GUILD_ID", None)
    monkeypatch.setattr(scheduler, "_session_factory", None)
    monkeypatch.setattr(scheduler.RefreshScheduler, "start", lambda self: None)
    bot = create_bot()
    bot.add_dynamic_items = MagicMock()
    bot.tree.sync = AsyncMock()
    await bot.setup_hook()
    bot.add_dynamic_items.assert_called_once_with(SignupButton)


# ---- toggle callback ----

async def test_toggle_adds_and_updates_label(db, interaction):
    event_id = _add_event(db)
    await handle_signup_toggle(interaction, event_id)

    interaction.response.edit_message.assert_awaited_once()
    new_view = interaction.response.edit_message.call_args.kwargs["view"]
    assert new_view.children[0].label == "✅ Going (1)"
    assert new_view.children[0].custom_id == f"signup:{event_id}"
    args, kwargs = interaction.followup.send.call_args
    assert "on the list" in args[0] and kwargs["ephemeral"] is True
    with db() as s:
        assert count_signups(s, event_id) == 1


async def test_toggle_again_removes(db, interaction):
    event_id = _add_event(db)
    await handle_signup_toggle(interaction, event_id)
    interaction.response.edit_message.reset_mock()
    interaction.followup.send.reset_mock()
    await handle_signup_toggle(interaction, event_id)

    new_view = interaction.response.edit_message.call_args.kwargs["view"]
    assert new_view.children[0].label == "✅ Going (0)"
    args, kwargs = interaction.followup.send.call_args
    assert "Removed" in args[0] and kwargs["ephemeral"] is True


async def test_count_includes_other_users(db, interaction):
    event_id = _add_event(db)
    with db() as s:
        toggle_signup(s, GUILD, event_id, 200)
    await handle_signup_toggle(interaction, event_id)
    new_view = interaction.response.edit_message.call_args.kwargs["view"]
    assert new_view.children[0].label == "✅ Going (2)"


async def _assert_ended(interaction, db, event_id):
    interaction.response.edit_message.assert_not_awaited()
    args, kwargs = interaction.response.send_message.call_args
    assert args[0] == "This event has ended." and kwargs["ephemeral"] is True
    with db() as s:
        assert count_signups(s, event_id) == 0


async def test_inactive_event_rejected(db, interaction):
    event_id = _add_event(db, active=False)
    await handle_signup_toggle(interaction, event_id)
    await _assert_ended(interaction, db, event_id)


async def test_missing_event_rejected(db, interaction):
    await handle_signup_toggle(interaction, 12345)
    await _assert_ended(interaction, db, 12345)


async def test_other_guild_event_rejected(db, interaction):
    event_id = _add_event(db, guild_id=GUILD + 1)
    await handle_signup_toggle(interaction, event_id)
    await _assert_ended(interaction, db, event_id)


# ---- /board signups + autocomplete ----

@pytest.fixture
def cog():
    return AdminCog(MagicMock())


def _members(mapping):
    return MagicMock(get_member=MagicMock(side_effect=lambda uid: mapping.get(uid)))


async def test_signups_lists_names(db, cog, interaction):
    event_id = _add_event(db)
    with db() as s:
        toggle_signup(s, GUILD, event_id, 1)
        toggle_signup(s, GUILD, event_id, 2)
        toggle_signup(s, GUILD, event_id, 3)
    interaction.guild = _members({1: SimpleNamespace(display_name="Alice"),
                                  2: SimpleNamespace(display_name="Bob")})
    await cog.board_signups.callback(cog, interaction, str(event_id))

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    args, kwargs = interaction.followup.send.call_args
    assert kwargs["ephemeral"] is True
    assert "Alice" in args[0] and "Bob" in args[0]
    assert "3" in args[0]  # unresolved member still counted
    assert kwargs["allowed_mentions"].users is False


async def test_signups_escapes_display_names(db, cog, interaction):
    event_id = _add_event(db)
    with db() as s:
        toggle_signup(s, GUILD, event_id, 1)
    interaction.guild = _members({1: SimpleNamespace(display_name="@everyone")})
    await cog.board_signups.callback(cog, interaction, str(event_id))
    assert "@everyone" not in interaction.followup.send.call_args.args[0].replace("@\u200beveryone", "")


async def test_signups_empty(db, cog, interaction):
    event_id = _add_event(db)
    interaction.guild = _members({})
    await cog.board_signups.callback(cog, interaction, str(event_id))
    assert interaction.followup.send.call_args.args[0] == "No signups yet."


@pytest.mark.parametrize("value", ["abc", "9999"])
async def test_signups_unknown_event(db, cog, interaction, value):
    interaction.guild = _members({})
    await cog.board_signups.callback(cog, interaction, value)
    assert interaction.followup.send.call_args.args[0] == "Event not found."


async def test_signups_other_guild_event_not_found(db, cog, interaction):
    event_id = _add_event(db, guild_id=GUILD + 1)
    interaction.guild = _members({})
    await cog.board_signups.callback(cog, interaction, str(event_id))
    assert interaction.followup.send.call_args.args[0] == "Event not found."


async def test_autocomplete_active_events_capped(db, cog, interaction):
    for n in range(30):
        _add_event(db, title=f"Event {n}", ext=f"wp:{n}")
    _add_event(db, title="Ended one", ext="wp:x", active=False)
    _add_event(db, title="Foreign", ext="wp:f", guild_id=GUILD + 1)
    choices = await cog.signups_event_autocomplete(interaction, "")
    assert len(choices) == 25
    assert all(isinstance(c.value, str) and c.value.isdigit() for c in choices)
    assert not any(c.name in ("Ended one", "Foreign") for c in choices)


async def test_autocomplete_filters_by_current(db, cog, interaction):
    _add_event(db, title="Commander Night", ext="wp:1")
    _add_event(db, title="Draft Night", ext="wp:2")
    choices = await cog.signups_event_autocomplete(interaction, "draft")
    assert [c.name for c in choices] == ["Draft Night"]


# ---- board integration ----

class _Msg:
    def __init__(self, channel, mid, embed, view):
        self.channel, self.id, self.embed, self.view = channel, mid, embed, view
        self.author = SimpleNamespace(id=42)
        self.edits = []

    async def edit(self, **kwargs):
        self.embed = kwargs["embed"]
        self.edits.append(kwargs)
        if "view" in kwargs:
            self.view = kwargs["view"]

    async def delete(self):
        self.channel.messages.pop(self.id, None)


class _Channel:
    id = 111

    def __init__(self):
        self.messages = {}
        self.sends = []
        self._next = 1000

    async def send(self, **kwargs):
        self._next += 1
        msg = _Msg(self, self._next, kwargs["embed"], kwargs.get("view"))
        self.messages[msg.id] = msg
        self.sends.append(kwargs)
        return msg

    async def fetch_message(self, mid):
        return self.messages[mid]


def _board_bot(channel):
    bot = MagicMock()
    bot.user.id = 42
    bot.get_channel = MagicMock(return_value=channel)
    return bot


async def test_week_cards_get_signup_view_with_db_count(session):
    session.add(GuildConfig(guild_id=GUILD, timezone="Australia/Perth", this_week_channel_id=111,
                            week_start_mode="today_start", include_tba_section=True))
    session.add(Event(guild_id=GUILD, external_id="wp:1", title="Draft Night", url="https://x",
                      starts_at=datetime.now(TZ) + timedelta(days=1), time_known=True,
                      is_active=True))
    session.commit()
    event = session.query(Event).one()
    toggle_signup(session, GUILD, event.id, 1)
    toggle_signup(session, GUILD, event.id, 2)

    channel = _Channel()
    assert await upsert_board(_board_bot(channel), session, GUILD, "this_week") == "created"

    header, card = channel.sends
    assert "view" not in header
    assert card["view"].children[0].custom_id == f"signup:{event.id}"
    assert card["view"].children[0].label == "✅ Going (2)"


async def test_count_change_refreshes_card_on_next_edit(session):
    session.add(GuildConfig(guild_id=GUILD, timezone="Australia/Perth", this_week_channel_id=111,
                            week_start_mode="today_start", include_tba_section=True))
    session.add(Event(guild_id=GUILD, external_id="wp:1", title="Draft Night", url="https://x",
                      starts_at=datetime.now(TZ) + timedelta(days=1), time_known=True,
                      is_active=True))
    session.commit()
    event = session.query(Event).one()
    channel = _Channel()
    bot = _board_bot(channel)
    await upsert_board(bot, session, GUILD, "this_week")
    assert await upsert_board(bot, session, GUILD, "this_week") == "skipped"

    toggle_signup(session, GUILD, event.id, 7)
    assert await upsert_board(bot, session, GUILD, "this_week") == "edited"

    post = get_board_post(session, GUILD, "this_week")
    card = channel.messages[json.loads(post.message_ids)[0]]
    assert card.edits[-1]["view"].children[0].label == "✅ Going (1)"
    assert await upsert_board(bot, session, GUILD, "this_week") == "skipped"
