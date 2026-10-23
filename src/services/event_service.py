import logging

from sqlalchemy.orm import Session

from src.db.repositories import (
    get_active_events, mark_absent_events_stale, upsert_event,
)
from src.scraping.fetcher import fetch_events
from src.scraping.hightide_parser import parse_product

log = logging.getLogger("hightide.events")


def upsert_products(session: Session, guild_id: int, products: list[dict], tz) -> dict:
    created = updated = 0
    seen: set[str] = set()
    for product in products:
        ev = parse_product(product, tz)
        seen.add(ev.external_id)
        _, was_created = upsert_event(session, guild_id, {
            "external_id": ev.external_id,
            "title": ev.title,
            "url": ev.url,
            "starts_at": ev.starts_at,
            "time_known": ev.time_known,
            "price_text": ev.price_text,
            "summary": ev.summary,
            "image_url": ev.image_url,
            "fingerprint": ev.fingerprint,
        })
        if was_created:
            created += 1
        else:
            updated += 1
    # Callers must upsert every seen event BEFORE calling this; it never resets counters for seen events.
    deactivated = mark_absent_events_stale(session, guild_id, seen)
    stats = {
        "fetched": len(products), "created": created,
        "updated": updated, "deactivated": deactivated,
    }
    log.info("upsert complete: %s", stats)
    return stats


def refresh_events(session: Session, guild_id: int, base_url: str, tz) -> dict:
    products = fetch_events(base_url)
    return upsert_products(session, guild_id, products, tz)
