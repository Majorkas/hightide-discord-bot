import copy
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from src.db.models import Base, Event, EventRolePing, GuildConfig
from src.db.repositories import add_interest_role, add_keyword
from src.db.session import get_engine, get_session_factory
from src.discord_ui.embeds import build_ping_message
from src.services import scheduler
from src.services.event_service import upsert_products
from src.services.role_service import match_roles_for_title, ping_new_events

GUILD = 999
CHANNEL_ID = 555
ROLE_ID = 11


def _roles():
    r1 = SimpleNamespace(id=1, label="Yu-Gi-Oh", role_id=11, enabled=True)
    r2 = SimpleNamespace(id=2, label="MTG", role_id=22, enabled=True)
    return {1: r1, 2: r2}, {1: ["yu-gi-oh", "yugioh"], 2: ["mtg", "magic the gathering"]}


# --- matching ---

def test_keyword_match_basic():
    roles_by_id, kw = _roles()
    got = match_roles_for_title("05.11.26 – Weekly Duels – Yu-Gi-Oh!", kw, roles_by_id)
    assert [r.id for r in got] == [1]


def test_no_partial_word_match():
    roles_by_id, kw = _roles()
    assert match_roles_for_title("Scmtg night", kw, roles_by_id) == []
    assert match_roles_for_title("MTGX open", kw, roles_by_id) == []
    assert [r.id for r in match_roles_for_title("FNM – MTG draft", kw, roles_by_id)] == [2]


def test_yu_does_not_match_yummy():
    roles_by_id = {1: SimpleNamespace(id=1, label="x", role_id=1, enabled=True)}
    assert match_roles_for_title("Yummy night", {1: ["yu"]}, roles_by_id) == []


def test_case_insensitive():
    roles_by_id, kw = _roles()
    assert [r.id for r in match_roles_for_title("YUGIOH locals", kw, roles_by_id)] == [1]


def test_multi_keyword_and_multi_role():
    roles_by_id, kw = _roles()
    got = match_roles_for_title("Magic the Gathering vs Yugioh showdown", kw, roles_by_id)
    assert sorted(r.id for r in got) == [1, 2]


def test_role_matched_once_when_several_keywords_hit():
    roles_by_id, kw = _roles()
    got = match_roles_for_title("MTG: Magic the Gathering draft", kw, roles_by_id)
    assert [r.id for r in got] == [2]


def test_disabled_role_filtered():
    roles_by_id, kw = _roles()
    roles_by_id[1].enabled = False
    assert match_roles_for_title("Yu-Gi-Oh locals", kw, roles_by_id) == []


def test_role_missing_from_roles_by_id_ignored():
    roles_by_id, kw = _roles()
    del roles_by_id[1]
    assert match_roles_for_title("Yu-Gi-Oh locals", kw, roles_by_id) == []


def test_keyword_with_regex_metacharacters_is_literal():
    roles_by_id = {1: SimpleNamespace(id=1, label="x", role_id=1, enabled=True)}
    assert match_roles_for_title("Anything", {1: [".*"]}, roles_by_id) == []
    assert len(match_roles_for_title("C++ night", {1: ["c++"]}, roles_by_id)) == 1


# --- message ---

def test_build_ping_message_known_time(tz):
    ev = Event(title="Yu-Gi-Oh [Locals]", url="https://example.com/e",
               starts_at=datetime(2026, 10, 23, 10, 30, tzinfo=timezone.utc), time_known=True)
    msg = build_ping_message(ev, ROLE_ID, tz)
    assert msg.startswith("<@&11> — New event: [")
    assert "\\[Locals\\]](https://example.com/e)" in msg
    assert msg.endswith(" · Fri 23 Oct · 18:30")


def test_build_ping_message_time_tbc_and_unsafe_url(tz):
    ev = Event(title="@everyone night", url="javascript:alert(1)",
               starts_at=datetime(2026, 10, 23, 0, 0, tzinfo=timezone.utc), time_known=False)
    msg = build_ping_message(ev, ROLE_ID, tz)
    assert msg.endswith("· Fri 23 Oct · time TBC")
    assert "javascript" not in msg
    assert "@everyone" not in msg.replace("@\u200beveryone", "")


# --- ping pipeline ---

@pytest.fixture
def role_env(session):
    session.add(GuildConfig(guild_id=GUILD, timezone="Australia/Perth",
                            announce_channel_id=CHANNEL_ID))
    session.commit()
    role = add_interest_role(session, GUILD, ROLE_ID, "Yu-Gi-Oh")
    add_keyword(session, role.id, "yugioh")
    add_keyword(session, role.id, "yu-gi-oh")
    return role


def _bot(*, role_exists=True, send=None):
    channel = MagicMock()
    channel.send = send or AsyncMock()
    guild = MagicMock()
    guild.get_role = MagicMock(
        return_value=SimpleNamespace(id=ROLE_ID) if role_exists else None)
    bot = MagicMock()
    bot.get_guild = MagicMock(return_value=guild)
    bot.get_channel = MagicMock(return_value=channel)
    bot.fetch_channel = AsyncMock(return_value=channel)
    return bot, channel


def _event(session, ext, title="Weekly Duels – Yu-Gi-Oh!", starts_at="future", **kw):
    now = datetime.now(timezone.utc)
    when = {"future": now + timedelta(days=30), "past": now - timedelta(days=30),
            "tba": None}[starts_at]
    ev = Event(guild_id=GUILD, external_id=ext, title=title, url="https://example.com/" + ext,
               starts_at=when, time_known=when is not None, fingerprint="f", **kw)
    session.add(ev)
    session.commit()
    return ev


async def test_new_event_pings_once_with_scoped_mentions(session, tz, role_env):
    bot, channel = _bot()
    ev = _event(session, "wp:1")
    sent = await ping_new_events(bot, session, GUILD, [ev], tz)
    assert sent == 1
    channel.send.assert_awaited_once()
    args, kwargs = channel.send.await_args
    content = args[0] if args else kwargs["content"]
    assert content.startswith("<@&11> — New event: [")
    mentions = kwargs["allowed_mentions"]
    assert [r.id for r in mentions.roles] == [ROLE_ID]
    assert mentions.everyone is False
    assert mentions.users is False
    assert session.query(EventRolePing).count() == 1


async def test_same_event_not_pinged_twice(session, tz, role_env):
    bot, channel = _bot()
    ev = _event(session, "wp:1")
    assert await ping_new_events(bot, session, GUILD, [ev], tz) == 1
    assert await ping_new_events(bot, session, GUILD, [ev], tz) == 0
    assert channel.send.await_count == 1


async def test_unchanged_rerefresh_sends_nothing(session, tz, role_env, fixture_products):
    bot, channel = _bot()
    products = copy.deepcopy(fixture_products)
    product = next(p for p in products if "weekly-duels" in p["slug"])
    product["title"]["rendered"] = "Weekly Duels – Yu-Gi-Oh!"
    first = upsert_products(session, GUILD, [product], tz)
    second = upsert_products(session, GUILD, [product], tz)
    assert len(first["created_events"]) == 1
    assert second["created_events"] == []
    await ping_new_events(bot, session, GUILD, second["created_events"], tz)
    assert channel.send.await_count == 0


async def test_unset_announce_channel_sends_nothing(session, tz, role_env):
    cfg = session.get(GuildConfig, GUILD)
    cfg.announce_channel_id = None
    session.commit()
    bot, channel = _bot()
    ev = _event(session, "wp:1")
    assert await ping_new_events(bot, session, GUILD, [ev], tz) == 0
    channel.send.assert_not_awaited()
    assert session.query(EventRolePing).count() == 0


async def test_deleted_role_skipped_and_logged(session, tz, role_env, caplog):
    bot, channel = _bot(role_exists=False)
    ev = _event(session, "wp:1")
    with caplog.at_level(logging.WARNING):
        assert await ping_new_events(bot, session, GUILD, [ev], tz) == 0
    channel.send.assert_not_awaited()
    assert session.query(EventRolePing).count() == 0
    assert caplog.records


async def test_disabled_role_not_pinged(session, tz, role_env):
    role_env.enabled = False
    session.commit()
    bot, channel = _bot()
    ev = _event(session, "wp:1")
    assert await ping_new_events(bot, session, GUILD, [ev], tz) == 0
    channel.send.assert_not_awaited()


async def test_non_matching_event_not_pinged(session, tz, role_env):
    bot, channel = _bot()
    ev = _event(session, "wp:1", title="Painting night")
    assert await ping_new_events(bot, session, GUILD, [ev], tz) == 0
    channel.send.assert_not_awaited()


async def test_past_event_not_pinged(session, tz, role_env):
    bot, channel = _bot()
    ev = _event(session, "wp:1", starts_at="past")
    assert await ping_new_events(bot, session, GUILD, [ev], tz) == 0
    channel.send.assert_not_awaited()
    assert session.query(EventRolePing).count() == 0


async def test_tba_event_is_pinged(session, tz, role_env):
    bot, channel = _bot()
    ev = _event(session, "wp:1", starts_at="tba")
    assert await ping_new_events(bot, session, GUILD, [ev], tz) == 1
    channel.send.assert_awaited_once()


async def test_send_failure_not_recorded_then_retries(session, tz, role_env):
    resp = MagicMock(status=403, reason="Forbidden")
    send = AsyncMock(side_effect=discord.Forbidden(resp, "nope"))
    bot, channel = _bot(send=send)
    ev = _event(session, "wp:1")
    assert await ping_new_events(bot, session, GUILD, [ev], tz) == 0
    assert session.query(EventRolePing).count() == 0
    channel.send = AsyncMock()
    assert await ping_new_events(bot, session, GUILD, [ev], tz) == 1
    assert session.query(EventRolePing).count() == 1


async def test_send_failure_continues_with_remaining(session, tz, role_env):
    resp = MagicMock(status=404, reason="Not Found")
    send = AsyncMock(side_effect=[discord.NotFound(resp, "gone"), None])
    bot, channel = _bot(send=send)
    e1 = _event(session, "wp:1")
    e2 = _event(session, "wp:2")
    assert await ping_new_events(bot, session, GUILD, [e1, e2], tz) == 1
    assert session.query(EventRolePing).count() == 1


async def test_missing_channel_returns_zero(session, tz, role_env):
    bot, _ = _bot()
    bot.get_channel = MagicMock(return_value=None)
    resp = MagicMock(status=404, reason="Not Found")
    bot.fetch_channel = AsyncMock(side_effect=discord.NotFound(resp, "gone"))
    ev = _event(session, "wp:1")
    assert await ping_new_events(bot, session, GUILD, [ev], tz) == 0


# --- scheduler hook ---

@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'test.db'}"
    engine = get_engine(url)
    Base.metadata.create_all(engine)
    factory = get_session_factory(url)
    monkeypatch.setattr(scheduler, "_session_factory", factory)
    with factory() as s:
        s.add(GuildConfig(guild_id=GUILD, timezone="Australia/Perth",
                          this_week_channel_id=111, this_month_channel_id=222,
                          announce_channel_id=CHANNEL_ID))
        s.commit()
    yield factory
    engine.dispose()
    factory.kw["bind"].dispose()


def _patch_boards(monkeypatch):
    monkeypatch.setattr("src.services.scheduler.upsert_board", AsyncMock(return_value="skipped"))


async def test_run_daily_refresh_pings_created_events(temp_db, monkeypatch):
    _patch_boards(monkeypatch)
    created = [object()]
    monkeypatch.setattr("src.services.scheduler.refresh_events",
                        MagicMock(return_value={"created": 1, "created_events": created}))
    ping = AsyncMock(return_value=2)
    monkeypatch.setattr("src.services.scheduler.ping_new_events", ping)

    result = await scheduler.run_daily_refresh(MagicMock(), GUILD)

    ping.assert_awaited_once()
    assert ping.await_args.args[3] is created
    assert result["pings"] == 2
    assert "created_events" not in result["events"]


async def test_run_daily_refresh_cached_path_does_not_ping(temp_db, monkeypatch):
    _patch_boards(monkeypatch)
    ping = AsyncMock()
    monkeypatch.setattr("src.services.scheduler.ping_new_events", ping)
    result = await scheduler.run_daily_refresh(MagicMock(), GUILD, fetch=False)
    ping.assert_not_awaited()
    assert "pings" not in result


async def test_run_daily_refresh_failed_events_step_does_not_ping(temp_db, monkeypatch):
    _patch_boards(monkeypatch)
    monkeypatch.setattr("src.services.scheduler.refresh_events",
                        MagicMock(side_effect=ConnectionError("boom")))
    ping = AsyncMock()
    monkeypatch.setattr("src.services.scheduler.ping_new_events", ping)
    result = await scheduler.run_daily_refresh(MagicMock(), GUILD)
    ping.assert_not_awaited()
    assert result["events"] == "error:ConnectionError"


async def test_ping_failure_isolated_from_boards(temp_db, monkeypatch):
    upsert = AsyncMock(return_value="skipped")
    monkeypatch.setattr("src.services.scheduler.upsert_board", upsert)
    monkeypatch.setattr("src.services.scheduler.refresh_events",
                        MagicMock(return_value={"created_events": [object()]}))
    monkeypatch.setattr("src.services.scheduler.ping_new_events",
                        AsyncMock(side_effect=RuntimeError("x")))
    result = await scheduler.run_daily_refresh(MagicMock(), GUILD)
    assert result["pings"] == "error:RuntimeError"
    assert upsert.await_count == 2
