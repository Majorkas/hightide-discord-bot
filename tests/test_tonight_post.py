from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import discord
import pytest

from src.db.models import Event, GuildConfig
from src.discord_ui import commands_admin
from src.discord_ui.commands_admin import AdminCog
from src.discord_ui.embeds import build_tonight_embed
from src.services import scheduler
from src.services.scheduler import RefreshScheduler, maybe_post_tonight

GUILD = 999
CHANNEL_ID = 555
PERTH = ZoneInfo("Australia/Perth")
NOW = datetime(2026, 10, 23, 9, 0, tzinfo=PERTH)  # Friday


def _ev(n, title, hour, minute=0, *, day=23, known=True, price=None, url=None, active=True):
    local = datetime(2026, 10, day, hour, minute, tzinfo=PERTH)
    return Event(guild_id=GUILD, external_id=f"e{n}", title=title,
                 url=url or f"https://example.com/{n}",
                 starts_at=local.astimezone(timezone.utc), time_known=known,
                 price_text=price, is_active=active)


# --- build_tonight_embed ---

def test_embed_empty_returns_none():
    assert build_tonight_embed([], PERTH) is None


def test_embed_title_and_sorted_by_time_with_tbc_last():
    events = [
        _ev(1, "Late", 19, 30),
        _ev(2, "Unknown", 0, known=False),
        _ev(3, "Early", 10, price="$5"),
    ]
    embed = build_tonight_embed(events, PERTH)
    assert embed.title == "Today at Hightide — Fri 23 Oct"
    lines = embed.description.split("\n")
    assert len(lines) == 3
    assert lines[0].startswith("`10:00` [Early](https://example.com/3)") and lines[0].endswith("· $5")
    assert lines[1].startswith("`19:30` [Late]")
    assert lines[2].startswith("`time TBC` [Unknown]")


def test_embed_escapes_title_and_drops_unsafe_url():
    embed = build_tonight_embed(
        [_ev(1, "[x](http://evil) @everyone *b*", 10, url="javascript:alert(1)")], PERTH)
    assert "javascript" not in embed.description
    assert "@everyone" not in embed.description.replace("@\u200beveryone", "")
    assert "](http://evil)" not in embed.description
    assert "\\*b\\*" in embed.description  # bold markers escaped, not rendered
    assert "@\u200beveryone" in embed.description


def test_embed_title_dropped_by_escaping_is_detected():
    embed = build_tonight_embed([_ev(1, "\u202e\u202d", 10)], PERTH)
    # The line survives with an empty link text rather than vanishing.
    assert embed.description == "`10:00` [](https://example.com/1)"


def test_embed_same_time_events_tiebreak_by_title():
    embed = build_tonight_embed([_ev(1, "Zeta", 10), _ev(2, "Alpha", 10)], PERTH)
    lines = embed.description.split("\n")
    assert "Alpha" in lines[0] and "Zeta" in lines[1]


def test_embed_all_starts_at_none_returns_none():
    events = [Event(guild_id=GUILD, external_id=f"n{i}", title="TBA", url="u", starts_at=None)
              for i in range(2)]
    assert build_tonight_embed(events, PERTH) is None


def test_embed_truncates_long_lists_within_limit():
    events = [_ev(i, "T" * 150, 10, minute=i % 60) for i in range(60)]
    embed = build_tonight_embed(events, PERTH)
    assert len(embed.description) <= 4096
    assert "more" in embed.description


# --- maybe_post_tonight ---

@pytest.fixture
def env(session):
    cfg = GuildConfig(guild_id=GUILD, timezone="Australia/Perth", announce_channel_id=CHANNEL_ID)
    session.add(cfg)
    session.commit()
    channel = MagicMock()
    channel.send = AsyncMock()
    bot = MagicMock()
    bot.get_channel.return_value = channel
    return SimpleNamespace(bot=bot, cfg=cfg, channel=channel, session=session)


async def _post(env, events, now=NOW):
    return await maybe_post_tonight(env.bot, env.session, env.cfg, PERTH, events, now)


async def test_posts_once_then_same_day_noop(env):
    events = [_ev(1, "Chess", 18)]
    assert await _post(env, events) is True
    env.channel.send.assert_awaited_once()
    kwargs = env.channel.send.await_args.kwargs
    assert kwargs["embed"].title.startswith("Today at Hightide")
    assert kwargs["allowed_mentions"].roles is False and kwargs["allowed_mentions"].everyone is False
    assert env.cfg.last_daily_post_date == "2026-10-23"

    assert await _post(env, events, NOW + timedelta(hours=5)) is False
    env.channel.send.assert_awaited_once()


async def test_date_persisted_across_sessions(env):
    await _post(env, [_ev(1, "Chess", 18)])
    env.session.expire_all()
    assert env.session.get(GuildConfig, GUILD).last_daily_post_date == "2026-10-23"


async def test_next_day_posts_again(env):
    assert await _post(env, [_ev(1, "Chess", 18)]) is True
    tomorrow = NOW + timedelta(days=1)
    assert await _post(env, [_ev(2, "Draft", 18, day=24)], tomorrow) is True
    assert env.channel.send.await_count == 2
    assert env.cfg.last_daily_post_date == "2026-10-24"


async def test_date_boundary_uses_guild_timezone(env):
    # 2026-10-23 23:30 UTC is already Oct 24 in Perth; the Oct 23 event is not "today".
    now = datetime(2026, 10, 23, 23, 30, tzinfo=timezone.utc)
    assert await _post(env, [_ev(1, "Chess", 18)], now) is False
    assert await _post(env, [_ev(2, "Draft", 18, day=24)], now) is True
    assert env.cfg.last_daily_post_date == "2026-10-24"


async def test_unset_channel_noop(env):
    env.cfg.announce_channel_id = None
    assert await _post(env, [_ev(1, "Chess", 18)]) is False
    env.channel.send.assert_not_awaited()
    assert env.cfg.last_daily_post_date is None


async def test_zero_events_today_noop(env):
    events = [_ev(1, "Tomorrow", 18, day=24), _ev(2, "Gone", 18, active=False)]
    events.append(Event(guild_id=GUILD, external_id="tba", title="TBA", url="u", starts_at=None))
    assert await _post(env, events) is False
    env.channel.send.assert_not_awaited()
    assert env.cfg.last_daily_post_date is None


async def test_includes_all_events_today_including_time_tbc_and_past(env):
    events = [_ev(1, "Morning", 8), _ev(2, "TBC", 0, known=False), _ev(3, "Night", 20)]
    assert await _post(env, events) is True
    desc = env.channel.send.await_args.kwargs["embed"].description
    assert "Morning" in desc and "TBC" in desc and "Night" in desc


@pytest.mark.parametrize("exc", [
    discord.Forbidden(MagicMock(status=403, reason="x"), "no"),
    discord.NotFound(MagicMock(status=404, reason="x"), "gone"),
    discord.HTTPException(MagicMock(status=500, reason="x"), "boom"),
])
async def test_send_failure_logs_and_does_not_set_date(env, exc, caplog):
    env.channel.send.side_effect = exc
    assert await _post(env, [_ev(1, "Chess", 18)]) is False
    assert env.cfg.last_daily_post_date is None
    assert any("tonight" in r.getMessage().lower() for r in caplog.records)


async def test_channel_unavailable_does_not_set_date(env):
    env.bot.get_channel.return_value = None
    env.bot.fetch_channel = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404, reason="x"), "gone"))
    assert await _post(env, [_ev(1, "Chess", 18)]) is False
    assert env.cfg.last_daily_post_date is None


async def test_day_edge_boundaries(env):
    events = [_ev(1, "Midnight today", 0, 0), _ev(2, "Last minute", 23, 59),
              _ev(3, "Next midnight", 0, 0, day=24)]
    now = datetime(2026, 10, 23, 23, 59, tzinfo=PERTH)
    assert await _post(env, events, now) is True
    desc = env.channel.send.await_args.kwargs["embed"].description
    assert "Last minute" in desc and "Midnight today" in desc
    assert "Next midnight" not in desc
    assert env.cfg.last_daily_post_date == "2026-10-23"


async def test_next_local_midnight_event_alone_is_not_today(env):
    assert await _post(env, [_ev(1, "Next midnight", 0, 0, day=24)], NOW) is False
    env.channel.send.assert_not_awaited()


# --- scheduler tick integration ---

class _Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self, tz):
        return self.now.astimezone(tz)


@pytest.fixture
def clock(monkeypatch):
    c = _Clock(NOW)
    monkeypatch.setattr(scheduler, "_now", c)
    return c


@pytest.fixture
def tick_env(temp_db, clock, monkeypatch):
    monkeypatch.setattr("src.services.scheduler.config.GUILD_ID", GUILD)
    with temp_db() as s:
        s.add(GuildConfig(guild_id=GUILD, timezone="Australia/Perth", announce_channel_id=CHANNEL_ID))
        s.add(_ev(1, "Chess", 18))
        s.commit()
    channel = MagicMock()
    channel.send = AsyncMock()
    bot = MagicMock()
    bot.wait_until_ready = AsyncMock()
    bot.get_channel.return_value = channel
    return SimpleNamespace(bot=bot, channel=channel, factory=temp_db)


_OK = {"events": {"added": 1}, "this_week": "created", "this_month": "created"}


async def test_tick_posts_after_successful_refresh_once_per_day(tick_env, monkeypatch):
    refresh = AsyncMock(return_value=dict(_OK))
    monkeypatch.setattr("src.services.scheduler.run_daily_refresh", refresh)
    inst = RefreshScheduler(tick_env.bot)

    await inst.tick.coro(inst)
    tick_env.channel.send.assert_awaited_once()
    await inst.tick.coro(inst)
    tick_env.channel.send.assert_awaited_once()
    with tick_env.factory() as s:
        assert s.get(GuildConfig, GUILD).last_daily_post_date == "2026-10-23"


async def test_tick_no_post_when_refresh_failed(tick_env, monkeypatch):
    failed = {"events": "error:ConnectionError", "this_week": "created", "this_month": "created"}
    monkeypatch.setattr("src.services.scheduler.run_daily_refresh", AsyncMock(return_value=failed))
    inst = RefreshScheduler(tick_env.bot)

    await inst.tick.coro(inst)
    tick_env.channel.send.assert_not_awaited()


async def test_tonight_failure_does_not_trigger_backoff(tick_env, monkeypatch, clock):
    refresh = AsyncMock(return_value=dict(_OK))
    monkeypatch.setattr("src.services.scheduler.run_daily_refresh", refresh)
    tick_env.channel.send.side_effect = discord.Forbidden(MagicMock(status=403, reason="x"), "no")
    inst = RefreshScheduler(tick_env.bot)

    await inst.tick.coro(inst)
    clock.now = NOW + timedelta(hours=1)
    await inst.tick.coro(inst)

    assert refresh.await_count == 1
    assert inst._next_attempt[GUILD] is None
    assert inst._last_run_key[GUILD] == "2026-10-23"


def _failed_refresh():
    return {"events": "error:ConnectionError", "this_week": "error:X", "this_month": "error:X"}


async def test_retry_after_failed_refresh_posts_exactly_once(tick_env, monkeypatch, clock):
    refresh = AsyncMock(side_effect=[_failed_refresh(), dict(_OK)])
    monkeypatch.setattr("src.services.scheduler.run_daily_refresh", refresh)
    inst = RefreshScheduler(tick_env.bot)

    await inst.tick.coro(inst)
    tick_env.channel.send.assert_not_awaited()
    with tick_env.factory() as s:
        assert s.get(GuildConfig, GUILD).last_daily_post_date is None

    clock.now = NOW + timedelta(minutes=31)
    await inst.tick.coro(inst)
    await inst.tick.coro(inst)
    assert refresh.await_count == 2
    tick_env.channel.send.assert_awaited_once()
    with tick_env.factory() as s:
        assert s.get(GuildConfig, GUILD).last_daily_post_date == "2026-10-23"


async def test_board_error_does_not_suppress_tonight_and_retry_does_not_repost(
        tick_env, monkeypatch, clock):
    board_err = {"events": {"added": 1}, "this_week": "error:HTTPException", "this_month": "created"}
    refresh = AsyncMock(side_effect=[board_err, {"events": "cached", "this_week": "ok",
                                                 "this_month": "ok"}])
    monkeypatch.setattr("src.services.scheduler.run_daily_refresh", refresh)
    inst = RefreshScheduler(tick_env.bot)

    await inst.tick.coro(inst)
    tick_env.channel.send.assert_awaited_once()
    assert board_err["tonight"] == "posted"
    assert inst._next_attempt[GUILD] is not None  # the board error still drives backoff

    clock.now = NOW + timedelta(minutes=31)
    await inst.tick.coro(inst)
    assert refresh.await_count == 2
    tick_env.channel.send.assert_awaited_once()
    assert refresh.await_args.kwargs["fetch"] is False


async def test_db_guard_blocks_post_on_fresh_scheduler(tick_env, monkeypatch):
    with tick_env.factory() as s:
        s.get(GuildConfig, GUILD).last_daily_post_date = "2026-10-23"
        s.commit()
    result = dict(_OK)
    refresh = AsyncMock(return_value=result)
    monkeypatch.setattr("src.services.scheduler.run_daily_refresh", refresh)
    inst = RefreshScheduler(tick_env.bot)  # empty in-memory state: only the DB can block

    await inst.tick.coro(inst)
    refresh.assert_awaited_once()
    tick_env.channel.send.assert_not_awaited()
    assert result["tonight"] == "skipped:already_posted"


async def test_run_level_guard_second_same_day_run_does_not_post(tick_env, monkeypatch):
    refresh = AsyncMock(side_effect=lambda *a, **k: dict(_OK))
    monkeypatch.setattr("src.services.scheduler.run_daily_refresh", refresh)
    inst = RefreshScheduler(tick_env.bot)

    await inst.tick.coro(inst)
    await inst.tick.coro(inst)
    assert refresh.await_count == 1
    tick_env.channel.send.assert_awaited_once()


async def test_unexpected_tonight_exception_rolls_back_without_backoff(tick_env, monkeypatch):
    from sqlalchemy.orm import Session
    result = dict(_OK)
    monkeypatch.setattr("src.services.scheduler.run_daily_refresh", AsyncMock(return_value=result))
    real_commit, real_rollback = Session.commit, Session.rollback
    rollbacks = []

    def commit(self):
        if any(isinstance(o, GuildConfig) for o in self.dirty):
            raise RuntimeError("commit failed")
        return real_commit(self)

    def rollback(self):
        rollbacks.append(1)
        return real_rollback(self)

    monkeypatch.setattr(Session, "commit", commit)
    monkeypatch.setattr(Session, "rollback", rollback)
    inst = RefreshScheduler(tick_env.bot)

    await inst.tick.coro(inst)
    assert result["tonight"] == "error:RuntimeError"
    assert rollbacks
    assert inst._last_run_key[GUILD] == "2026-10-23"
    assert inst._next_attempt[GUILD] is None
    with tick_env.factory() as s:
        assert s.get(GuildConfig, GUILD).last_daily_post_date is None


async def test_failed_send_leaves_date_unset_and_next_day_posts(tick_env, monkeypatch, clock):
    monkeypatch.setattr("src.services.scheduler.run_daily_refresh",
                        AsyncMock(side_effect=lambda *a, **k: dict(_OK)))
    with tick_env.factory() as s:
        s.add(_ev(2, "Tomorrow", 18, day=24))
        s.commit()
    tick_env.channel.send.side_effect = discord.Forbidden(MagicMock(status=403, reason="x"), "no")
    inst = RefreshScheduler(tick_env.bot)

    await inst.tick.coro(inst)
    with tick_env.factory() as s:
        assert s.get(GuildConfig, GUILD).last_daily_post_date is None

    tick_env.channel.send.side_effect = None
    clock.now = NOW + timedelta(days=1)
    await inst.tick.coro(inst)
    with tick_env.factory() as s:
        assert s.get(GuildConfig, GUILD).last_daily_post_date == "2026-10-24"
    embed = tick_env.channel.send.await_args.kwargs["embed"]
    assert "Tomorrow" in embed.description and "Chess" not in embed.description


async def test_tonight_reads_fresh_config_after_refresh(tick_env, monkeypatch):
    new_channel = MagicMock()
    new_channel.send = AsyncMock()
    channels = {CHANNEL_ID: tick_env.channel, 777: new_channel}
    tick_env.bot.get_channel.side_effect = channels.get

    async def refresh(*a, **k):
        # Another session (e.g. /config announce_channel) commits mid-tick.
        with tick_env.factory() as other:
            other.get(GuildConfig, GUILD).announce_channel_id = 777
            other.commit()
        return dict(_OK)

    monkeypatch.setattr("src.services.scheduler.run_daily_refresh", refresh)
    inst = RefreshScheduler(tick_env.bot)

    await inst.tick.coro(inst)
    new_channel.send.assert_awaited_once()
    tick_env.channel.send.assert_not_awaited()


@pytest.mark.parametrize("branch, expected", [
    ("no_channel", "skipped:no_channel"),
    ("already_posted", "skipped:already_posted"),
    ("no_events", "skipped:no_events"),
    ("posted", "posted"),
    ("forbidden", "error:Forbidden"),
    ("channel_unavailable", "error:ChannelUnavailable"),
])
async def test_tick_tonight_result_per_branch(tick_env, monkeypatch, branch, expected):
    with tick_env.factory() as s:
        cfg = s.get(GuildConfig, GUILD)
        if branch == "no_channel":
            cfg.announce_channel_id = None
        elif branch == "already_posted":
            cfg.last_daily_post_date = "2026-10-23"
        elif branch == "no_events":
            s.query(Event).delete()
        s.commit()
    if branch == "forbidden":
        tick_env.channel.send.side_effect = discord.Forbidden(MagicMock(status=403, reason="x"), "no")
    elif branch == "channel_unavailable":
        tick_env.bot.get_channel.return_value = None
        tick_env.bot.fetch_channel = AsyncMock(
            side_effect=discord.NotFound(MagicMock(status=404, reason="x"), "gone"))
    result = dict(_OK)
    monkeypatch.setattr("src.services.scheduler.run_daily_refresh", AsyncMock(return_value=result))
    inst = RefreshScheduler(tick_env.bot)

    await inst.tick.coro(inst)
    assert result["tonight"] == expected


# --- config commands ---

@pytest.fixture
def db(tmp_path, monkeypatch):
    from src.db.models import Base
    from src.db.session import get_engine, get_session_factory
    url = f"sqlite:///{tmp_path / 'admin.db'}"
    engine = get_engine(url)
    Base.metadata.create_all(engine)
    factory = get_session_factory(url)
    monkeypatch.setattr(commands_admin, "_session_factory", factory)
    yield factory
    engine.dispose()
    factory.kw["bind"].dispose()


@pytest.fixture
def interaction():
    i = MagicMock()
    i.guild_id = GUILD
    i.response.send_message = AsyncMock()
    return i


async def test_set_announce_channel(db, interaction):
    cog = AdminCog(MagicMock())
    await cog.set_announce_channel.callback(
        cog, interaction, SimpleNamespace(id=444, mention="<#444>"))
    with db() as s:
        assert s.get(GuildConfig, GUILD).announce_channel_id == 444
    assert "<#444>" in interaction.response.send_message.call_args.args[0]


async def test_set_daily_post_time(db, interaction):
    cog = AdminCog(MagicMock())
    await cog.set_daily_post_time.callback(cog, interaction, "18:15")
    with db() as s:
        assert s.get(GuildConfig, GUILD).daily_post_time == "18:15"
    assert "18:15" in interaction.response.send_message.call_args.args[0]


@pytest.mark.parametrize("value", ["9:00", "24:00", "12:60", "ab:cd", ""])
async def test_set_daily_post_time_invalid(db, interaction, value):
    cog = AdminCog(MagicMock())
    await cog.set_daily_post_time.callback(cog, interaction, value)
    with db() as s:
        assert s.get(GuildConfig, GUILD) is None
    assert "HH:MM" in interaction.response.send_message.call_args.args[0]


@pytest.mark.parametrize("value", ["00:00", "23:59"])
async def test_set_daily_post_time_accepts_range_bounds(db, interaction, value):
    cog = AdminCog(MagicMock())
    await cog.set_daily_post_time.callback(cog, interaction, value)
    with db() as s:
        assert s.get(GuildConfig, GUILD).daily_post_time == value


async def test_set_daily_post_time_overwrites_existing(db, interaction):
    cog = AdminCog(MagicMock())
    await cog.set_daily_post_time.callback(cog, interaction, "18:15")
    await cog.set_daily_post_time.callback(cog, interaction, "07:05")
    with db() as s:
        assert s.get(GuildConfig, GUILD).daily_post_time == "07:05"


def test_fresh_config_row_defaults_post_time_to_0900(session):
    session.add(GuildConfig(guild_id=GUILD))
    session.commit()
    session.expire_all()
    assert session.get(GuildConfig, GUILD).daily_post_time == "09:00"


def test_daily_post_time_description_documents_v2_behaviour():
    assert "refresh" in AdminCog.set_daily_post_time.description.lower()
    assert len(AdminCog.set_daily_post_time.description) <= 100
