import re
from dataclasses import dataclass
from datetime import datetime, time

from bs4 import BeautifulSoup
from dateutil import parser as dateutil_parser

from src.scraping.normalize import (
    _DOT_PREFIX, _NATURAL_DATE, clean_title, extract_price, extract_time,
    fingerprint, parse_event_datetime,
)

MAX_TITLE_CHARS = 200
MAX_PRICE_CHARS = 80
MAX_URL_CHARS = 500

# A bare number like "11" or "10 - 6" is not a start time; need am/pm or HH:MM.
_TIME_MARKER = re.compile(r"(?<![a-z])[ap]\.?m\.?\b|\d:\d", re.IGNORECASE)
_SEPARATORS = re.compile(r"^[\s\-–—:,]+|[\s\-–—:,]+$")


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


def _norm(s: str) -> str:
    s = re.sub(r"[–—]", "-", s.lower())
    s = re.sub(r"\s+", " ", s)
    return _SEPARATORS.sub("", s)


def _date_free_forms(s: str) -> set[str]:
    """Normalised forms of s with/without its dot-prefix and natural-language date."""
    no_dot = _DOT_PREFIX.sub("", s)
    forms = {
        _norm(s),
        _norm(no_dot),
        _norm(_NATURAL_DATE.sub("", s)),
        _norm(_NATURAL_DATE.sub("", no_dot)),
    }
    forms.discard("")
    return forms


def _is_title_duplicate(para_text: str, title: str) -> bool:
    return bool(_date_free_forms(para_text) & _date_free_forms(title))


def _extract_summary(content_html: str, title: str) -> str | None:
    """First non-metadata paragraph that isn't an echo of the title."""
    if not content_html:
        return None

    soup = BeautifulSoup(content_html, "html.parser")
    for p in soup.find_all("p"):
        text = p.get_text(" ", strip=True)
        if not text:
            continue
        if text.startswith("→"):
            continue
        if _is_title_duplicate(text, title):
            continue
        if len(text) > 280:
            return text[:277] + "..."
        return text
    return None


def _image_url(product: dict) -> str | None:
    embedded = product.get("_embedded", {})
    media = embedded.get("wp:featuredmedia") or []
    if media and isinstance(media, list):
        return media[0].get("source_url")
    return None


def _parse_clock(s: str) -> time | None:
    if not _TIME_MARKER.search(s):
        return None
    try:
        return dateutil_parser.parse(s).time()
    except (ValueError, OverflowError):
        return None


def parse_product(product: dict, tz) -> NormalizedEvent:
    full_title = clean_title(product["title"]["rendered"])
    content = product.get("content", {}).get("rendered", "")
    starts_at, _ = parse_event_datetime(full_title, tz)
    title = full_title[:MAX_TITLE_CHARS]

    time_known = False
    time_str = extract_time(content)
    if starts_at is not None and time_str:
        t = _parse_clock(time_str)
        if t is None and time_str.lower().startswith("from "):
            t = _parse_clock(time_str[5:])
        if t is not None:
            starts_at = starts_at.replace(hour=t.hour, minute=t.minute, second=0)
            time_known = True

    price_text = extract_price(content)
    if price_text is not None:
        price_text = price_text[:MAX_PRICE_CHARS]
    summary_text = _extract_summary(content, full_title)
    starts_iso = starts_at.isoformat() if starts_at else None
    fp = fingerprint(title, starts_iso, price_text, summary_text)

    return NormalizedEvent(
        external_id=f"wp:{product['id']}",
        title=title,
        url=product["link"][:MAX_URL_CHARS],
        starts_at=starts_at,
        time_known=time_known,
        price_text=price_text,
        summary=summary_text,
        image_url=_image_url(product),
        fingerprint=fp,
    )
