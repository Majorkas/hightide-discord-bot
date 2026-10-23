import logging

from sqlalchemy.orm import Session

from src.db.repositories import (
    mark_absent_events_stale, upsert_event,
)
from src.scraping.fetcher import fetch_events
from src.scraping.hightide_parser import parse_product_or_none

log = logging.getLogger("hightide.events")


def upsert_products(session: Session, guild_id: int, products: list[dict], tz,
                    source_url: str | None = None) -> dict:
    created = updated = unchanged = failed = filtered = 0
    seen: set[str] = set()
    for product in products:
        try:
            ev = parse_product_or_none(product, tz)
            if ev is None:
                # Junk stays "seen" so stale-marking never touches it.
                filtered += 1
                if product.get("id") is not None:
                    seen.add(f"wp:{product['id']}")
                continue
            data = {
                "external_id": ev.external_id,
                "title": ev.title,
                "url": ev.url,
                "starts_at": ev.starts_at,
                "time_known": ev.time_known,
                "price_text": ev.price_text,
                "summary": ev.summary,
                "image_url": ev.image_url,
                "fingerprint": ev.fingerprint,
            }
            if source_url is not None:
                data["source_url"] = source_url
            _, was_created, changed = upsert_event(session, guild_id, data)
        except (KeyError, TypeError, ValueError, AttributeError):
            pid = product.get("id") if isinstance(product, dict) else None
            log.exception("failed to process product id=%s", pid)
            session.rollback()
            failed += 1
            if pid is not None:
                # Don't let a poison product count as absent and get deactivated.
                seen.add(f"wp:{pid}")
            continue
        seen.add(ev.external_id)
        if was_created:
            created += 1
        elif changed:
            updated += 1
        else:
            unchanged += 1
    # Callers must upsert every seen event BEFORE calling this; it never resets counters for seen events.
    deactivated = mark_absent_events_stale(session, guild_id, seen)
    stats = {
        "fetched": len(products), "created": created, "updated": updated,
        "unchanged": unchanged, "deactivated": deactivated, "failed": failed,
        "filtered": filtered,
    }
    log.info("upsert complete: %s", stats)
    return stats


def refresh_events(session: Session, guild_id: int, base_url: str, tz) -> dict:
    products = fetch_events(base_url)
    return upsert_products(session, guild_id, products, tz, source_url=base_url)
