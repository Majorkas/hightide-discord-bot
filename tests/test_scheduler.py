from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from src.db.models import Base, GuildConfig
from src.db.session import get_engine, get_session_factory
from src.services import scheduler
from src.services.scheduler import should_run_now


def test_should_run_now_at_configured_time():
    assert should_run_now("09:00", 9, 0) is True
    assert should_run_now("09:00", 9, 1) is False
    assert should_run_now("9:05", 9, 5) is True


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


@pytest.mark.asyncio
async def test_run_daily_refresh(temp_db, monkeypatch):
    """Test that run_daily_refresh returns correct dict shape and both boards are upserted."""
    # Seed DB with config
    with temp_db() as s:
        cfg = GuildConfig(guild_id=999, timezone="Australia/Perth", this_week_channel_id=111, this_month_channel_id=222)
        s.add(cfg)
        s.commit()
    
    # Mock refresh_events (synchronous, not async, since it's run via asyncio.to_thread)
    mock_refresh = MagicMock(return_value={"added": 2, "updated": 1})
    monkeypatch.setattr("src.services.scheduler.refresh_events", mock_refresh)
    
    # First call returns "created", second returns "skipped" via side_effect
    mock_upsert = AsyncMock(side_effect=["created", "skipped"])
    monkeypatch.setattr("src.services.scheduler.upsert_board", mock_upsert)
    
    bot = MagicMock()
    result = await scheduler.run_daily_refresh(bot, 999)
    
    # Check result shape
    assert "events" in result
    assert "this_week" in result
    assert "this_month" in result
    assert result["events"] == {"added": 2, "updated": 1}
    assert result["this_week"] == "created"
    assert result["this_month"] == "skipped"
    
    # Verify both upsert calls were made
    assert mock_upsert.call_count == 2


@pytest.mark.asyncio
async def test_tick_guild_id_none_skip(temp_db, monkeypatch):
    """Test that tick skips when GUILD_ID is None."""
    monkeypatch.setattr("src.services.scheduler.config.GUILD_ID", None)
    mock_refresh = AsyncMock()
    monkeypatch.setattr("src.services.scheduler.run_daily_refresh", mock_refresh)
    
    bot = MagicMock()
    bot.wait_until_ready = AsyncMock()
    
    from src.services.scheduler import RefreshScheduler
    scheduler_inst = RefreshScheduler(bot)
    # Call the underlying coroutine function directly
    await scheduler_inst.tick.coro(scheduler_inst)
    
    # Should not call run_daily_refresh
    mock_refresh.assert_not_awaited()
    # But should call wait_until_ready
    bot.wait_until_ready.assert_awaited_once()


@pytest.mark.asyncio
async def test_tick_last_run_key_same_day_guard(temp_db, monkeypatch):
    """Test that refresh runs only once per day via _last_run_key guard."""
    from src.services.scheduler import RefreshScheduler
    
    # Set config GUILD_ID
    monkeypatch.setattr("src.services.scheduler.config.GUILD_ID", 999)
    
    # Seed DB
    with temp_db() as s:
        cfg = GuildConfig(guild_id=999, timezone="Australia/Perth", daily_refresh_time="14:30")
        s.add(cfg)
        s.commit()
    
    # Mock run_daily_refresh
    mock_refresh = AsyncMock()
    monkeypatch.setattr("src.services.scheduler.run_daily_refresh", mock_refresh)
    
    # Mock should_run_now to always return True (i.e., at the refresh time)
    monkeypatch.setattr("src.services.scheduler.should_run_now", lambda hhmm, h, m: True)
    
    bot = MagicMock()
    bot.wait_until_ready = AsyncMock()
    
    scheduler_inst = RefreshScheduler(bot)
    
    # First tick should run refresh
    await scheduler_inst.tick.coro(scheduler_inst)
    assert mock_refresh.call_count == 1
    
    # Second tick (same minute, same day) should NOT run refresh
    await scheduler_inst.tick.coro(scheduler_inst)
    assert mock_refresh.call_count == 1  # Still 1, not 2
    
    # Simulate next day by resetting _last_run_key
    scheduler_inst._last_run_key = None
    await scheduler_inst.tick.coro(scheduler_inst)
    assert mock_refresh.call_count == 2  # Now it runs again
