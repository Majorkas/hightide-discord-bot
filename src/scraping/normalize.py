import hashlib
import html as html_lib
import re
from datetime import datetime

from bs4 import BeautifulSoup
from dateutil import parser as dateutil_parser

_DOT_PREFIX = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{2}|\d{4})\b")
_NATURAL_DATE = re.compile(
    r"((?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?"
    r"(January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}",
    re.IGNORECASE,
)
_TIME_RE = re.compile(r"→\s*Time:\s*(.+?)\s*(?:<|$)", re.IGNORECASE)
_TICKET_RE = re.compile(r"→\s*Ticket:\s*(.+?)\s*(?:<|$)", re.IGNORECASE)


def clean_title(raw: str) -> str:
    return html_lib.unescape(raw).strip()


def strip_html(raw: str) -> str:
    return BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)


def parse_event_datetime(title: str, tz) -> tuple[datetime | None, bool]:
    m = _DOT_PREFIX.search(title)
    if m:
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if year < 100:
            year += 2000
        return datetime(year, month, day, tzinfo=tz), False
    m = _NATURAL_DATE.search(title)
    if m:
        try:
            dt = dateutil_parser.parse(m.group(0))
            return dt.replace(tzinfo=tz), False
        except (ValueError, OverflowError):
            return None, False
    return None, False


def extract_time(content_html: str) -> str | None:
    m = _TIME_RE.search(content_html)
    return m.group(1).strip() if m else None


def extract_price(content_html: str) -> str | None:
    m = _TICKET_RE.search(content_html)
    return m.group(1).strip() if m else None


def fingerprint(*parts: str | None) -> str:
    joined = "|".join(p or "" for p in parts)
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()
