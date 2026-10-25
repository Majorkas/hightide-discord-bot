"""Signup buttons on week cards + /board signups."""
import asyncio
import json
import re
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import discord
import pytest

from src.bot import create_bot
from sqlalchemy import select
from sqlalchemy.orm.exc import ObjectDeletedError, StaleDataError

from src.db.models import Base, Event, EventSignup, GuildConfig, WeeklyRecurring
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


_DEFAULT = object()


def _add_event(db, title="Draft Night", guild_id=GUILD, active=True, ext="wp:1",
               starts_at=_DEFAULT):
    if starts_at is _DEFAULT:
        starts_at = datetime.now(TZ) + timedelta(days=1)
    with db() as s:
        e = Event(guild_id=guild_id, external_id=ext, title=title, url="https://x/e",
                  starts_at=starts_at, time_known=True,
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
    from src.discord_ui.role_views import NextWeekButton
    # DecisionAction is registered on the decision bot (it authors those cards).
    bot.add_dynamic_items.assert_called_once_with(SignupButton, NextWeekButton)


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


def _not_found():
    return discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Member")


def _members(mapping, fetched=None):
    """Guild stub: get_member hits the cache map; fetch_member hits `fetched` else NotFound."""
    fetched = fetched or {}

    async def fetch(uid):
        if uid in fetched:
            return fetched[uid]
        raise _not_found()

    return MagicMock(get_member=MagicMock(side_effect=lambda uid: mapping.get(uid)),
                     fetch_member=AsyncMock(side_effect=fetch))


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


@pytest.mark.parametrize("value", ["abc", "9999", "²", "-1", "", "1" * 19, "9" * 40])
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
    assert not any(c.name.startswith(("Ended one", "Foreign")) for c in choices)


async def test_autocomplete_filters_by_current(db, cog, interaction):
    _add_event(db, title="Commander Night", ext="wp:1")
    _add_event(db, title="Draft Night", ext="wp:2")
    choices = await cog.signups_event_autocomplete(interaction, "draft")
    assert [c.name.split(" — ")[0] for c in choices] == ["Draft Night"]


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
    assert await upsert_board(bot, session, GUILD, "this_week") == "edited"  # header re-sync

    toggle_signup(session, GUILD, event.id, 7)
    assert await upsert_board(bot, session, GUILD, "this_week") == "edited"

    post = get_board_post(session, GUILD, "this_week")
    card = channel.messages[json.loads(post.message_ids)[0]]
    assert card.edits[-1]["view"].children[0].label == "✅ Going (1)"
    assert await upsert_board(bot, session, GUILD, "this_week") == "edited"  # header re-sync


# ---- council batch: restart dispatch ----

def _signup_message(event_id):
    from discord.components import ActionRow as ActionRowComponent
    row = SignupView(event_id).to_components()[0]
    return SimpleNamespace(id=555, flags=SimpleNamespace(components_v2=False),
                           components=[ActionRowComponent(row)])


def _dispatch(bot, custom_id, message):
    interaction = MagicMock()
    interaction.message = message
    interaction.data = {"custom_id": custom_id, "component_type": 2}
    bot._connection._view_store.dispatch_view(2, custom_id, interaction)
    return interaction


def _dynamic_tasks():
    return [t for t in asyncio.all_tasks() if t.get_name().startswith("discord-ui-dynamic-item")]


async def test_restart_dispatch_routes_signup_custom_id(monkeypatch):
    bot = create_bot()
    bot.add_dynamic_items(SignupButton)
    handler = AsyncMock()
    monkeypatch.setattr(signup_service, "handle_signup_toggle", handler)

    interaction = _dispatch(bot, "signup:42", _signup_message(42))
    tasks = _dynamic_tasks()
    assert len(tasks) == 1
    await asyncio.gather(*tasks)

    handler.assert_awaited_once_with(interaction, 42)


@pytest.mark.parametrize("custom_id", ["signup:abc", "signup:-1", "signup:", "signup:" + "1" * 19,
                                       "signup:12x", "xsignup:1"])
async def test_restart_dispatch_rejects_malformed_custom_id(monkeypatch, custom_id):
    bot = create_bot()
    bot.add_dynamic_items(SignupButton)
    handler = AsyncMock()
    monkeypatch.setattr(signup_service, "handle_signup_toggle", handler)

    _dispatch(bot, custom_id, _signup_message(1))
    assert _dynamic_tasks() == []
    await asyncio.sleep(0)
    handler.assert_not_awaited()


def test_template_accepts_18_digit_id_only():
    pattern = SignupButton.__discord_ui_compiled_template__
    assert pattern.fullmatch("signup:" + "9" * 18)
    assert not pattern.fullmatch("signup:" + "9" * 19)
    assert not pattern.fullmatch("signup:\u00b2")


# ---- handler: double click and failure paths ----

async def test_rapid_double_click_toggles_on_then_off(db, interaction):
    event_id = _add_event(db)
    await handle_signup_toggle(interaction, event_id)
    await handle_signup_toggle(interaction, event_id)

    edits = interaction.response.edit_message.call_args_list
    assert [c.kwargs["view"].children[0].label for c in edits] == \
        ["✅ Going (1)", "✅ Going (0)"]
    with db() as s:
        assert count_signups(s, event_id) == 0


def _not_found_http():
    return discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Message")


async def test_edit_failure_sends_note_and_keeps_row(db, interaction):
    event_id = _add_event(db)
    interaction.response.is_done = MagicMock(return_value=False)
    interaction.response.edit_message = AsyncMock(side_effect=_not_found_http())

    await handle_signup_toggle(interaction, event_id)

    args, kwargs = interaction.response.send_message.call_args
    assert "signup was recorded" in args[0] and kwargs["ephemeral"] is True
    interaction.followup.send.assert_not_awaited()
    with db() as s:
        assert count_signups(s, event_id) == 1


async def test_edit_failure_note_uses_followup_when_response_done(db, interaction):
    event_id = _add_event(db)
    interaction.response.is_done = MagicMock(return_value=True)
    interaction.response.edit_message = AsyncMock(
        side_effect=discord.HTTPException(SimpleNamespace(status=500, reason="x"), "boom"))

    await handle_signup_toggle(interaction, event_id)

    interaction.response.send_message.assert_not_awaited()
    assert "recorded" in interaction.followup.send.call_args.args[0]


async def test_edit_and_note_both_failing_does_not_raise(db, interaction):
    event_id = _add_event(db)
    interaction.response.is_done = MagicMock(return_value=False)
    interaction.response.edit_message = AsyncMock(side_effect=_not_found_http())
    interaction.response.send_message = AsyncMock(side_effect=_not_found_http())

    await handle_signup_toggle(interaction, event_id)

    with db() as s:
        assert count_signups(s, event_id) == 1


async def test_confirmation_followup_failure_does_not_raise(db, interaction):
    event_id = _add_event(db)
    interaction.followup.send = AsyncMock(side_effect=_not_found_http())

    await handle_signup_toggle(interaction, event_id)

    interaction.response.edit_message.assert_awaited_once()
    with db() as s:
        assert count_signups(s, event_id) == 1


async def test_db_error_is_acknowledged_with_note(db, interaction, monkeypatch):
    from sqlalchemy.exc import OperationalError
    event_id = _add_event(db)
    interaction.response.is_done = MagicMock(return_value=False)

    def boom(*a, **k):
        raise OperationalError("stmt", {}, Exception("database is locked"))

    monkeypatch.setattr(signup_service, "toggle_signup", boom)
    await handle_signup_toggle(interaction, event_id)

    interaction.response.edit_message.assert_not_awaited()
    args, kwargs = interaction.response.send_message.call_args
    assert "try again" in args[0] and kwargs["ephemeral"] is True


@pytest.mark.filterwarnings("ignore:DELETE statement")
def test_toggle_remove_tolerates_row_deleted_concurrently(tmp_path):
    from unittest import mock
    url = f"sqlite:///{tmp_path / 'race.db'}"
    engine = get_engine(url)
    Base.metadata.create_all(engine)
    factory = get_session_factory(url)
    try:
        with factory() as setup:
            setup.add(Event(guild_id=GUILD, external_id="wp:1", title="T", url="https://x"))
            setup.commit()
            toggle_signup(setup, GUILD, 1, 100)
        a, b = factory(), factory()
        stale = a.execute(select(EventSignup)).scalar_one()
        real_execute = a.execute

        def execute(stmt, *args, **kwargs):
            if not execute.called:
                execute.called = True
                b.delete(b.get(EventSignup, stale.id))  # concurrent remove wins
                b.commit()
                return SimpleNamespace(scalar_one_or_none=lambda: stale)
            return real_execute(stmt, *args, **kwargs)

        execute.called = False
        with mock.patch.object(a, "execute", side_effect=execute):
            assert toggle_signup(a, GUILD, 1, 100) == (False, 0)
        a.close()
        b.close()
    finally:
        engine.dispose()
        factory.kw["bind"].dispose()


@pytest.mark.parametrize("make_exc", [
    lambda: StaleDataError("gone"),
    lambda: ObjectDeletedError(SimpleNamespace(class_=EventSignup, obj=lambda: None)),
])
def test_toggle_remove_counts_stale_delete_as_removed(session, monkeypatch, make_exc):
    exc = make_exc()
    session.add(Event(guild_id=GUILD, external_id="wp:1", title="T", url="https://x"))
    session.commit()
    toggle_signup(session, GUILD, 1, 100)
    real_commit = session.commit
    calls = []

    def commit():
        if not calls:
            calls.append(1)
            raise exc
        real_commit()

    monkeypatch.setattr(session, "commit", commit)
    added, count = toggle_signup(session, GUILD, 1, 100)
    assert added is False and count == 1  # row still exists in this simulated failure
    assert calls == [1]


# ---- autocomplete ----

async def test_autocomplete_truncates_to_100_chars(db, cog, interaction):
    event_id = _add_event(db, title="X" * 150)
    choices = await cog.signups_event_autocomplete(interaction, "")
    assert len(choices) == 1 and len(choices[0].name) == 100
    assert choices[0].value == str(event_id)


async def test_autocomplete_is_case_insensitive(db, cog, interaction):
    _add_event(db, title="Draft Night", ext="wp:1")
    _add_event(db, title="Commander", ext="wp:2")
    assert len(await cog.signups_event_autocomplete(interaction, "DrAfT")) == 1
    assert len(await cog.signups_event_autocomplete(interaction, "  night ")) == 1


async def test_autocomplete_orders_dated_ascending_then_undated(db, cog, interaction):
    base = datetime(2026, 11, 1, 10, 0, tzinfo=TZ)
    _add_event(db, title="Zed undated", ext="wp:1", starts_at=None)
    _add_event(db, title="Late", ext="wp:2", starts_at=base + timedelta(days=3))
    _add_event(db, title="Alpha undated", ext="wp:3", starts_at=None)
    _add_event(db, title="Early", ext="wp:4", starts_at=base)
    _add_event(db, title="Early B", ext="wp:5", starts_at=base)
    choices = await cog.signups_event_autocomplete(interaction, "")
    assert [c.name for c in choices] == [
        "Early — Sun 01 Nov", "Early B — Sun 01 Nov", "Late — Wed 04 Nov",
        "Alpha undated", "Zed undated"]


async def test_autocomplete_label_uses_guild_timezone(db, cog, interaction):
    # Time-TBC event at local midnight Perth = 16:00 UTC the previous day.
    _add_event(db, title="TBC Night", starts_at=datetime(2026, 11, 5, 0, 0, tzinfo=TZ))
    with db() as s:
        s.add(GuildConfig(guild_id=GUILD, timezone="Australia/Perth"))
        s.commit()
    choices = await cog.signups_event_autocomplete(interaction, "")
    assert [c.name for c in choices] == ["TBC Night — Thu 05 Nov"]


async def test_autocomplete_other_guild_does_not_leak(db, cog, interaction):
    _add_event(db, title="Mine", ext="wp:1")
    interaction.guild_id = GUILD + 1
    assert await cog.signups_event_autocomplete(interaction, "") == []


# ---- /board signups ----

async def test_signups_truncates_long_lists(db, cog, interaction):
    event_id = _add_event(db)
    with db() as s:
        for uid in range(1, 151):
            toggle_signup(s, GUILD, event_id, uid)
    interaction.guild = _members({uid: SimpleNamespace(display_name="n" * 30)
                                  for uid in range(1, 151)})
    await cog.board_signups.callback(cog, interaction, str(event_id))
    text = interaction.followup.send.call_args.args[0]
    assert len(text) == commands_admin._MAX_MESSAGE_CHARS and text.endswith("…")


async def test_signups_for_ended_event_still_listed(db, cog, interaction):
    event_id = _add_event(db)
    with db() as s:
        toggle_signup(s, GUILD, event_id, 1)
        s.get(Event, event_id).is_active = False
        s.commit()
    interaction.guild = _members({1: SimpleNamespace(display_name="Alice")})
    await cog.board_signups.callback(cog, interaction, str(event_id))
    assert "Alice" in interaction.followup.send.call_args.args[0]


async def test_signups_name_falls_back_to_fetch_member(db, cog, interaction):
    event_id = _add_event(db)
    with db() as s:
        toggle_signup(s, GUILD, event_id, 7)
    interaction.guild = _members({}, fetched={7: SimpleNamespace(display_name="Fetched")})
    await cog.board_signups.callback(cog, interaction, str(event_id))
    interaction.guild.fetch_member.assert_awaited_once_with(7)
    assert "Fetched" in interaction.followup.send.call_args.args[0]


async def test_signups_cached_member_skips_fetch(db, cog, interaction):
    event_id = _add_event(db)
    with db() as s:
        toggle_signup(s, GUILD, event_id, 7)
    interaction.guild = _members({7: SimpleNamespace(display_name="Cached")})
    await cog.board_signups.callback(cog, interaction, str(event_id))
    interaction.guild.fetch_member.assert_not_awaited()


async def test_signups_unresolvable_member_shows_user_id(db, cog, interaction):
    event_id = _add_event(db)
    with db() as s:
        toggle_signup(s, GUILD, event_id, 7)
        toggle_signup(s, GUILD, event_id, 8)
    interaction.guild = _members({})
    interaction.guild.fetch_member = AsyncMock(side_effect=[
        _not_found(), discord.HTTPException(SimpleNamespace(status=500, reason="x"), "boom")])
    await cog.board_signups.callback(cog, interaction, str(event_id))
    text = interaction.followup.send.call_args.args[0]
    assert "User 7" in text and "User 8" in text


# ---- buttons only on event cards ----

async def test_only_event_cards_carry_views(session):
    session.add(GuildConfig(guild_id=GUILD, timezone="Australia/Perth", this_week_channel_id=111,
                            week_start_mode="today_start", include_tba_section=True,
                            weekly_image_url="https://x/img.png"))
    base = datetime.now(TZ).replace(hour=12, minute=0) + timedelta(days=1)
    for n in range(8):
        session.add(Event(guild_id=GUILD, external_id=f"wp:{n}", title=f"Event {n}",
                          url="https://x", starts_at=base + timedelta(minutes=n),
                          time_known=True, is_active=True))
    session.add(Event(guild_id=GUILD, external_id="wp:tba", title="Undated", url="https://x",
                      is_active=True))
    session.add(WeeklyRecurring(guild_id=GUILD, title="Door night", days_text="Mon",
                                time_text="6:00 PM"))
    session.commit()

    channel = _Channel()
    bot = _board_bot(channel)
    await upsert_board(bot, session, GUILD, "this_week")

    by_title = {s["embed"].title: s for s in channel.sends}
    assert len(channel.sends) == 11
    # event cards carry SignupView; the header carries NextWeekView; nothing else has a view
    from src.discord_ui.role_views import NextWeekView, SignupView
    assert isinstance(by_title["This Week at Hightide"]["view"], NextWeekView)
    for title in ("Also this week", "Every week at Hightide", "Date TBA"):
        assert "view" not in by_title[title]
    assert sum(1 for s in channel.sends if isinstance(s.get("view"), SignupView)) == 7

    cfg = session.get(GuildConfig, GUILD)
    cfg.weekly_image_url = "https://x/other.png"
    session.commit()
    await upsert_board(bot, session, GUILD, "this_week")
    edited = [e for m in channel.messages.values() for e in m.edits]
    # only the weekly card re-edits (image change); the header re-sync carries its NextWeek view
    assert edited and all(isinstance(e.get("view"), (SignupView, NextWeekView)) or "view" not in e
                          for e in edited)


# ---- recreate path ----

async def test_recreate_this_week_keeps_signup_counts(db, interaction):
    with db() as s:
        s.add(GuildConfig(guild_id=GUILD, timezone="Australia/Perth", this_week_channel_id=111,
                          week_start_mode="today_start", include_tba_section=True))
        s.commit()
    first = _add_event(db, title="First", ext="wp:1")
    second = _add_event(db, title="Second", ext="wp:2",
                        starts_at=datetime.now(TZ) + timedelta(days=2))
    with db() as s:
        toggle_signup(s, GUILD, first, 1)
        toggle_signup(s, GUILD, first, 2)
        toggle_signup(s, GUILD, second, 3)

    channel = _Channel()
    cog = AdminCog(_board_bot(channel))
    await cog.board_recreate.callback(cog, interaction, "this_week")

    from src.discord_ui.role_views import SignupView
    cards = {s["view"].children[0].custom_id: s["view"].children[0].label
             for s in channel.sends if isinstance(s.get("view"), SignupView)}
    assert cards == {f"signup:{first}": "✅ Going (2)", f"signup:{second}": "✅ Going (1)"}
    assert "recreate failed" not in str(interaction.followup.send.call_args)


# ---- model ----

def test_event_ids_are_never_reused(session):
    session.add(Event(guild_id=GUILD, external_id="wp:1", title="A", url="https://x"))
    session.commit()
    first_id = session.query(Event).one().id
    session.delete(session.get(Event, first_id))
    session.commit()
    session.add(Event(guild_id=GUILD, external_id="wp:2", title="B", url="https://x"))
    session.commit()
    assert session.query(Event).one().id > first_id
