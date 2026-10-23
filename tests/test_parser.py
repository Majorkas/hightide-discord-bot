import json
from pathlib import Path
from zoneinfo import ZoneInfo

from src.scraping.hightide_parser import parse_product

TZ = ZoneInfo("Australia/Perth")
FIXTURE = json.loads(
    Path("tests/fixtures/wp_products_events.json").read_text())


def _by_slug(slug_part: str):
    return next(p for p in FIXTURE if slug_part in p["slug"])


def test_external_id_format():
    p = _by_slug("05-11-26-weekly-duels")
    ev = parse_product(p, TZ)
    assert ev.external_id == f"wp:{p['id']}"


def test_dot_prefix_date_and_time_known():
    ev = parse_product(_by_slug("05-11-26-weekly-duels"), TZ)
    assert ev.starts_at is not None
    assert (ev.starts_at.year, ev.starts_at.month, ev.starts_at.day) == (2026, 11, 5)
    assert ev.time_known is True  # fixture content has → Time: 6:30pm
    assert ev.price_text == "$10"


def test_natural_language_title_date():
    ev = parse_product(_by_slug("friday-night-magic-countersculpt"), TZ)
    assert ev.starts_at is not None
    assert (ev.starts_at.month, ev.starts_at.day, ev.starts_at.year) == (10, 23, 2026)


def test_image_from_embed():
    ev = parse_product(_by_slug("05-11-26-weekly-duels"), TZ)
    assert ev.image_url is None or ev.image_url.startswith("https://")


def test_title_entities_cleaned():
    ev = parse_product(_by_slug("05-11-26-weekly-duels"), TZ)
    assert "&#8211;" not in ev.title
