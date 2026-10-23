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
    r"November|December)\s+\d{1,2}(?!\d)(?:st|nd|rd|th)?(?:,?\s+(?P<year>\d{4}))?",
    re.IGNORECASE,
)
_ROLL_FORWARD_DAYS = 30
_JUNK_TITLE_TERMS = ("test", "placeholder", "painters guild")
_TIME_RE = re.compile(r"→\s*Time:\s*(.+?)\s*(?:<|$)", re.IGNORECASE)
_TICKET_RE = re.compile(r"→\s*Ticket:\s*(.+?)\s*(?:<|$)", re.IGNORECASE)
_RANGE_RE = re.compile(r"^(.+?)\s+(?:to|[-–—])\s+(.+)$", re.IGNORECASE)
_PROSE_CLOCK = r"(\d{1,2}(?::\d{2}\s*(?:[ap]\.?m\b\.?)?|\s*[ap]\.?m\b\.?))(?![\d/:])"
_PROSE_START_RE = re.compile(
    r"\b(?:kick(?:s|ing)?\s+off|start(?:s|ing)?)\s+at\s+" + _PROSE_CLOCK
    + r"|\bfrom\s+" + _PROSE_CLOCK,
    re.IGNORECASE,
)


def clean_title(raw: str) -> str:
    return html_lib.unescape(raw).strip()


def is_junk_title(title: str) -> bool:
    lowered = title.lower()
    return any(term in lowered for term in _JUNK_TITLE_TERMS)


def _now(tz) -> datetime:
    return datetime.now(tz)


def strip_html(raw: str) -> str:
    return BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)


def parse_event_datetime(
    title: str, tz, now: datetime | None = None,
) -> tuple[datetime | None, bool]:
    m = _DOT_PREFIX.search(title)
    if m:
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if year < 100:
            year += 2000
        try:
            return datetime(year, month, day, tzinfo=tz), False
        except (ValueError, OverflowError):
            return None, False
    m = _NATURAL_DATE.search(title)
    if m:
        try:
            if m.group("year"):
                return dateutil_parser.parse(m.group(0)).replace(tzinfo=tz), False
            now = now or _now(tz)
            dt = dateutil_parser.parse(m.group(0), default=datetime(now.year, 1, 1))
            dt = dt.replace(tzinfo=tz)
            # Yearless dates well in the past mean next year (e.g. "January 10th" seen in December).
            if (now.date() - dt.date()).days > _ROLL_FORWARD_DAYS:
                dt = dt.replace(year=dt.year + 1)
            return dt, False
        except (ValueError, OverflowError):
            return None, False
    return None, False


def extract_time(content_html: str) -> str | None:
    m = _TIME_RE.search(content_html)
    return m.group(1).strip() if m else None


def split_time_range(time_str: str) -> tuple[str, str] | None:
    m = _RANGE_RE.match(time_str.strip())
    return (m.group(1), m.group(2)) if m else None


def extract_prose_start_time(content_html: str) -> str | None:
    m = _PROSE_START_RE.search(strip_html(content_html))
    return (m.group(1) or m.group(2)) if m else None


def extract_price(content_html: str) -> str | None:
    m = _TICKET_RE.search(content_html)
    return m.group(1).strip() if m else None


def fingerprint(*parts: str | None) -> str:
    joined = "|".join(p or "" for p in parts)
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()
