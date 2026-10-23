import copy
import logging

from sqlalchemy import select

from src.db.models import Event
from src.db.repositories import get_active_events
from src.services.event_service import upsert_products

SLUG = "05-11-26-weekly-duels"
JUNK_COUNT = 3  # test-event-30-09, product-placeholder, painters-guild-entry


def _real_count(products) -> int:
    return len(products) - JUNK_COUNT


def _row(session, external_id: str) -> Event:
    return session.execute(
        select(Event).where(Event.external_id == external_id)).scalar_one()


def _product(products, slug_part=SLUG):
    return next(p for p in products if slug_part in p["slug"])


def test_upsert_all_fixture_events(session, fixture_products, tz):
    stats = upsert_products(session, guild_id=1, products=fixture_products, tz=tz)
    assert stats["fetched"] == len(fixture_products)
    assert stats["filtered"] == JUNK_COUNT
    assert stats["created"] == _real_count(fixture_products)
    assert stats["failed"] == 0
    assert len(get_active_events(session, 1)) == _real_count(fixture_products)


def test_junk_products_not_persisted(session, fixture_products, tz):
    upsert_products(session, 1, fixture_products, tz)
    titles = [e.title.lower() for e in session.query(Event).all()]
    assert not any(
        j in t for t in titles for j in ("test", "placeholder", "painters guild"))


def test_preexisting_junk_rows_self_heal_via_stale_marking(
        session, fixture_products, tz):
    junk = next(p for p in fixture_products if p["slug"] == "product-placeholder")
    # A pre-existing active row for the junk id (e.g. from before the filter existed).
    session.add(Event(guild_id=1, external_id=f"wp:{junk['id']}", title="Product placeholder",
                      url="u", fingerprint="x", is_active=True, missed_refreshes=0))
    session.commit()
    
    # First run: junk is filtered but NOT marked seen, so it gets a miss
    stats1 = upsert_products(session, 1, fixture_products, tz)
    assert stats1["filtered"] == JUNK_COUNT
    row1 = _row(session, f"wp:{junk['id']}")
    assert row1.missed_refreshes == 1
    assert row1.is_active is True
    
    # Second run: another miss triggers deactivation (2-miss threshold)
    stats2 = upsert_products(session, 1, fixture_products, tz)
    assert stats2["filtered"] == JUNK_COUNT
    row2 = _row(session, f"wp:{junk['id']}")
    assert row2.missed_refreshes == 2
    assert row2.is_active is False


def test_filtered_not_counted_in_created_or_updated(session, tz):
    junk = {"id": 1, "slug": "t", "title": {"rendered": "Test event"},
            "content": {"rendered": ""}, "link": "http://x"}
    stats = upsert_products(session, 1, [junk], tz)
    assert (stats["filtered"], stats["created"], stats["updated"],
            stats["unchanged"], stats["failed"]) == (1, 0, 0, 0, 0)
    assert session.query(Event).count() == 0


def test_upsert_is_idempotent(session, fixture_products, tz):
    upsert_products(session, 1, fixture_products, tz)
    stats = upsert_products(session, 1, fixture_products, tz)
    assert stats["created"] == 0
    assert stats["unchanged"] == _real_count(fixture_products)
    assert stats["updated"] == 0
    assert stats["filtered"] == JUNK_COUNT
    assert len(get_active_events(session, 1)) == _real_count(fixture_products)


def test_stale_deactivation(session, fixture_products, tz):
    upsert_products(session, 1, fixture_products, tz)
    upsert_products(session, 1, fixture_products[:5], tz)  # miss 1
    stats = upsert_products(session, 1, fixture_products[:5], tz)  # miss 2
    assert stats["deactivated"] == _real_count(fixture_products) - 5
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
    assert stats["unchanged"] == _real_count(fixture_products) - 1
    assert stats["created"] == 0
    after = _row(session, ext_id)
    assert after.price_text == "$12"
    assert after.fingerprint != old_fp


def test_unchanged_reupsert_counts_unchanged(session, fixture_products, tz):
    upsert_products(session, 1, fixture_products, tz)
    stats = upsert_products(session, 1, copy.deepcopy(fixture_products), tz)
    assert (stats["updated"], stats["unchanged"]) == (0, _real_count(fixture_products))


def test_title_change_same_wp_id_keeps_row_count(session, fixture_products, tz):
    upsert_products(session, 1, fixture_products, tz)
    mutated = copy.deepcopy(fixture_products)
    p = _product(mutated)
    p["title"]["rendered"] = "05.11.26 &#8211; Weekly Duels &#8211; Renamed"
    stats = upsert_products(session, 1, mutated, tz)

    assert stats["updated"] == 1 and stats["created"] == 0
    assert session.query(Event).count() == _real_count(fixture_products)
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
    assert stats1["unchanged"] == _real_count(fixture_products) - 1
    assert stats3["deactivated"] == 0
    assert stats2["deactivated"] == 0
    assert len(get_active_events(session, 1)) == _real_count(fixture_products)
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
