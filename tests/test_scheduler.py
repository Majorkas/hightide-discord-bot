from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from src.db.models import Base, GuildConfig
from src.db.session import get_engine, get_session_factory
from src.services import scheduler
from src.services.scheduler import RefreshScheduler, _in_run_window, should_run_now

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


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Temp DB factory for scheduler tests; monkeypatches scheduler._session_factory."""
    url = f"sqlite:///{tmp_path / 'test.db'}"
    engine = get_engine(url)
    Base.metadata.create_all(engine)
    factory = get_session_factory(url)
    monkeypatch.setattr(scheduler, "_session_factory", factory)
    yield factory
    engine.dispose()
    factory.kw["bind"].dispose()


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

    assert result == {"events": {"added": 2, "updated": 1},
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
