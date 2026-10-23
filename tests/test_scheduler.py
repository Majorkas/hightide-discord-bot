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

@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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

@pytest.mark.asyncio
async def test_tick_guild_id_none_skip(temp_db, monkeypatch):
    monkeypatch.setattr("src.services.scheduler.config.GUILD_ID", None)
    mock_refresh = AsyncMock()
    monkeypatch.setattr("src.services.scheduler.run_daily_refresh", mock_refresh)

    bot = _bot()
    inst = RefreshScheduler(bot)
    await inst.tick.coro(inst)

    mock_refresh.assert_not_awaited()
    bot.wait_until_ready.assert_awaited_once()


@pytest.mark.asyncio
async def test_tick_runs_at_matching_time(temp_db, monkeypatch, clock):
    monkeypatch.setattr("src.services.scheduler.config.GUILD_ID", 999)
    _seed(temp_db, daily_refresh_time="09:00")
    # Mock run_daily_refresh to return a successful result (no errors)
    mock_refresh = AsyncMock(return_value={"events": [], "this_week": "created", "this_month": "created"})
    monkeypatch.setattr("src.services.scheduler.run_daily_refresh", mock_refresh)

    inst = RefreshScheduler(_bot())
    await inst.tick.coro(inst)

    mock_refresh.assert_awaited_once()
    assert inst._last_run_key == "2026-10-23"


@pytest.mark.asyncio
async def test_tick_does_not_run_before_time(temp_db, monkeypatch, clock):
    monkeypatch.setattr("src.services.scheduler.config.GUILD_ID", 999)
    _seed(temp_db, daily_refresh_time="09:30")
    mock_refresh = AsyncMock()
    monkeypatch.setattr("src.services.scheduler.run_daily_refresh", mock_refresh)

    inst = RefreshScheduler(_bot())
    await inst.tick.coro(inst)

    mock_refresh.assert_not_awaited()
    assert inst._last_run_key is None
    assert inst._last_attempt is None


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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
    assert inst._last_run_key == "2026-10-24"


@pytest.mark.asyncio
async def test_tick_failure_swallowed_and_retried_after_window(temp_db, monkeypatch, clock):
    monkeypatch.setattr("src.services.scheduler.config.GUILD_ID", 999)
    _seed(temp_db, daily_refresh_time="09:00")
    mock_refresh = AsyncMock(side_effect=[RuntimeError("down"), {"ok": True}])
    monkeypatch.setattr("src.services.scheduler.run_daily_refresh", mock_refresh)

    inst = RefreshScheduler(_bot())
    await inst.tick.coro(inst)  # fails, must not raise
    assert mock_refresh.await_count == 1
    assert inst._last_run_key is None
    assert inst._last_attempt is not None

    # Within the 30-minute attempt window: no retry.
    clock.now += timedelta(minutes=29)
    await inst.tick.coro(inst)
    assert mock_refresh.await_count == 1

    # After the window: retry succeeds and records the day.
    clock.now += timedelta(minutes=1)
    await inst.tick.coro(inst)
    assert mock_refresh.await_count == 2
    assert inst._last_run_key == "2026-10-23"

    # Done for the day.
    clock.now += timedelta(hours=1)
    await inst.tick.coro(inst)
    assert mock_refresh.await_count == 2


@pytest.mark.asyncio
async def test_tick_partial_error_does_not_set_last_run_key(temp_db, monkeypatch, clock):
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
    assert inst._last_run_key is None
    assert inst._last_attempt is not None

    # Within 30-minute window: no retry
    clock.now += timedelta(minutes=29)
    await inst.tick.coro(inst)
    assert mock_refresh.await_count == 1

    # After 30-minute window: retry happens
    clock.now += timedelta(minutes=1)
    await inst.tick.coro(inst)
    assert mock_refresh.await_count == 2


@pytest.mark.asyncio
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
    assert inst._last_run_key is None


@pytest.mark.asyncio
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
    assert inst._last_run_key == "2026-10-23"
