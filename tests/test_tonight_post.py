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


def test_daily_post_time_description_documents_v2_behaviour():
    assert "refresh" in AdminCog.set_daily_post_time.description.lower()
    assert len(AdminCog.set_daily_post_time.description) <= 100
