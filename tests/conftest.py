import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.db.models import Base
from src.db.session import get_engine, get_session_factory
from src.services import board_service, scheduler

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "wp_products_events.json"


@pytest.fixture(autouse=True)
def _clear_board_locks():
    board_service._board_locks.clear()
    scheduler._refresh_locks.clear()
    yield
    board_service._board_locks.clear()
    scheduler._refresh_locks.clear()


@pytest.fixture(autouse=True)
def _no_weekly_seeding(monkeypatch):
    # Keep board tests independent of the default recurring roster; weekly tests opt back in.
    monkeypatch.setattr(board_service, "seed_weekly_recurring", lambda session, guild_id: 0)


@pytest.fixture
def session():
    engine = get_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = get_session_factory("sqlite:///:memory:")
    factory.configure(bind=engine)
    s = factory()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


@pytest.fixture
def fixture_products() -> list[dict]:
    return json.loads(FIXTURE_PATH.read_text())


@pytest.fixture
def tz() -> ZoneInfo:
    return ZoneInfo("Australia/Perth")


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """File-backed DB factory; monkeypatches scheduler._session_factory."""
    url = f"sqlite:///{tmp_path / 'test.db'}"
    engine = get_engine(url)
    Base.metadata.create_all(engine)
    factory = get_session_factory(url)
    monkeypatch.setattr(scheduler, "_session_factory", factory)
    yield factory
    engine.dispose()
    factory.kw["bind"].dispose()
