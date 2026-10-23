from dataclasses import dataclass
from datetime import datetime

from dateutil import parser as dateutil_parser

from src.scraping.normalize import (
    clean_title, extract_price, extract_time, fingerprint, parse_event_datetime,
    strip_html,
)


@dataclass
class NormalizedEvent:
    external_id: str
    title: str
    url: str
    starts_at: datetime | None
    time_known: bool
    price_text: str | None
    summary: str | None
    image_url: str | None
    fingerprint: str


def _image_url(product: dict) -> str | None:
    embedded = product.get("_embedded", {})
    media = embedded.get("wp:featuredmedia") or []
    if media and isinstance(media, list):
        return media[0].get("source_url")
    return None


def parse_product(product: dict, tz) -> NormalizedEvent:
    title = clean_title(product["title"]["rendered"])
    content = product.get("content", {}).get("rendered", "")
    starts_at, _ = parse_event_datetime(title, tz)

    time_known = False
    time_str = extract_time(content)
    if starts_at is not None and time_str:
        try:
            t = dateutil_parser.parse(time_str).time()
            starts_at = starts_at.replace(hour=t.hour, minute=t.minute, second=0)
            time_known = True
        except (ValueError, OverflowError):
            pass

    price_text = extract_price(content)
    summary_text = strip_html(content) or None
    if summary_text and len(summary_text) > 280:
        summary_text = summary_text[:277] + "..."
    starts_iso = starts_at.isoformat() if starts_at else None
    fp = fingerprint(title, starts_iso, price_text, summary_text)

    return NormalizedEvent(
        external_id=f"wp:{product['id']}",
        title=title,
        url=product["link"],
        starts_at=starts_at,
        time_known=time_known,
        price_text=price_text,
        summary=summary_text,
        image_url=_image_url(product),
        fingerprint=fp,
    )
