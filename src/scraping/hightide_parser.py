from dataclasses import dataclass
from datetime import datetime

from bs4 import BeautifulSoup
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


def _extract_summary(content_html: str, title: str) -> str | None:
    """Extract the first non-metadata, non-title-duplicate paragraph as summary."""
    if not content_html:
        return None
    
    soup = BeautifulSoup(content_html, "html.parser")
    paragraphs = soup.find_all("p")
    
    for p in paragraphs:
        text = p.get_text(" ", strip=True)
        
        # Skip paragraphs starting with arrow metadata
        if text.startswith("→"):
            continue
        
        # Skip paragraphs that are near-duplicates of the title
        # (title often repeats in first paragraph after normalization)
        if _is_title_duplicate(text, title):
            continue
        
        # Found the first real summary paragraph
        if len(text) > 280:
            return text[:277] + "..."
        return text if text else None
    
    return None


def _is_title_duplicate(para_text: str, title: str) -> bool:
    """Check if a paragraph text is a near-duplicate of the title."""
    # Normalize both strings: lowercase, collapse whitespace, remove common punctuation
    import re
    
    def normalize(s: str) -> str:
        s = s.lower().strip()
        # Replace em-dashes and en-dashes with regular dash
        s = re.sub(r'[–—]', '-', s)
        # Collapse whitespace
        s = re.sub(r'\s+', ' ', s)
        return s
    
    para_norm = normalize(para_text)
    title_norm = normalize(title)
    
    # Exact match after normalization
    if para_norm == title_norm:
        return True
    
    # Check if the cleaned paragraph (without date prefix) matches title
    # e.g., "05.11.26 - Weekly Duels - Yu-Gi-Oh!" title vs "Weekly Duels - Yu-Gi-Oh!" paragraph
    # Remove leading date patterns (e.g., "05.11.26 - ")
    para_cleaned = re.sub(r'^\d{1,2}\.\d{1,2}\.\d{2,4}\s*-?\s*', '', para_norm)
    if para_cleaned and para_cleaned == title_norm:
        return True
    if para_cleaned and para_cleaned == re.sub(r'^\d{1,2}\.\d{1,2}\.\d{2,4}\s*-?\s*', '', title_norm):
        return True
    
    return False



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
            # If parse fails and time_str starts with "From ", retry on the remainder
            if time_str.lower().startswith("from "):
                try:
                    t = dateutil_parser.parse(time_str[5:]).time()
                    starts_at = starts_at.replace(hour=t.hour, minute=t.minute, second=0)
                    time_known = True
                except (ValueError, OverflowError):
                    pass

    price_text = extract_price(content)
    summary_text = _extract_summary(content, title)
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
