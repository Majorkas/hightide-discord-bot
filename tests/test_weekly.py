import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import discord
import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from src.db.models import Base, Event, GuildConfig, WeeklyRecurring
from src.db.repositories import get_board_post, get_or_create_config
from src.db.session import get_engine, get_session_factory
from src.discord_ui import commands_admin
from src.discord_ui.commands_admin import AdminCog
from src.discord_ui.embeds import build_weekly_embed, build_week_board_items
from src.services import board_service
from src.services import weekly_service
from src.services.board_service import _should_recreate_week_set, upsert_board
from src.services.weekly_service import seed_weekly_recurring
from tests.test_week_board_set import GUILD, FakeChannel, _bot, _seed

TZ = ZoneInfo("Australia/Perth")
NOW = datetime(2026, 10, 23, 10, 0, tzinfo=TZ)
IMG = "https://example.com/weekly.png"


def _row(title="Chess", days="Mon", time="6:00 PM", sort=0, enabled=True, ends_on=None):
    return WeeklyRecurring(guild_id=GUILD, title=title, days_text=days, time_text=time,
                           sort_order=sort, enabled=enabled, ends_on=ends_on)


def _add_rows(session, *rows):
    session.add_all(rows)
    session.commit()


# --- model -------------------------------------------------------------------------

def test_weekly_table_round_trip(session):
    session.add(WeeklyRecurring(guild_id=1, title="T", days_text="Mon", time_text="6:00 PM"))
    session.commit()
    row = session.execute(select(WeeklyRecurring)).scalar_one()
    assert row.id is not None
    assert (row.sort_order, row.enabled, row.ends_on) == (0, True, None)
    row.ends_on = "2026-12-09"
    session.commit()
    assert session.get(WeeklyRecurring, row.id).ends_on == "2026-12-09"


def test_guild_config_weekly_image_url_column(session):
    session.add(GuildConfig(guild_id=1))
    session.commit()
    assert session.get(GuildConfig, 1).weekly_image_url is None
    session.get(GuildConfig, 1).weekly_image_url = IMG
    session.commit()
    assert session.get(GuildConfig, 1).weekly_image_url == IMG


# --- seeding -----------------------------------------------------------------------

def test_seed_inserts_six_rows_once(session):
    assert seed_weekly_recurring(session, 1) == 6
    assert seed_weekly_recurring(session, 1) == 0
    rows = session.execute(select(WeeklyRecurring).order_by(WeeklyRecurring.sort_order)).scalars().all()
    assert [r.sort_order for r in rows] == [0, 1, 2, 3, 4, 5]
    assert (rows[0].title, rows[0].days_text, rows[0].time_text) == ("DND Fremantle", "Mon & Thu", "6:00 PM")
    assert rows[5].title == "Mordheim: The Cursed City Campaign"
    assert rows[5].ends_on == "2026-12-09"
    assert all(r.enabled for r in rows)
    assert [r.ends_on for r in rows[:5]] == [None] * 5


def test_seed_is_per_guild(session):
    seed_weekly_recurring(session, 1)
    assert seed_weekly_recurring(session, 2) == 6
    for g in (1, 2):
        assert len(session.execute(select(WeeklyRecurring).where(
            WeeklyRecurring.guild_id == g)).scalars().all()) == 6


def test_seed_skipped_when_guild_has_any_row(session):
    _add_rows(session, _row())
    assert seed_weekly_recurring(session, GUILD) == 0
    assert len(session.execute(select(WeeklyRecurring)).scalars().all()) == 1
    assert session.get(GuildConfig, GUILD).weekly_seeded is True


def test_seed_sets_marker_and_never_reseeds_emptied_roster(session):
    assert seed_weekly_recurring(session, GUILD) == 6
    assert session.get(GuildConfig, GUILD).weekly_seeded is True
    for row in session.execute(select(WeeklyRecurring)).scalars().all():
        session.delete(row)
    session.commit()
    assert seed_weekly_recurring(session, GUILD) == 0
    assert session.execute(select(WeeklyRecurring)).scalars().all() == []


def test_seed_second_concurrent_session_skips(session):
    other = sessionmaker(bind=session.get_bind())()
    try:
        get_or_create_config(session, GUILD)
        assert seed_weekly_recurring(session, GUILD) == 6
        assert seed_weekly_recurring(other, GUILD) == 0
    finally:
        other.close()
    assert len(session.execute(select(WeeklyRecurring)).scalars().all()) == 6


# --- rendering ---------------------------------------------------------------------

def test_weekly_embed_lines_and_escaping():
    embed = build_weekly_embed(
        [_row("Warhammer (600 pts) *x*", "Fri", "5:00 PM"), _row("DND", "Mon & Thu", "6:00 PM", sort=1)],
        None, TZ)
    assert embed.title == "Every week at Hightide"
    lines = embed.description.split("\n")
    assert lines[0] == "Recurring door-ticket-only events"
    assert "Fri · 5:00 PM — Warhammer \\(600 pts\\) \\*x\\*" in lines
    assert "Mon & Thu · 6:00 PM — DND" in lines
    assert embed.image.url is None


def test_weekly_embed_ends_on_suffix():
    embed = build_weekly_embed([_row("Mordheim", "Tue", "6:00 PM", ends_on="2026-12-09")], None, TZ)
    assert "Tue · 6:00 PM — Mordheim (until Dec 09)" in embed.description


def test_weekly_embed_image_only_valid_urls():
    assert build_weekly_embed([_row()], IMG, TZ).image.url == IMG
    assert build_weekly_embed([_row()], "javascript:alert(1)", TZ).image.url is None


def test_weekly_embed_neutralises_mentions():
    embed = build_weekly_embed([_row("@everyone")], None, TZ)
    assert "@everyone" not in embed.description


def _keys(items):
    return [k for k, _ in items]


def _items(rows, image=None, tba=False):
    events = []
    if tba:
        events.append(Event(guild_id=GUILD, external_id="wp:tba", title="TBA", url="https://x/t",
                            starts_at=None, time_known=False, is_active=True))
    return build_week_board_items(events, "T", TZ, True, weekly_rows=rows,
                                  weekly_image_url=image, now=NOW)


def test_expired_ends_on_excluded():
    items = _items([_row("Old", ends_on="2026-10-22"), _row("Keep", sort=1)])
    desc = dict(items)["weekly"].description
    assert "Old" not in desc and "Keep" in desc


def test_ends_on_today_still_shown_and_disabled_hidden():
    items = _items([_row("Today", ends_on="2026-10-23"), _row("Off", sort=1, enabled=False)])
    desc = dict(items)["weekly"].description
    assert "Today" in desc and "Off" not in desc


def test_no_current_rows_and_no_image_omits_item():
    assert "weekly" not in _keys(_items([]))
    assert "weekly" not in _keys(_items([_row(ends_on="2026-01-01")]))
    assert "weekly" not in _keys(_items([_row(enabled=False)], image=None))


def test_image_without_rows_keeps_item():
    items = dict(_items([], image=IMG))
    assert items["weekly"].image.url == IMG


def test_weekly_position_after_cards_and_overflow_before_tba():
    events = [Event(guild_id=GUILD, external_id=f"wp:{i}", title=f"e{i}", url="https://x",
                    starts_at=datetime(2026, 10, 24 + (i // 3), 10 + i % 3, tzinfo=TZ),
                    time_known=True, is_active=True) for i in range(9)]
    events.append(Event(guild_id=GUILD, external_id="wp:tba", title="tba", url="https://x",
                        starts_at=None, time_known=False, is_active=True))
    items = build_week_board_items(events, "T", TZ, True, weekly_rows=[_row()], now=NOW)
    keys = _keys(items)
    assert keys[-3:] == ["overflow", "weekly", "tba"]


def test_week_items_unchanged_without_weekly_args():
    assert _keys(build_week_board_items([], "T", TZ, True)) == ["header"]


# --- board integration -------------------------------------------------------------

def _stored(session):
    return json.loads(get_board_post(session, GUILD, "this_week").render_hash)


def _weekly_msg(session, channel):
    post = get_board_post(session, GUILD, "this_week")
    keys = _stored(session)["keys"]
    return channel.messages[json.loads(post.message_ids)[keys.index("weekly") - 1]]


async def _created_board(session, image=None, tba=False):
    _seed(session, weekly_image_url=image)
    if tba:
        session.add(Event(guild_id=GUILD, external_id="wp:tba", title="tba", url="https://x/t",
                          starts_at=None, time_known=False, is_active=True))
    _add_rows(session, _row("Chess", sort=0))
    channel = FakeChannel()
    bot = _bot(channel)
    assert await upsert_board(bot, session, GUILD, "this_week") == "created"
    channel.sends.clear()
    return bot, channel


async def test_board_includes_weekly_before_tba(session):
    await _created_board(session, tba=True)
    assert _stored(session)["keys"] == ["header", "wp:a", "wp:b", "wp:c", "weekly", "tba"]


async def test_board_without_rows_or_image_has_no_weekly(session):
    _seed(session)
    channel = FakeChannel()
    await upsert_board(_bot(channel), session, GUILD, "this_week")
    assert "weekly" not in _stored(session)["keys"]


async def test_row_added_edits_weekly_in_place(session):
    bot, channel = await _created_board(session, tba=True)
    msg = _weekly_msg(session, channel)
    ids_before = list(channel.messages)
    _add_rows(session, _row("Go", "Sat", "2:00 PM", sort=1))

    assert await upsert_board(bot, session, GUILD, "this_week") == "edited"

    assert channel.sends == [] and channel.deleted == []
    assert list(channel.messages) == ids_before
    assert "Go" in msg.embed.description and len(msg.edits) == 1


async def test_row_removed_edits_weekly_in_place(session):
    bot, channel = await _created_board(session)
    _add_rows(session, _row("Go", "Sat", "2:00 PM", sort=1))
    await upsert_board(bot, session, GUILD, "this_week")
    channel.sends.clear()
    session.delete(session.execute(select(WeeklyRecurring).where(
        WeeklyRecurring.title == "Go")).scalar_one())
    session.commit()

    assert await upsert_board(bot, session, GUILD, "this_week") == "edited"
    assert channel.sends == [] and channel.deleted == []
    assert "Go" not in _weekly_msg(session, channel).embed.description


async def test_image_change_edits_weekly_in_place(session):
    bot, channel = await _created_board(session, tba=True)
    msg = _weekly_msg(session, channel)
    session.get(GuildConfig, GUILD).weekly_image_url = IMG
    session.commit()

    assert await upsert_board(bot, session, GUILD, "this_week") == "edited"
    assert channel.sends == [] and channel.deleted == []
    assert msg.embed.image.url == IMG


async def test_unchanged_weekly_skipped(session):
    bot, channel = await _created_board(session, image=IMG, tba=True)
    assert await upsert_board(bot, session, GUILD, "this_week") == "skipped"
    assert channel.sends == [] and channel.deleted == []


async def test_expired_row_drops_off_via_edit(session):
    bot, channel = await _created_board(session)
    _add_rows(session, _row("Gone", sort=1, ends_on="2020-01-01"))
    assert await upsert_board(bot, session, GUILD, "this_week") == "skipped"
    assert "Gone" not in _weekly_msg(session, channel).embed.description


def test_same_key_weekly_change_is_not_a_recreate():
    keys = ["header", "wp:a", "weekly", "tba"]
    assert _should_recreate_week_set(keys, list(keys)) is False
    assert _should_recreate_week_set(["header", "wp:a", "tba"], ["header", "wp:a", "tba"]) is False
    # Weekly appended at the tail (no TBA) is a pure suffix append.
    assert _should_recreate_week_set(["header", "wp:a"], ["header", "wp:a", "weekly"]) is False


async def test_first_refresh_self_seeds(session, monkeypatch):
    monkeypatch.setattr(board_service, "seed_weekly_recurring", weekly_service.seed_weekly_recurring)
    _seed(session)
    channel = FakeChannel()
    await upsert_board(_bot(channel), session, GUILD, "this_week")
    assert len(session.execute(select(WeeklyRecurring)).scalars().all()) == 6
    assert "weekly" in _stored(session)["keys"]
    await upsert_board(_bot(channel), session, GUILD, "this_week")
    assert len(session.execute(select(WeeklyRecurring)).scalars().all()) == 6


async def test_this_month_board_has_no_weekly(session):
    _seed(session, this_month_channel_id=111)
    _add_rows(session, _row("Chess"))
    channel = FakeChannel()
    await upsert_board(_bot(channel), session, GUILD, "this_month")
    sent = channel.sends[0]["embed"]
    assert "Chess" not in (sent.description or "")
    assert all("Chess" not in f.value for f in sent.fields)


# --- commands ----------------------------------------------------------------------

@pytest.fixture
def db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'weekly.db'}"
    engine = get_engine(url)
    Base.metadata.create_all(engine)
    factory = get_session_factory(url)
    monkeypatch.setattr(commands_admin, "_session_factory", factory)
    yield factory
    engine.dispose()
    factory.kw["bind"].dispose()


@pytest.fixture
def cog():
    return AdminCog(MagicMock())


@pytest.fixture
def interaction():
    i = MagicMock()
    i.guild_id = GUILD
    i.response.send_message = AsyncMock()
    return i


def _reply(interaction) -> str:
    interaction.response.send_message.assert_awaited_once()
    args, kwargs = interaction.response.send_message.call_args
    assert kwargs.get("ephemeral") is True
    return args[0]


def _rows(db):
    with db() as s:
        return s.execute(select(WeeklyRecurring).order_by(WeeklyRecurring.id)).scalars().all()


def test_weekly_commands_registered_in_config_group():
    group = AdminCog(MagicMock()).config_group
    names = {c.name for c in group.commands}
    assert {"weekly_list", "weekly_add", "weekly_remove", "weekly_image"} <= names
    assert group.guild_only is True
    assert group.default_permissions == discord.Permissions(manage_guild=True)


async def test_weekly_add_appends_with_next_sort_order(db, cog, interaction):
    with db() as s:
        s.add_all([WeeklyRecurring(guild_id=GUILD, title="a", days_text="Mon", time_text="1", sort_order=4),
                   WeeklyRecurring(guild_id=GUILD + 1, title="b", days_text="Mon", time_text="1", sort_order=99)])
        s.commit()
    await cog.weekly_add.callback(cog, interaction, "Chess", "Wed", "7:00 PM")
    msg = _reply(interaction)
    assert "next refresh" in msg and "/board refresh" in msg
    added = _rows(db)[-1]
    assert (added.guild_id, added.title, added.days_text, added.time_text) == (GUILD, "Chess", "Wed", "7:00 PM")
    assert added.sort_order == 5 and added.enabled is True


async def test_weekly_add_first_row_in_empty_guild_seeds_defaults_first(db, cog, interaction):
    await cog.weekly_add.callback(cog, interaction, "Chess", "Wed", "7:00 PM")
    rows = _rows(db)
    assert len(rows) == 7 and rows[-1].title == "Chess" and rows[-1].sort_order == 6


async def test_weekly_add_after_roster_emptied_does_not_reseed(db, cog, interaction):
    with db() as s:
        seed_weekly_recurring(s, GUILD)
        for r in s.execute(select(WeeklyRecurring)).scalars().all():
            s.delete(r)
        s.commit()
    await cog.weekly_add.callback(cog, interaction, "Chess", "Wed", "7:00 PM")
    rows = _rows(db)
    assert [r.title for r in rows] == ["Chess"] and rows[0].sort_order == 0


async def test_weekly_list_after_roster_emptied_does_not_reseed(db, cog, interaction):
    with db() as s:
        seed_weekly_recurring(s, GUILD)
        for r in s.execute(select(WeeklyRecurring)).scalars().all():
            s.delete(r)
        s.commit()
    await cog.weekly_list.callback(cog, interaction)
    _reply(interaction)
    assert _rows(db) == []


@pytest.mark.parametrize("title,days,time", [
    ("", "Mon", "6 PM"), ("  ", "Mon", "6 PM"), ("T", "", "6 PM"), ("T", "Mon", " "),
    ("x" * 201, "Mon", "6 PM"), ("T", "d" * 41, "6 PM"), ("T", "Mon", "t" * 41),
])
async def test_weekly_add_rejects_invalid(db, cog, interaction, title, days, time):
    await cog.weekly_add.callback(cog, interaction, title, days, time)
    _reply(interaction)
    assert _rows(db) == []


async def test_weekly_add_accepts_max_lengths(db, cog, interaction):
    await cog.weekly_add.callback(cog, interaction, "x" * 200, "d" * 40, "t" * 40)
    _reply(interaction)
    assert len(_rows(db)) == 7


async def test_weekly_list_shows_all_rows_with_ids_and_disabled(db, cog, interaction):
    with db() as s:
        s.add_all([WeeklyRecurring(guild_id=GUILD, title="On", days_text="Mon", time_text="1 PM", sort_order=0),
                   WeeklyRecurring(guild_id=GUILD, title="Off", days_text="Tue", time_text="2 PM",
                                   sort_order=1, enabled=False, ends_on="2026-12-09"),
                   WeeklyRecurring(guild_id=GUILD + 1, title="Other", days_text="Wed", time_text="3 PM")])
        s.commit()
        ids = [r.id for r in s.execute(select(WeeklyRecurring).order_by(WeeklyRecurring.id)).scalars()]
    await cog.weekly_list.callback(cog, interaction)
    msg = _reply(interaction)
    lines = msg.split("\n")
    assert lines[0].startswith("1.") and f"`{ids[0]}`" in lines[0] and "On" in lines[0]
    assert lines[1].startswith("2.") and f"`{ids[1]}`" in lines[1] and "Off" in lines[1]
    assert "disabled" in lines[1] and "2026-12-09" in lines[1]
    assert "Other" not in msg


async def test_weekly_list_empty_seeds_defaults(db, cog, interaction):
    await cog.weekly_list.callback(cog, interaction)
    msg = _reply(interaction)
    assert "DND Fremantle" in msg and len(_rows(db)) == 6


async def test_weekly_remove_deletes_own_row(db, cog, interaction):
    with db() as s:
        s.add(WeeklyRecurring(guild_id=GUILD, title="a", days_text="Mon", time_text="1"))
        s.commit()
        rid = s.execute(select(WeeklyRecurring.id)).scalar_one()
    await cog.weekly_remove.callback(cog, interaction, rid)
    msg = _reply(interaction)
    assert "next refresh" in msg
    assert _rows(db) == []


async def test_weekly_remove_wrong_guild_or_missing_is_not_found(db, cog, interaction):
    with db() as s:
        s.add(WeeklyRecurring(guild_id=GUILD + 1, title="a", days_text="Mon", time_text="1"))
        s.commit()
        rid = s.execute(select(WeeklyRecurring.id)).scalar_one()
    await cog.weekly_remove.callback(cog, interaction, rid)
    assert "not found" in _reply(interaction)
    assert len(_rows(db)) == 1
    interaction.response.send_message.reset_mock()
    await cog.weekly_remove.callback(cog, interaction, 9999)
    assert "not found" in _reply(interaction)


async def test_weekly_image_sets_url(db, cog, interaction):
    await cog.weekly_image.callback(cog, interaction, IMG)
    msg = _reply(interaction)
    assert "next refresh" in msg
    with db() as s:
        assert s.get(GuildConfig, GUILD).weekly_image_url == IMG


@pytest.mark.parametrize("value", ["none", "NONE", " None "])
async def test_weekly_image_none_clears_url(db, cog, interaction, value):
    with db() as s:
        s.add(GuildConfig(guild_id=GUILD, weekly_image_url=IMG))
        s.commit()
    await cog.weekly_image.callback(cog, interaction, value)
    assert "cleared" in _reply(interaction)
    with db() as s:
        assert s.get(GuildConfig, GUILD).weekly_image_url is None


async def test_board_preview_this_week_includes_weekly_section(db, cog, interaction):
    with db() as s:
        s.add(GuildConfig(guild_id=GUILD, timezone="Australia/Perth", weekly_image_url=IMG))
        s.commit()
    await cog.board_preview.callback(cog, interaction, "this_week")
    _, kwargs = interaction.response.send_message.call_args
    weekly = [e for e in kwargs["embeds"] if e.title == "Every week at Hightide"]
    assert len(weekly) == 1 and "DND Fremantle" in weekly[0].description
    assert weekly[0].image.url == IMG


@pytest.mark.parametrize("url", ["ftp://x/y.png", "javascript:alert(1)", "not a url", "",
                                 "https://x/" + "a" * 500])
async def test_weekly_image_rejects_invalid_url(db, cog, interaction, url):
    await cog.weekly_image.callback(cog, interaction, url)
    assert "Invalid URL" in _reply(interaction)
    with db() as s:
        cfg = s.get(GuildConfig, GUILD)
        assert cfg is None or cfg.weekly_image_url is None
