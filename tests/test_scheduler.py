import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from src.db.models import CardWant, Event, GuildConfig
from src.db.repositories import save_board_post
from src.services import scheduler
from src.services.scheduler import RefreshScheduler, _in_run_window, should_run_now
from tests.test_wants import _SetChannel

TZ = ZoneInfo("Australia/Perth")


def test_should_run_now_at_configured_time():
    assert should_run_now("09:00", 9, 0) is True
    assert should_run_now("09:00", 9, 1) is False
    assert should_run_now("9:05", 9, 5) is True


@pytest.mark.parametrize("hhmm,now,expected", [
    ("09:00", datetime(2026, 10, 23, 9, 0, tzinfo=TZ), True),
    ("09:00", datetime(2026, 10, 23, 8, 59, tzinfo=TZ), False),
    ("09:00", datetime(2026, 10, 23, 14, 30, tzinfo=TZ), True),
    ("9:05", datetime(2026, 10, 23, 9, 5, tzinfo=TZ), True),
    ("09:00", datetime(2026, 10, 23, 0, 0, tzinfo=TZ), False),
    ("garbage", datetime(2026, 10, 23, 12, 0, tzinfo=TZ), False),
    ("", datetime(2026, 10, 23, 12, 0, tzinfo=TZ), False),
    ("25:00", datetime(2026, 10, 23, 12, 0, tzinfo=TZ), False),
    ("09:61", datetime(2026, 10, 23, 12, 0, tzinfo=TZ), False),
])
def test_in_run_window(hhmm, now, expected):
    assert _in_run_window(hhmm, now) is expected


def _seed(temp_db, **kw):
    base = dict(guild_id=999, timezone="Australia/Perth",
                this_week_channel_id=111, this_month_channel_id=222)
    base.update(kw)
    with temp_db() as s:
        s.add(GuildConfig(**base))
        s.commit()


class _Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self, tz):
        return self.now.astimezone(tz)


@pytest.fixture
def clock(monkeypatch):
    c = _Clock(datetime(2026, 10, 23, 9, 0, tzinfo=TZ))
    monkeypatch.setattr(scheduler, "_now", c)
    return c


def _bot():
    bot = MagicMock()
    bot.wait_until_ready = AsyncMock()
    return bot


# --- run_daily_refresh ---

async def test_run_daily_refresh(temp_db, monkeypatch):
    _seed(temp_db)
    monkeypatch.setattr("src.services.scheduler.refresh_events",
                        MagicMock(return_value={"added": 2, "updated": 1}))
    mock_upsert = AsyncMock(side_effect=["created", "skipped"])
    monkeypatch.setattr("src.services.scheduler.upsert_board", mock_upsert)

    bot = MagicMock()
    result = await scheduler.run_daily_refresh(bot, 999)

    assert result == {"events": {"added": 2, "updated": 1}, "pings": 0,
                      "this_week": "created", "this_month": "skipped"}
    assert mock_upsert.await_count == 2
    boards = [c.args[3] for c in mock_upsert.await_args_list]
    assert boards == ["this_week", "this_month"]
    for c in mock_upsert.await_args_list:
        assert c.args[0] is bot
        assert c.args[2] == 999


async def test_run_daily_refresh_fetch_failure_still_updates_boards(temp_db, monkeypatch):
    _seed(temp_db)
    monkeypatch.setattr("src.services.scheduler.refresh_events",
                        MagicMock(side_effect=ConnectionError("boom")))
    mock_upsert = AsyncMock(side_effect=["edited", "skipped"])
    monkeypatch.setattr("src.services.scheduler.upsert_board", mock_upsert)

    result = await scheduler.run_daily_refresh(MagicMock(), 999)

    assert result["events"] == "error:ConnectionError"
    assert result["this_week"] == "edited"
    assert result["this_month"] == "skipped"
    assert mock_upsert.await_count == 2


async def test_run_daily_refresh_one_board_failure_isolated(temp_db, monkeypatch):
    _seed(temp_db)
    monkeypatch.setattr("src.services.scheduler.refresh_events",
                        MagicMock(return_value={"added": 0}))
    mock_upsert = AsyncMock(side_effect=[RuntimeError("x"), "created"])
    monkeypatch.setattr("src.services.scheduler.upsert_board", mock_upsert)

    result = await scheduler.run_daily_refresh(MagicMock(), 999)

    assert result["this_week"] == "error:RuntimeError"
    assert result["this_month"] == "created"
    assert mock_upsert.await_count == 2


async def test_run_daily_refresh_wants_channel_unset_skips_call(temp_db, monkeypatch):
    _seed(temp_db, wants_channel_id=None)
    monkeypatch.setattr("src.services.scheduler.refresh_events",
                        MagicMock(return_value={"added": 0}))
    monkeypatch.setattr("src.services.scheduler.upsert_board", AsyncMock(side_effect=["created", "created"]))
    mock_wants = AsyncMock()
    monkeypatch.setattr("src.services.scheduler.update_wants_board", mock_wants)

    result = await scheduler.run_daily_refresh(MagicMock(), 999)

    mock_wants.assert_not_awaited()
    assert "wants" not in result


async def test_run_daily_refresh_wants_channel_set_calls_update(temp_db, monkeypatch):
    _seed(temp_db, wants_channel_id=333)
    monkeypatch.setattr("src.services.scheduler.refresh_events",
                        MagicMock(return_value={"added": 0}))
    monkeypatch.setattr("src.services.scheduler.upsert_board", AsyncMock(side_effect=["created", "created"]))
    mock_wants = AsyncMock(return_value="edited")
    monkeypatch.setattr("src.services.scheduler.update_wants_board", mock_wants)

    result = await scheduler.run_daily_refresh(MagicMock(), 999)

    mock_wants.assert_awaited_once()
    assert result["wants"] == "edited"


async def test_run_daily_refresh_wants_failure_isolated(temp_db, monkeypatch):
    _seed(temp_db, wants_channel_id=333)
    monkeypatch.setattr("src.services.scheduler.refresh_events",
                        MagicMock(return_value={"added": 0}))
    monkeypatch.setattr("src.services.scheduler.upsert_board", AsyncMock(side_effect=["created", "created"]))
    mock_wants = AsyncMock(side_effect=ConnectionError("net fail"))
    monkeypatch.setattr("src.services.scheduler.update_wants_board", mock_wants)

    result = await scheduler.run_daily_refresh(MagicMock(), 999)

    assert result["wants"] == "error:ConnectionError"
    assert result["this_week"] == "created"
    assert result["this_month"] == "created"


async def test_run_daily_refresh_really_expires_wants_and_edits_board(temp_db, monkeypatch):
    _seed(temp_db, this_week_channel_id=None, this_month_channel_id=None, wants_channel_id=333)
    now = datetime.now(timezone.utc)
    with temp_db() as s:
        s.add(CardWant(guild_id=999, user_id=1, card_name="Stale", active=True,
                       created_at=now - timedelta(days=31)))
        s.add(CardWant(guild_id=999, user_id=1, card_name="Fresh", active=True,
                       created_at=now - timedelta(days=1)))
        s.commit()
        save_board_post(s, 999, "wants", "wants", 333, 900,
                        '{"keys": ["header"], "hashes": {}}', "[]")
    monkeypatch.setattr("src.services.scheduler.refresh_events", MagicMock(return_value={}))

    channel = _SetChannel(333)
    bot = MagicMock()
    bot.user = MagicMock(id=1)
    bot.get_channel.return_value = channel
    bot.fetch_channel = AsyncMock(return_value=channel)

    result = await scheduler.run_daily_refresh(bot, 999)

    assert result["wants"] in ("edited", "created")
    with temp_db() as s:
        active = {w.card_name: w.active for w in s.query(CardWant).all()}
    assert active == {"Stale": False, "Fresh": True}
    # the board now holds only the Fresh card
    titles = [m.embed.title for m in channel.messages.values()]
    assert any("Fresh" in (t or "") for t in titles)
    assert not any("Stale" in (t or "") for t in titles)


async def test_run_daily_refresh_all_wants_expired_shows_empty_board(temp_db, monkeypatch):
    _seed(temp_db, this_week_channel_id=None, this_month_channel_id=None, wants_channel_id=333)
    with temp_db() as s:
        s.add(CardWant(guild_id=999, user_id=1, card_name="Stale", active=True,
                       created_at=datetime.now(timezone.utc) - timedelta(days=31)))
        s.commit()
        save_board_post(s, 999, "wants", "wants", 333, 900,
                        '{"keys": ["header"], "hashes": {}}', "[]")
    monkeypatch.setattr("src.services.scheduler.refresh_events", MagicMock(return_value={}))
    channel = _SetChannel(333)
    bot = MagicMock()
    bot.user = MagicMock(id=1)
    bot.get_channel.return_value = channel
    bot.fetch_channel = AsyncMock(return_value=channel)

    await scheduler.run_daily_refresh(bot, 999)

    with temp_db() as s:
        assert s.query(CardWant).one().active is False
    # only the header remains, showing the empty state
    descs = [m.embed.description for m in channel.messages.values()]
    assert any("No active wants" in (d or "") for d in descs)


# --- tick ---

_BOARD_FAIL = {"events": {"added": 1}, "this_week": "error:RuntimeError", "this_month": "created"}
_FETCH_FAIL = {"events": "error:ConnectionError", "this_week": "created", "this_month": "created"}
_OK = {"events": {"added": 1}, "this_week": "created", "this_month": "created"}
_OK_CACHED = {"events": "cached", "this_week": "edited", "this_month": "skipped"}


async def test_tick_guild_id_unset_no_config_rows_idles(temp_db, monkeypatch, caplog):
    monkeypatch.setattr("src.services.scheduler.config.GUILD_ID", None)
    mock_refresh = AsyncMock()
    monkeypatch.setattr("src.services.scheduler.run_daily_refresh", mock_refresh)

    bot = _bot()
    inst = RefreshScheduler(bot)
    with caplog.at_level("INFO", logger="hightide.scheduler"):
        await inst.tick.coro(inst)
        await inst.tick.coro(inst)

    mock_refresh.assert_not_awaited()
    bot.wait_until_ready.assert_awaited()
    assert [r.getMessage() for r in caplog.records].count(
        "scheduler idle — no guild configured yet") == 1
    assert not any(r.levelname == "ERROR" for r in caplog.records)


async def test_tick_guild_id_unset_refreshes_configured_guild(temp_db, monkeypatch, clock):
    monkeypatch.setattr("src.services.scheduler.config.GUILD_ID", None)
    _seed(temp_db, guild_id=555, daily_refresh_time="09:00")
    mock_refresh = AsyncMock(return_value=_OK)
    monkeypatch.setattr("src.services.scheduler.run_daily_refresh", mock_refresh)

    inst = RefreshScheduler(_bot())
    await inst.tick.coro(inst)

    mock_refresh.assert_awaited_once()
    assert mock_refresh.await_args.args[1] == 555
    assert inst._last_run_key == {555: "2026-10-23"}


async def test_tick_guild_id_unset_one_guild_failing_does_not_affect_other(
        temp_db, monkeypatch, clock):
    monkeypatch.setattr("src.services.scheduler.config.GUILD_ID", None)
    _seed(temp_db, guild_id=111, daily_refresh_time="09:00")
    _seed(temp_db, guild_id=222, daily_refresh_time="09:00")

    async def fake_refresh(bot, guild_id, *, fetch=True):
        if guild_id == 111:
            raise RuntimeError("down")
        return _OK

    mock_refresh = AsyncMock(side_effect=fake_refresh)
    monkeypatch.setattr("src.services.scheduler.run_daily_refresh", mock_refresh)

    inst = RefreshScheduler(_bot())
    await inst.tick.coro(inst)

    assert [c.args[1] for c in mock_refresh.await_args_list] == [111, 222]
    assert 111 not in inst._last_run_key
    assert inst._last_run_key[222] == "2026-10-23"
    assert inst._next_attempt[111] is not None
    assert inst._next_attempt[222] is None

    # Guild 222 is done for the day; 111 retries after its backoff.
    clock.now += timedelta(minutes=30)
    await inst.tick.coro(inst)
    assert [c.args[1] for c in mock_refresh.await_args_list] == [111, 222, 111]
    assert inst._retry_delay[111] == timedelta(hours=2)
    assert inst._retry_delay[222] == timedelta(minutes=30)


async def test_tick_guild_id_set_ignores_other_configured_guilds(temp_db, monkeypatch, clock):
    monkeypatch.setattr("src.services.scheduler.config.GUILD_ID", 999)
    _seed(temp_db, guild_id=999, daily_refresh_time="09:00")
    _seed(temp_db, guild_id=555, daily_refresh_time="09:00")
    mock_refresh = AsyncMock(return_value=_OK)
    monkeypatch.setattr("src.services.scheduler.run_daily_refresh", mock_refresh)

    inst = RefreshScheduler(_bot())
    await inst.tick.coro(inst)

    mock_refresh.assert_awaited_once()
    assert mock_refresh.await_args.args[1] == 999
    assert set(inst._last_run_key) == {999}


async def test_tick_runs_at_matching_time(temp_db, monkeypatch, clock):
    monkeypatch.setattr("src.services.scheduler.config.GUILD_ID", 999)
    _seed(temp_db, daily_refresh_time="09:00")
    # Mock run_daily_refresh to return a successful result (no errors)
    mock_refresh = AsyncMock(return_value={"events": [], "this_week": "created", "this_month": "created"})
    monkeypatch.setattr("src.services.scheduler.run_daily_refresh", mock_refresh)

    inst = RefreshScheduler(_bot())
    await inst.tick.coro(inst)

    mock_refresh.assert_awaited_once()
    assert inst._last_run_key[999] == "2026-10-23"


async def test_tick_does_not_run_before_time(temp_db, monkeypatch, clock):
    monkeypatch.setattr("src.services.scheduler.config.GUILD_ID", 999)
    _seed(temp_db, daily_refresh_time="09:30")
    mock_refresh = AsyncMock()
    monkeypatch.setattr("src.services.scheduler.run_daily_refresh", mock_refresh)

    inst = RefreshScheduler(_bot())
    await inst.tick.coro(inst)

    mock_refresh.assert_not_awaited()
    assert 999 not in inst._last_run_key
    assert 999 not in inst._last_attempt


async def test_tick_runs_late_after_missed_minute(temp_db, monkeypatch, clock):
    """A tick well after the configured time still triggers the daily run."""
    monkeypatch.setattr("src.services.scheduler.config.GUILD_ID", 999)
    _seed(temp_db, daily_refresh_time="09:00")
    clock.now = datetime(2026, 10, 23, 11, 17, tzinfo=TZ)
    # Mock run_daily_refresh to return a successful result (no errors)
    mock_refresh = AsyncMock(return_value={"events": [], "this_week": "created", "this_month": "created"})
    monkeypatch.setattr("src.services.scheduler.run_daily_refresh", mock_refresh)

    inst = RefreshScheduler(_bot())
    await inst.tick.coro(inst)

    mock_refresh.assert_awaited_once()


async def test_tick_same_day_guard_and_next_day(temp_db, monkeypatch, clock):
    monkeypatch.setattr("src.services.scheduler.config.GUILD_ID", 999)
    _seed(temp_db, daily_refresh_time="09:00")
    # Mock run_daily_refresh to return a successful result (no errors)
    mock_refresh = AsyncMock(return_value={"events": [], "this_week": "created", "this_month": "created"})
    monkeypatch.setattr("src.services.scheduler.run_daily_refresh", mock_refresh)

    inst = RefreshScheduler(_bot())
    await inst.tick.coro(inst)
    assert mock_refresh.await_count == 1

    # Later the same day (past the retry window): must not run again.
    clock.now += timedelta(hours=2)
    await inst.tick.coro(inst)
    assert mock_refresh.await_count == 1

    # Next day: runs again.
    clock.now = datetime(2026, 10, 24, 9, 0, tzinfo=TZ)
    await inst.tick.coro(inst)
    assert mock_refresh.await_count == 2
    assert inst._last_run_key[999] == "2026-10-24"


async def test_tick_failure_swallowed_and_retried_after_window(temp_db, monkeypatch, clock):
    monkeypatch.setattr("src.services.scheduler.config.GUILD_ID", 999)
    _seed(temp_db, daily_refresh_time="09:00")
    mock_refresh = AsyncMock(side_effect=[RuntimeError("down"), {"ok": True}])
    monkeypatch.setattr("src.services.scheduler.run_daily_refresh", mock_refresh)

    inst = RefreshScheduler(_bot())
    await inst.tick.coro(inst)  # fails, must not raise
    assert mock_refresh.await_count == 1
    assert 999 not in inst._last_run_key
    assert 999 in inst._last_attempt

    # Within the 30-minute attempt window: no retry.
    clock.now += timedelta(minutes=29)
    await inst.tick.coro(inst)
    assert mock_refresh.await_count == 1

    # After the window: retry succeeds and records the day.
    clock.now += timedelta(minutes=1)
    await inst.tick.coro(inst)
    assert mock_refresh.await_count == 2
    assert inst._last_run_key[999] == "2026-10-23"

    # Done for the day.
    clock.now += timedelta(hours=1)
    await inst.tick.coro(inst)
    assert mock_refresh.await_count == 2


async def test_tick_fetch_error_does_not_set_last_run_key(temp_db, monkeypatch, clock):
    """
    When run_daily_refresh returns a result with any error string (e.g., events="error:..."),
    _last_run_key should NOT be set so retry happens after 30-minute window.
    """
    monkeypatch.setattr("src.services.scheduler.config.GUILD_ID", 999)
    _seed(temp_db, daily_refresh_time="09:00")
    # Simulate fetch failure: events="error:ValueError"
    error_result = {
        "events": "error:ValueError",
        "this_week": "created",
        "this_month": "skipped",
    }
    mock_refresh = AsyncMock(return_value=error_result)
    monkeypatch.setattr("src.services.scheduler.run_daily_refresh", mock_refresh)

    inst = RefreshScheduler(_bot())
    await inst.tick.coro(inst)

    # Because result contains "error:ValueError", _last_run_key should not be set
    assert 999 not in inst._last_run_key
    assert 999 in inst._last_attempt

    # Within 30-minute window: no retry
    clock.now += timedelta(minutes=29)
    await inst.tick.coro(inst)
    assert mock_refresh.await_count == 1

    # After 30-minute window: retry happens
    clock.now += timedelta(minutes=1)
    await inst.tick.coro(inst)
    assert mock_refresh.await_count == 2


async def test_tick_board_error_does_not_set_last_run_key(temp_db, monkeypatch, clock):
    """
    When run_daily_refresh returns a result with board error (e.g., this_month="error:..."),
    _last_run_key should NOT be set so retry happens after 30-minute window.
    """
    monkeypatch.setattr("src.services.scheduler.config.GUILD_ID", 999)
    _seed(temp_db, daily_refresh_time="09:00")
    # Simulate board upsert failure
    error_result = {
        "events": {"added": 2},
        "this_week": "created",
        "this_month": "error:RuntimeError",
    }
    mock_refresh = AsyncMock(return_value=error_result)
    monkeypatch.setattr("src.services.scheduler.run_daily_refresh", mock_refresh)

    inst = RefreshScheduler(_bot())
    await inst.tick.coro(inst)

    # Because result contains "error:RuntimeError", _last_run_key should not be set
    assert 999 not in inst._last_run_key


async def test_tick_full_success_sets_last_run_key(temp_db, monkeypatch, clock):
    """
    When run_daily_refresh returns a result with no error strings,
    _last_run_key SHOULD be set.
    """
    monkeypatch.setattr("src.services.scheduler.config.GUILD_ID", 999)
    _seed(temp_db, daily_refresh_time="09:00")
    success_result = {
        "events": {"added": 2, "updated": 1},
        "this_week": "edited",
        "this_month": "created",
    }
    mock_refresh = AsyncMock(return_value=success_result)
    monkeypatch.setattr("src.services.scheduler.run_daily_refresh", mock_refresh)

    inst = RefreshScheduler(_bot())
    await inst.tick.coro(inst)

    # All values are non-error, so _last_run_key should be set
    assert inst._last_run_key[999] == "2026-10-23"


async def test_tick_wants_error_does_not_drive_backoff(temp_db, monkeypatch, clock):
    """
    When update_wants_board fails (wants="error:..."), _last_run_key SHOULD still be set
    because wants errors are excluded from has_error scan (like pings and tonight).
    """
    monkeypatch.setattr("src.services.scheduler.config.GUILD_ID", 999)
    _seed(temp_db, daily_refresh_time="09:00", wants_channel_id=333)
    # Boards succeed, but wants fails
    success_with_wants_error = {
        "events": {"added": 2},
        "this_week": "created",
        "this_month": "created",
        "wants": "error:ConnectionError",
    }
    mock_refresh = AsyncMock(return_value=success_with_wants_error)
    monkeypatch.setattr("src.services.scheduler.run_daily_refresh", mock_refresh)

    inst = RefreshScheduler(_bot())
    await inst.tick.coro(inst)

    # Wants error should not drive backoff; _last_run_key should be set
    assert inst._last_run_key[999] == "2026-10-23"
    # No retry scheduled since this is considered a success
    assert inst._next_attempt.get(999) is None


async def test_tick_products_step_raising_does_not_drive_backoff(temp_db, monkeypatch, clock):
    """A raising products step flows through the real run_daily_refresh and stays non-driving."""
    monkeypatch.setattr("src.services.scheduler.config.GUILD_ID", 999)
    _seed(temp_db, daily_refresh_time="09:00", new_stock_channel_id=555)
    monkeypatch.setattr("src.services.scheduler.refresh_events",
                        MagicMock(return_value={"added": 0}))
    monkeypatch.setattr("src.services.scheduler.upsert_board",
                        AsyncMock(return_value="created"))
    monkeypatch.setattr("src.services.scheduler.check_new_products",
                        MagicMock(side_effect=ConnectionError("net")))

    inst = RefreshScheduler(_bot())
    await inst.tick.coro(inst)

    assert inst._last_run_key[999] == "2026-10-23"
    assert inst._next_attempt.get(999) is None


# --- fetch-once-per-day + bounded backoff ---

def _fetch_flags(mock_refresh):
    return [c.kwargs["fetch"] for c in mock_refresh.await_args_list]


async def _tick(inst):
    await inst.tick.coro(inst)


async def test_run_daily_refresh_fetch_false_skips_refresh_events(temp_db, monkeypatch):
    _seed(temp_db)
    mock_events = MagicMock(return_value={"added": 1})
    monkeypatch.setattr("src.services.scheduler.refresh_events", mock_events)
    mock_upsert = AsyncMock(side_effect=["edited", "skipped"])
    monkeypatch.setattr("src.services.scheduler.upsert_board", mock_upsert)

    result = await scheduler.run_daily_refresh(MagicMock(), 999, fetch=False)

    mock_events.assert_not_called()
    assert result == {"events": "cached", "this_week": "edited", "this_month": "skipped"}
    assert mock_upsert.await_count == 2


async def test_tick_events_success_board_failure_retry_skips_fetch(temp_db, monkeypatch, clock):
    monkeypatch.setattr("src.services.scheduler.config.GUILD_ID", 999)
    _seed(temp_db, daily_refresh_time="09:00")
    mock_refresh = AsyncMock(side_effect=[_BOARD_FAIL, _OK_CACHED])
    monkeypatch.setattr("src.services.scheduler.run_daily_refresh", mock_refresh)

    inst = RefreshScheduler(_bot())
    await _tick(inst)
    assert inst._events_done_key[999] == "2026-10-23"
    assert 999 not in inst._last_run_key

    clock.now += timedelta(minutes=30)
    await _tick(inst)

    assert _fetch_flags(mock_refresh) == [True, False]
    assert inst._last_run_key[999] == "2026-10-23"


async def test_tick_board_failure_retry_does_not_refetch_upstream(temp_db, monkeypatch, clock):
    """End-to-end with the real run_daily_refresh: WP site is fetched once."""
    monkeypatch.setattr("src.services.scheduler.config.GUILD_ID", 999)
    _seed(temp_db, daily_refresh_time="09:00")
    mock_events = MagicMock(return_value={"added": 1})
    monkeypatch.setattr("src.services.scheduler.refresh_events", mock_events)
    mock_upsert = AsyncMock(side_effect=[RuntimeError("x"), "created", "created", "skipped"])
    monkeypatch.setattr("src.services.scheduler.upsert_board", mock_upsert)

    inst = RefreshScheduler(_bot())
    await _tick(inst)
    clock.now += timedelta(minutes=30)
    await _tick(inst)

    assert mock_events.call_count == 1
    assert mock_upsert.await_count == 4
    assert inst._last_run_key[999] == "2026-10-23"


async def test_tick_fetch_failure_retry_refetches(temp_db, monkeypatch, clock):
    monkeypatch.setattr("src.services.scheduler.config.GUILD_ID", 999)
    _seed(temp_db, daily_refresh_time="09:00")
    mock_refresh = AsyncMock(side_effect=[_FETCH_FAIL, _OK])
    monkeypatch.setattr("src.services.scheduler.run_daily_refresh", mock_refresh)

    inst = RefreshScheduler(_bot())
    await _tick(inst)
    assert 999 not in inst._events_done_key

    clock.now += timedelta(minutes=30)
    await _tick(inst)

    assert _fetch_flags(mock_refresh) == [True, True]
    assert inst._events_done_key[999] == "2026-10-23"
    assert inst._last_run_key[999] == "2026-10-23"


async def test_tick_error_error_then_success_sets_last_run_key(temp_db, monkeypatch, clock):
    monkeypatch.setattr("src.services.scheduler.config.GUILD_ID", 999)
    _seed(temp_db, daily_refresh_time="09:00")
    mock_refresh = AsyncMock(side_effect=[_FETCH_FAIL, _FETCH_FAIL, _OK])
    monkeypatch.setattr("src.services.scheduler.run_daily_refresh", mock_refresh)

    inst = RefreshScheduler(_bot())
    await _tick(inst)  # 09:00 fails

    clock.now += timedelta(minutes=29)
    await _tick(inst)
    assert mock_refresh.await_count == 1

    clock.now += timedelta(minutes=1)  # 09:30 fails again
    await _tick(inst)
    assert mock_refresh.await_count == 2
    assert 999 not in inst._last_run_key

    clock.now += timedelta(minutes=59)  # delay doubled to 1h
    await _tick(inst)
    assert mock_refresh.await_count == 2

    clock.now += timedelta(minutes=1)  # 10:30 succeeds
    await _tick(inst)
    assert mock_refresh.await_count == 3
    assert inst._last_run_key[999] == "2026-10-23"
    assert inst._retry_delay[999] == timedelta(minutes=30)


async def test_tick_retry_delay_doubles_and_caps_at_two_hours(temp_db, monkeypatch, clock):
    monkeypatch.setattr("src.services.scheduler.config.GUILD_ID", 999)
    _seed(temp_db, daily_refresh_time="09:00")
    mock_refresh = AsyncMock(return_value=_FETCH_FAIL)
    monkeypatch.setattr("src.services.scheduler.run_daily_refresh", mock_refresh)

    inst = RefreshScheduler(_bot())
    await _tick(inst)
    count = 1
    for wait_minutes in (30, 60, 120, 120):
        clock.now += timedelta(minutes=wait_minutes - 1)
        await _tick(inst)
        assert mock_refresh.await_count == count, f"retried early before {wait_minutes}m"
        clock.now += timedelta(minutes=1)
        await _tick(inst)
        count += 1
        assert mock_refresh.await_count == count
    assert inst._retry_delay[999] == timedelta(hours=2)


async def test_tick_exception_also_backs_off(temp_db, monkeypatch, clock):
    monkeypatch.setattr("src.services.scheduler.config.GUILD_ID", 999)
    _seed(temp_db, daily_refresh_time="09:00")
    mock_refresh = AsyncMock(side_effect=RuntimeError("down"))
    monkeypatch.setattr("src.services.scheduler.run_daily_refresh", mock_refresh)

    inst = RefreshScheduler(_bot())
    await _tick(inst)
    clock.now += timedelta(minutes=30)
    await _tick(inst)
    assert mock_refresh.await_count == 2

    clock.now += timedelta(minutes=59)
    await _tick(inst)
    assert mock_refresh.await_count == 2
    clock.now += timedelta(minutes=1)
    await _tick(inst)
    assert mock_refresh.await_count == 3


async def test_tick_failure_on_day_n_does_not_block_next_day(temp_db, monkeypatch, clock):
    monkeypatch.setattr("src.services.scheduler.config.GUILD_ID", 999)
    _seed(temp_db, daily_refresh_time="09:00")
    mock_refresh = AsyncMock(side_effect=[_BOARD_FAIL, _BOARD_FAIL, _BOARD_FAIL, _OK])
    monkeypatch.setattr("src.services.scheduler.run_daily_refresh", mock_refresh)

    inst = RefreshScheduler(_bot())
    await _tick(inst)  # 09:00
    clock.now += timedelta(minutes=30)
    await _tick(inst)  # 09:30
    clock.now += timedelta(hours=1)
    await _tick(inst)  # 10:30, next retry not until 12:30
    assert mock_refresh.await_count == 3
    assert 999 not in inst._last_run_key

    clock.now = datetime(2026, 10, 24, 9, 0, tzinfo=TZ)
    await _tick(inst)

    assert mock_refresh.await_count == 4
    assert _fetch_flags(mock_refresh)[-1] is True  # new day refetches upstream
    assert inst._last_run_key[999] == "2026-10-24"
    assert inst._events_done_key[999] == "2026-10-24"


# --- final review: rollback + per-guild refresh lock ---

async def test_run_daily_refresh_rolls_back_after_fetch_commit_failure(temp_db, monkeypatch):
    from unittest.mock import PropertyMock

    from sqlalchemy.exc import IntegrityError

    _seed(temp_db)

    def failing_refresh(session, guild_id, base_url, tz):
        # Unique (guild_id, external_id) violation fails inside flush, poisoning the session.
        for _ in range(2):
            session.add(Event(guild_id=guild_id, external_id="wp:dup", title="t", url="u"))
        session.commit()

    monkeypatch.setattr("src.services.scheduler.refresh_events", failing_refresh)
    sent = AsyncMock()
    type(sent).id = PropertyMock(return_value=555)
    channel = AsyncMock()
    type(channel).id = PropertyMock(return_value=111)
    channel.send = AsyncMock(return_value=sent)
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=channel)

    result = await scheduler.run_daily_refresh(bot, 999)

    assert result["events"] == f"error:{IntegrityError.__name__}"
    assert result["this_week"] == "created"
    assert result["this_month"] == "created"


async def test_tick_rolls_back_session_after_guild_failure(temp_db, monkeypatch, clock):
    monkeypatch.setattr("src.services.scheduler.config.GUILD_ID", None)
    _seed(temp_db, guild_id=111, daily_refresh_time="09:00")
    _seed(temp_db, guild_id=222, daily_refresh_time="09:00")
    sessions = []
    usable = []

    async def fake_tick_guild(self, session, guild_id):
        sessions.append(session)
        usable.append(session.get(GuildConfig, guild_id) is not None)
        if guild_id == 111:
            session.add(Event(guild_id=1, external_id="wp:dup", title="t", url="u"))
            session.add(Event(guild_id=1, external_id="wp:dup", title="t", url="u"))
            session.commit()  # flush fails, poisoning the session

    monkeypatch.setattr(RefreshScheduler, "_tick_guild", fake_tick_guild)
    inst = RefreshScheduler(_bot())
    await inst.tick.coro(inst)

    assert len(sessions) == 2
    assert sessions[0] is sessions[1]
    assert usable == [True, True]  # second guild's query would raise PendingRollbackError


async def test_concurrent_refreshes_for_same_guild_serialize(temp_db, monkeypatch):
    import time

    _seed(temp_db)
    events = []

    def slow_refresh(session, guild_id, base_url, tz):
        events.append("fetch_start")
        time.sleep(0.05)
        events.append("fetch_end")
        return {"added": 0}

    async def fake_upsert(bot, session, guild_id, board_type):
        events.append(f"upsert:{board_type}")
        return "skipped"

    monkeypatch.setattr("src.services.scheduler.refresh_events", slow_refresh)
    monkeypatch.setattr("src.services.scheduler.upsert_board", fake_upsert)

    await asyncio.gather(
        scheduler.run_daily_refresh(MagicMock(), 999),
        scheduler.run_daily_refresh(MagicMock(), 999),
    )

    run = ["fetch_start", "fetch_end", "upsert:this_week", "upsert:this_month"]
    assert events == run + run


async def test_refresh_locks_are_per_guild(temp_db, monkeypatch):
    assert scheduler._get_refresh_lock(1) is scheduler._get_refresh_lock(1)
    assert scheduler._get_refresh_lock(1) is not scheduler._get_refresh_lock(2)
