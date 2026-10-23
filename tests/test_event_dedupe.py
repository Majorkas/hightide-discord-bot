import json
from pathlib import Path
from zoneinfo import ZoneInfo

from src.scraping.hightide_parser import parse_product
from src.services.event_service import upsert_products
from src.db.repositories import get_active_events

TZ = ZoneInfo("Australia/Perth")
FIXTURE = json.loads(Path("tests/fixtures/wp_products_events.json").read_text())


def test_upsert_all_fixture_events(session):
    stats = upsert_products(session, guild_id=1, products=FIXTURE, tz=TZ)
    assert stats["created"] == len(FIXTURE)
    assert len(get_active_events(session, 1)) == len(FIXTURE)


def test_upsert_is_idempotent(session):
    upsert_products(session, 1, FIXTURE, TZ)
    stats = upsert_products(session, 1, FIXTURE, TZ)
    assert stats["created"] == 0
    assert stats["updated"] == len(FIXTURE)
    assert len(get_active_events(session, 1)) == len(FIXTURE)


def test_stale_deactivation(session):
    upsert_products(session, 1, FIXTURE, TZ)
    upsert_products(session, 1, FIXTURE[:5], TZ)  # miss 1
    stats = upsert_products(session, 1, FIXTURE[:5], TZ)  # miss 2
    assert stats["deactivated"] == len(FIXTURE) - 5
    assert len(get_active_events(session, 1)) == 5
