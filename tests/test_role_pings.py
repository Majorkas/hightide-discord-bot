import copy
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import discord
import pytest

from src.db.models import Event, EventRolePing, GuildConfig
from src.db.repositories import add_interest_role, add_keyword
from src.discord_ui.embeds import build_ping_message
from src.services import role_service, scheduler
from src.services.event_service import upsert_products
from src.services.role_service import match_roles_for_title, ping_new_events

GUILD = 999
CHANNEL_ID = 555
ROLE_ID = 11
PERTH = ZoneInfo("Australia/Perth")
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=PERTH)  # Thursday


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


def test_whitespace_folded_matching():
    roles_by_id, kw = _roles()
    nbsp = "Magic\u00a0the  Gathering draft"
    assert [r.id for r in match_roles_for_title(nbsp, kw, roles_by_id)] == [2]
    assert [r.id for r in match_roles_for_title("Magic the Gathering", {2: ["magic  the\tgathering"]},
                                                roles_by_id)] == [2]


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


@pytest.fixture
def fixed_now(monkeypatch):
    monkeypatch.setattr(role_service, "_now", lambda tz: NOW.astimezone(tz))
    return NOW


def _add_role(session, role_id, label, *keywords):
    role = add_interest_role(session, GUILD, role_id, label)
    for kw in keywords:
        add_keyword(session, role.id, kw)
    return role


def _bot(*, role_ids=(ROLE_ID,), send=None):
    channel = MagicMock()
    channel.send = send or AsyncMock()
    guild = MagicMock()
    guild.get_role = MagicMock(
        side_effect=lambda rid: SimpleNamespace(id=rid) if rid in role_ids else None)
    bot = MagicMock()
    bot.get_guild = MagicMock(return_value=guild)
    bot.get_channel = MagicMock(return_value=channel)
    bot.fetch_channel = AsyncMock(return_value=channel)
    return bot, channel


def _event(session, ext, title="Weekly Duels – Yu-Gi-Oh!", starts_at="future", *,
           first_seen_at=None, time_known=None):
    now = datetime.now(timezone.utc)
    presets = {"future": now + timedelta(days=30), "past": now - timedelta(days=30), "tba": None}
    when = presets[starts_at] if isinstance(starts_at, str) else starts_at
    extra = {"first_seen_at": first_seen_at} if first_seen_at else {}
    ev = Event(guild_id=GUILD, external_id=ext, title=title, url="https://example.com/" + ext,
               starts_at=when, time_known=when is not None if time_known is None else time_known,
               fingerprint="f", **extra)
    session.add(ev)
    session.commit()
    return ev


def _dated_product(fixture_products, pid, title, days=10):
    product = copy.deepcopy(next(p for p in fixture_products if "weekly-duels" in p["slug"]))
    when = datetime.now(PERTH) + timedelta(days=days)
    product["id"] = pid
    product["link"] = f"https://example.com/p{pid}"
    product["title"]["rendered"] = f"{when:%d.%m.%y} &#8211; {title}"
    return product


def _http_error(cls=discord.HTTPException, status=500):
    return cls(MagicMock(status=status, reason="x"), "x")


def _ping_rows(session):
    return session.query(EventRolePing).all()


async def test_new_event_pings_once_with_scoped_mentions(session, tz, role_env):
    bot, channel = _bot()
    _event(session, "wp:1")
    sent = await ping_new_events(bot, session, GUILD, tz)
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
    _event(session, "wp:1")
    assert await ping_new_events(bot, session, GUILD, tz) == 1
    assert await ping_new_events(bot, session, GUILD, tz) == 0
    assert channel.send.await_count == 1


async def test_unset_announce_channel_sends_nothing_and_warns(session, tz, role_env, caplog):
    cfg = session.get(GuildConfig, GUILD)
    cfg.announce_channel_id = None
    session.commit()
    bot, channel = _bot()
    _event(session, "wp:1")
    with caplog.at_level(logging.WARNING, logger="hightide.roles"):
        assert await ping_new_events(bot, session, GUILD, tz) == 0
    channel.send.assert_not_awaited()
    assert session.query(EventRolePing).count() == 0
    assert any(r.name == "hightide.roles" and r.levelno == logging.WARNING
               for r in caplog.records)


async def test_deleted_role_skipped_and_logged(session, tz, role_env, caplog):
    bot, channel = _bot(role_ids=())
    _event(session, "wp:1")
    with caplog.at_level(logging.WARNING):
        assert await ping_new_events(bot, session, GUILD, tz) == 0
    channel.send.assert_not_awaited()
    assert session.query(EventRolePing).count() == 0
    assert caplog.records


async def test_disabled_role_not_pinged(session, tz, role_env):
    role_env.enabled = False
    session.commit()
    bot, channel = _bot()
    _event(session, "wp:1")
    assert await ping_new_events(bot, session, GUILD, tz) == 0
    channel.send.assert_not_awaited()


async def test_non_matching_event_not_pinged(session, tz, role_env):
    bot, channel = _bot()
    _event(session, "wp:1", title="Painting night")
    assert await ping_new_events(bot, session, GUILD, tz) == 0
    channel.send.assert_not_awaited()


async def test_past_event_not_pinged(session, tz, role_env):
    bot, channel = _bot()
    _event(session, "wp:1", starts_at="past")
    assert await ping_new_events(bot, session, GUILD, tz) == 0
    channel.send.assert_not_awaited()
    assert session.query(EventRolePing).count() == 0


async def test_tba_event_is_pinged(session, tz, role_env):
    bot, channel = _bot()
    _event(session, "wp:1", starts_at="tba")
    assert await ping_new_events(bot, session, GUILD, tz) == 1
    channel.send.assert_awaited_once()


async def test_inactive_event_not_pinged(session, tz, role_env):
    bot, channel = _bot()
    ev = _event(session, "wp:1")
    ev.is_active = False
    session.commit()
    assert await ping_new_events(bot, session, GUILD, tz) == 0
    channel.send.assert_not_awaited()


async def test_final_message_truncated_to_discord_limit(session, tz, role_env, monkeypatch):
    bot, channel = _bot()
    _event(session, "wp:1")
    monkeypatch.setattr(role_service, "build_ping_message", lambda *a: "x" * 3000)
    assert await ping_new_events(bot, session, GUILD, tz) == 1
    assert len(channel.send.await_args.args[0]) == 2000


async def test_suppress_sends_and_records_nothing(session, tz, role_env):
    bot, channel = _bot()
    _event(session, "wp:1")
    assert await ping_new_events(bot, session, GUILD, tz, suppress=True) == 0
    channel.send.assert_not_awaited()
    assert session.query(EventRolePing).count() == 0


# --- multi-role scoping ---

async def test_multi_role_event_sends_one_scoped_message_per_role(session, tz, role_env):
    _add_role(session, 22, "MTG", "magic")
    bot, channel = _bot(role_ids=(ROLE_ID, 22))
    _event(session, "wp:1", title="Yugioh vs Magic night")
    assert await ping_new_events(bot, session, GUILD, tz) == 2
    assert channel.send.await_count == 2
    seen = {}
    for call in channel.send.await_args_list:
        roles = call.kwargs["allowed_mentions"].roles
        assert len(roles) == 1
        assert call.args[0].startswith(f"<@&{roles[0].id}>")
        seen[roles[0].id] = call.args[0]
    assert set(seen) == {ROLE_ID, 22}


# --- _already_started boundaries (injected clock) ---

@pytest.mark.parametrize("offset,time_known,expected", [
    (timedelta(hours=-12), False, True),    # TBC event, today (local midnight)
    (timedelta(hours=-36), False, False),   # TBC event, yesterday
    (timedelta(minutes=1), True, True),
    (timedelta(minutes=-1), True, False),
])
async def test_already_started_boundaries(session, tz, role_env, fixed_now,
                                          offset, time_known, expected):
    bot, channel = _bot()
    _event(session, "wp:1", starts_at=fixed_now + offset, time_known=time_known,
           first_seen_at=fixed_now)
    assert await ping_new_events(bot, session, GUILD, tz) == int(expected)
    assert channel.send.await_count == int(expected)


@pytest.mark.parametrize("starts_at,time_known,suffix", [
    (datetime(2026, 10, 22, 20, 0, tzinfo=timezone.utc), True, "Fri 23 Oct · 04:00"),
    (datetime(2026, 10, 24, 16, 0, tzinfo=timezone.utc), False, "Sun 25 Oct · time TBC"),
])
async def test_ping_message_uses_local_date(session, tz, role_env, fixed_now,
                                            starts_at, time_known, suffix):
    bot, channel = _bot()
    _event(session, "wp:1", starts_at=starts_at, time_known=time_known, first_seen_at=fixed_now)
    assert await ping_new_events(bot, session, GUILD, tz) == 1
    assert channel.send.await_args.args[0].endswith(suffix)


# --- real-ORM wiring ---

async def test_never_pings_on_title_edits(session, tz, role_env, fixture_products):
    first = upsert_products(session, GUILD, [_dated_product(fixture_products, 9001, "Painting night")], tz)
    assert len(first["created_events"]) == 1
    edited = _dated_product(fixture_products, 9001, "Yugioh night")
    second = upsert_products(session, GUILD, [edited], tz)
    assert second["created_events"] == []
    assert second["updated"] == 1
    assert session.query(EventRolePing).count() == 0


async def test_rerefresh_pings_once_then_nothing(session, tz, role_env, fixture_products):
    bot, channel = _bot()
    product = _dated_product(fixture_products, 9001, "Yugioh locals")
    upsert_products(session, GUILD, [product], tz)
    assert await ping_new_events(bot, session, GUILD, tz) == 1
    assert channel.send.await_count == 1
    upsert_products(session, GUILD, [product], tz)
    assert await ping_new_events(bot, session, GUILD, tz) == 0
    assert channel.send.await_count == 1


async def test_ping_rows_reference_real_event_ids(session, tz, role_env, fixture_products):
    bot, channel = _bot()
    products = [_dated_product(fixture_products, 9001, "Yugioh locals night", days=10),
                _dated_product(fixture_products, 9002, "Yugioh draft night", days=11)]
    upsert_products(session, GUILD, products, tz)
    assert await ping_new_events(bot, session, GUILD, tz) == 2
    events = {e.id: e for e in session.query(Event).all()}
    rows = _ping_rows(session)
    assert {r.event_id for r in rows} == set(events)
    assert {r.interest_role_id for r in rows} == {role_env.id}
    contents = [c.args[0] for c in channel.send.await_args_list]
    for event, content in zip(sorted(events.values(), key=lambda e: e.starts_at), contents):
        assert event.title in content
        assert event.url in content


# --- send-failure precision ---

async def test_send_failure_not_recorded(session, tz, role_env):
    send = AsyncMock(side_effect=_http_error(discord.Forbidden, 403))
    bot, channel = _bot(send=send)
    _event(session, "wp:1")
    assert await ping_new_events(bot, session, GUILD, tz) == 0
    assert session.query(EventRolePing).count() == 0


async def test_failed_ping_retried_by_pending_scan_next_run(session, tz, role_env):
    send = AsyncMock(side_effect=_http_error(discord.Forbidden, 403))
    bot, channel = _bot(send=send)
    _event(session, "wp:1", first_seen_at=datetime.now(timezone.utc) - timedelta(hours=24))
    assert await ping_new_events(bot, session, GUILD, tz) == 0
    channel.send = AsyncMock()
    assert await ping_new_events(bot, session, GUILD, tz) == 1
    assert session.query(EventRolePing).count() == 1


async def test_send_failure_only_skips_the_failing_event(session, tz, role_env):
    now = datetime.now(timezone.utc)
    send = AsyncMock(side_effect=[_http_error(discord.NotFound, 404), None])
    bot, channel = _bot(send=send)
    e1 = _event(session, "wp:1", starts_at=now + timedelta(days=1))
    e2 = _event(session, "wp:2", starts_at=now + timedelta(days=2))
    assert await ping_new_events(bot, session, GUILD, tz) == 1
    assert send.await_count == 2
    assert [r.event_id for r in _ping_rows(session)] == [e2.id]
    assert e1.id not in {r.event_id for r in _ping_rows(session)}


async def test_multi_role_partial_failure_records_only_success(session, tz, role_env):
    role_b = _add_role(session, 22, "MTG", "magic")
    send = AsyncMock(side_effect=[None, _http_error()])
    bot, channel = _bot(role_ids=(ROLE_ID, 22), send=send)
    ev = _event(session, "wp:1", title="Yugioh vs Magic night")
    assert await ping_new_events(bot, session, GUILD, tz) == 1
    assert [(r.event_id, r.interest_role_id) for r in _ping_rows(session)] == [(ev.id, role_env.id)]
    assert role_b.id not in {r.interest_role_id for r in _ping_rows(session)}


# --- channel / guild branches ---

async def test_get_channel_none_falls_back_to_fetch_channel(session, tz, role_env):
    bot, channel = _bot()
    bot.get_channel = MagicMock(return_value=None)
    _event(session, "wp:1")
    assert await ping_new_events(bot, session, GUILD, tz) == 1
    bot.fetch_channel.assert_awaited_once_with(CHANNEL_ID)
    channel.send.assert_awaited_once()


async def test_missing_guild_returns_zero(session, tz, role_env):
    bot, channel = _bot()
    bot.get_guild = MagicMock(return_value=None)
    _event(session, "wp:1")
    assert await ping_new_events(bot, session, GUILD, tz) == 0
    channel.send.assert_not_awaited()


@pytest.mark.parametrize("cls,status", [(discord.NotFound, 404), (discord.Forbidden, 403)])
async def test_fetch_channel_failure_returns_zero(session, tz, role_env, cls, status):
    bot, channel = _bot()
    bot.get_channel = MagicMock(return_value=None)
    bot.fetch_channel = AsyncMock(side_effect=_http_error(cls, status))
    _event(session, "wp:1")
    assert await ping_new_events(bot, session, GUILD, tz) == 0
    channel.send.assert_not_awaited()
    assert session.query(EventRolePing).count() == 0


# --- pending scan ---

async def test_pending_event_from_yesterday_picked_up(session, tz, role_env):
    bot, channel = _bot()
    _event(session, "wp:1", first_seen_at=datetime.now(timezone.utc) - timedelta(hours=24))
    assert await ping_new_events(bot, session, GUILD, tz) == 1


async def test_event_first_seen_over_48h_ago_not_pinged(session, tz, role_env):
    bot, channel = _bot()
    _event(session, "wp:1", first_seen_at=datetime.now(timezone.utc) - timedelta(hours=49))
    assert await ping_new_events(bot, session, GUILD, tz) == 0
    channel.send.assert_not_awaited()


async def test_more_than_five_candidates_capped_with_digest(session, tz, role_env):
    now = datetime.now(timezone.utc)
    events = [_event(session, f"wp:{i}", title=f"Yugioh night {i}",
                     starts_at=now + timedelta(days=i + 1)) for i in range(7)]
    bot, channel = _bot()
    assert await ping_new_events(bot, session, GUILD, tz) == 5
    assert channel.send.await_count == 6
    digest = channel.send.await_args_list[-1]
    assert digest.args[0] == "➕ 2 more new events this week — check the boards."
    mentions = digest.kwargs["allowed_mentions"]
    assert (mentions.everyone, mentions.users, mentions.roles) == (False, False, False)
    assert {r.event_id for r in _ping_rows(session)} == {e.id for e in events[:5]}
    # Overflow is still pending and gets pinged on the next run.
    channel.send.reset_mock()
    assert await ping_new_events(bot, session, GUILD, tz) == 2
    assert channel.send.await_count == 2


async def test_only_two_longest_keyword_roles_pinged_per_event(session, tz, role_env):
    _add_role(session, 22, "MTG", "magic the gathering")
    _add_role(session, 33, "YGO", "ygo")
    bot, channel = _bot(role_ids=(ROLE_ID, 22, 33))
    _event(session, "wp:1", title="YGO Yu-Gi-Oh and Magic the Gathering night")
    assert await ping_new_events(bot, session, GUILD, tz) == 2
    pinged = [call.kwargs["allowed_mentions"].roles[0].id for call in channel.send.await_args_list]
    assert pinged == [22, ROLE_ID]


# --- scheduler hook ---

def _seed_guild(temp_db):
    with temp_db() as s:
        s.add(GuildConfig(guild_id=GUILD, timezone="Australia/Perth",
                          this_week_channel_id=111, this_month_channel_id=222,
                          announce_channel_id=CHANNEL_ID))
        s.commit()
        role = add_interest_role(s, GUILD, ROLE_ID, "Yu-Gi-Oh")
        add_keyword(s, role.id, "yugioh")


def _patch_boards(monkeypatch):
    monkeypatch.setattr("src.services.scheduler.upsert_board", AsyncMock(return_value="skipped"))


def _patch_refresh_with(monkeypatch, products, tz=PERTH):
    def fake(session, guild_id, url, _tz):
        return upsert_products(session, guild_id, products, tz)
    monkeypatch.setattr("src.services.scheduler.refresh_events", fake)


async def test_run_daily_refresh_pings_from_db_regardless_of_created(temp_db, monkeypatch):
    _seed_guild(temp_db)
    _patch_boards(monkeypatch)
    monkeypatch.setattr("src.services.scheduler.refresh_events",
                        MagicMock(return_value={"created": 0}))
    ping = AsyncMock(return_value=2)
    monkeypatch.setattr("src.services.scheduler.ping_new_events", ping)

    result = await scheduler.run_daily_refresh(MagicMock(), GUILD)

    ping.assert_awaited_once()
    assert result["pings"] == 2
    assert ping.await_args.kwargs["suppress"] is True  # no prior events


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
                        MagicMock(return_value={"created": 1}))
    monkeypatch.setattr("src.services.scheduler.ping_new_events",
                        AsyncMock(side_effect=RuntimeError("x")))
    result = await scheduler.run_daily_refresh(MagicMock(), GUILD)
    assert result["pings"] == 0
    assert upsert.await_count == 2


async def test_ping_failure_does_not_trigger_tick_backoff(temp_db, monkeypatch):
    _seed_guild(temp_db)
    _patch_boards(monkeypatch)
    monkeypatch.setattr("src.services.scheduler.config.GUILD_ID", GUILD)
    monkeypatch.setattr(scheduler, "_now",
                        lambda tz: datetime(2026, 10, 23, 9, 0, tzinfo=PERTH).astimezone(tz))
    monkeypatch.setattr("src.services.scheduler.refresh_events",
                        MagicMock(return_value={"created": 1}))
    monkeypatch.setattr("src.services.scheduler.ping_new_events",
                        AsyncMock(side_effect=RuntimeError("x")))
    inst = scheduler.RefreshScheduler(MagicMock())
    with temp_db() as s:
        await inst._tick_guild(s, GUILD)
    assert inst._last_run_key[GUILD] == "2026-10-23"
    assert inst._next_attempt[GUILD] is None


async def test_bootstrap_refresh_is_silent(temp_db, monkeypatch, fixture_products):
    _seed_guild(temp_db)
    _patch_boards(monkeypatch)
    _patch_refresh_with(monkeypatch, [
        _dated_product(fixture_products, 9001, "Yugioh locals", days=10),
        _dated_product(fixture_products, 9002, "Yugioh draft", days=11)])
    bot, channel = _bot()

    result = await scheduler.run_daily_refresh(bot, GUILD)

    assert result["events"]["created"] == 2
    assert result["pings"] == 0
    channel.send.assert_not_awaited()
    with temp_db() as s:
        assert s.query(EventRolePing).count() == 0


async def test_refresh_with_prior_events_pings_new_ones(temp_db, monkeypatch, fixture_products):
    _seed_guild(temp_db)
    _patch_boards(monkeypatch)
    with temp_db() as s:
        s.add(Event(guild_id=GUILD, external_id="wp:old", title="Painting", url="u",
                    starts_at=None, time_known=False, fingerprint="f"))
        s.commit()
    _patch_refresh_with(monkeypatch, [_dated_product(fixture_products, 9001, "Yugioh locals")])
    bot, channel = _bot()

    result = await scheduler.run_daily_refresh(bot, GUILD)

    assert result["pings"] == 1
    channel.send.assert_awaited_once()
    with temp_db() as s:
        assert s.query(EventRolePing).count() == 1
