import copy
import logging

from sqlalchemy import select

from src.db.models import Event
from src.db.repositories import get_active_events
from src.services.event_service import upsert_products

SLUG = "05-11-26-weekly-duels"


def _row(session, external_id: str) -> Event:
    return session.execute(
        select(Event).where(Event.external_id == external_id)).scalar_one()


def _product(products, slug_part=SLUG):
    return next(p for p in products if slug_part in p["slug"])


def test_upsert_all_fixture_events(session, fixture_products, tz):
    stats = upsert_products(session, guild_id=1, products=fixture_products, tz=tz)
    assert stats["created"] == len(fixture_products)
    assert stats["failed"] == 0
    assert len(get_active_events(session, 1)) == len(fixture_products)


def test_upsert_is_idempotent(session, fixture_products, tz):
    upsert_products(session, 1, fixture_products, tz)
    stats = upsert_products(session, 1, fixture_products, tz)
    assert stats["created"] == 0
    assert stats["unchanged"] == len(fixture_products)
    assert stats["updated"] == 0
    assert len(get_active_events(session, 1)) == len(fixture_products)


def test_stale_deactivation(session, fixture_products, tz):
    upsert_products(session, 1, fixture_products, tz)
    upsert_products(session, 1, fixture_products[:5], tz)  # miss 1
    stats = upsert_products(session, 1, fixture_products[:5], tz)  # miss 2
    assert stats["deactivated"] == len(fixture_products) - 5
    assert len(get_active_events(session, 1)) == 5


def test_price_change_is_detected(session, fixture_products, tz):
    upsert_products(session, 1, fixture_products, tz)
    ext_id = f"wp:{_product(fixture_products)['id']}"
    before = _row(session, ext_id)
    old_price, old_fp = before.price_text, before.fingerprint
    assert old_price == "$10"

    mutated = copy.deepcopy(fixture_products)
    p = _product(mutated)
    p["content"]["rendered"] = p["content"]["rendered"].replace(
        "→ Ticket: $10", "→ Ticket: $12")
    stats = upsert_products(session, 1, mutated, tz)

    assert stats["updated"] == 1
    assert stats["unchanged"] == len(fixture_products) - 1
    assert stats["created"] == 0
    after = _row(session, ext_id)
    assert after.price_text == "$12"
    assert after.fingerprint != old_fp


def test_unchanged_reupsert_counts_unchanged(session, fixture_products, tz):
    upsert_products(session, 1, fixture_products, tz)
    stats = upsert_products(session, 1, copy.deepcopy(fixture_products), tz)
    assert (stats["updated"], stats["unchanged"]) == (0, len(fixture_products))


def test_title_change_same_wp_id_keeps_row_count(session, fixture_products, tz):
    upsert_products(session, 1, fixture_products, tz)
    mutated = copy.deepcopy(fixture_products)
    p = _product(mutated)
    p["title"]["rendered"] = "05.11.26 &#8211; Weekly Duels &#8211; Renamed"
    stats = upsert_products(session, 1, mutated, tz)

    assert stats["updated"] == 1 and stats["created"] == 0
    assert session.query(Event).count() == len(fixture_products)
    assert _row(session, f"wp:{p['id']}").title == "05.11.26 – Weekly Duels – Renamed"


def test_poison_product_is_isolated_and_not_deactivated(
        session, fixture_products, tz, caplog):
    upsert_products(session, 1, fixture_products, tz)
    poison = copy.deepcopy(fixture_products)
    del _product(poison)["title"]
    with caplog.at_level(logging.ERROR, logger="hightide.events"):
        stats1 = upsert_products(session, 1, poison, tz)
        stats2 = upsert_products(session, 1, poison, tz)
        stats3 = upsert_products(session, 1, poison, tz)
    assert stats1["failed"] == 1
    assert stats1["unchanged"] == len(fixture_products) - 1
    assert stats3["deactivated"] == 0
    assert stats2["deactivated"] == 0
    assert len(get_active_events(session, 1)) == len(fixture_products)
    assert str(_product(poison)["id"]) in caplog.text


def test_source_url_populated(session, fixture_products, tz):
    upsert_products(session, 1, fixture_products[:3], tz,
                    source_url="https://hightidetabletop.com.au")
    rows = session.query(Event).all()
    assert rows and all(r.source_url == "https://hightidetabletop.com.au" for r in rows)


def test_malformed_product_attribute_error_isolated(session, fixture_products, tz):
    bad = {"id": 99999, "title": {"rendered": "Broken Event"}, "content": None}
    stats = upsert_products(session, 1, [bad] + fixture_products[:3], tz)
    assert stats["failed"] == 1
    assert stats["created"] == 3
