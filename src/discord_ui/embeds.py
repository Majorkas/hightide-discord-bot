import re
from collections import defaultdict
from datetime import date, datetime
from urllib.parse import urlparse

import discord

from src.db.models import Event

# Discord embed limits
MAX_EMBED_FIELDS = 25
MAX_FIELD_VALUE_CHARS = 1024
MAX_EMBED_CHARS = 6000

_MD_EXTRA = re.compile(r"([\[\]()<>])")
_URL_UNSAFE = re.compile(r"[()<>\s\x00-\x1f\x7f]")


def _md_escape(s: str) -> str:
    """Neutralise markdown, links and mentions in scraped text."""
    s = re.sub(r"\s*[\r\n]+\s*", " ", s)
    s = discord.utils.escape_markdown(s)
    s = _MD_EXTRA.sub(r"\\\1", s)
    return discord.utils.escape_mentions(s)


def _safe_url(url: str | None) -> str | None:
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return _URL_UNSAFE.sub(lambda m: f"%{ord(m.group(0)):02X}", url)


def _link(title: str, url: str | None) -> str:
    text = _md_escape(title)
    safe = _safe_url(url)
    return f"[{text}]({safe})" if safe else text


def _event_line(e: Event, tz, with_day: bool = False) -> str:
    local = e.starts_at.astimezone(tz)
    time_part = local.strftime("%H:%M") if e.time_known else "time TBC"
    day_part = f"{local.strftime('%a %d %b')} " if with_day else ""
    price_part = f" · {_md_escape(e.price_text)}" if e.price_text else ""
    return f"{day_part}`{time_part}` {_link(e.title, e.url)}{price_part}"


def _truncate_lines(lines: list[str], max_chars: int) -> tuple[str, int]:
    """Join lines within max_chars, appending '… +N more'; return (text, dropped)."""
    if len("\n".join(lines)) <= max_chars:
        return "\n".join(lines), 0
    reserve = len(f"\n… +{len(lines)} more")
    budget = max_chars - reserve
    kept: list[str] = []
    used = 0
    for line in lines:
        needed = len(line) + (1 if kept else 0)
        if used + needed > budget:
            break
        kept.append(line)
        used += needed
    dropped = len(lines) - len(kept)
    if not kept:
        return f"… +{dropped} more", dropped
    return "\n".join(kept) + f"\n… +{dropped} more", dropped


def _count_embed_chars(embed: discord.Embed) -> int:
    """Count total characters in embed (title, description, fields, footer)."""
    count = len(embed.title or "")
    count += len(embed.description or "")
    for field in embed.fields:
        count += len(field.name) + len(field.value)
    count += len(embed.footer.text or "")
    return count


def build_board_embed(events: list[Event], title: str, tz,
                      include_tba: bool) -> discord.Embed:
    embed = discord.Embed(title=title, color=0x1F8B4C)
    dated = [e for e in events if e.starts_at is not None]
    tba = sorted((e for e in events if e.starts_at is None), key=lambda e: e.title)

    by_day: dict[date, list[Event]] = defaultdict(list)
    for e in dated:
        by_day[e.starts_at.astimezone(tz).date()].append(e)

    will_render_tba = include_tba and len(tba) > 0
    if not dated and not will_render_tba:
        embed.description = "No events found for this period."

    # (name, value, event_count) — built off-embed so the 6000 guard can trim cleanly.
    fields: list[tuple[str, str, int]] = []
    later_lines: list[str] = []
    # Reserve slots: Later (1) and TBA (1 when rendered) within the 25-field cap.
    max_day_fields = 23 if will_render_tba else 24

    for day in sorted(by_day):
        day_events = sorted(by_day[day], key=lambda x: (x.starts_at, x.title))
        if len(fields) >= max_day_fields:
            later_lines.extend(_event_line(e, tz, with_day=True) for e in day_events)
            continue
        lines = [_event_line(e, tz) for e in day_events]
        value, _ = _truncate_lines(lines, MAX_FIELD_VALUE_CHARS)
        fields.append((day.strftime("%a %d %b %Y"), value, len(lines)))

    if later_lines:
        value, _ = _truncate_lines(later_lines, MAX_FIELD_VALUE_CHARS)
        fields.append(("Later", value, len(later_lines)))

    if will_render_tba:
        tba_lines = [_link(e.title, e.url) for e in tba]
        value, _ = _truncate_lines(tba_lines, MAX_FIELD_VALUE_CHARS)
        fields.append(("Date TBA", value, len(tba_lines)))

    footer = f"Last updated {datetime.now(tz).strftime('%H:%M')} AWST"
    total = (len(embed.title or "") + len(embed.description or "") + len(footer)
             + sum(len(n) + len(v) for n, v, _ in fields))

    if total > MAX_EMBED_CHARS:
        total_events = sum(c for _, _, c in fields)
        # Worst-case indicator size: N can't exceed the total event count.
        reserve = len("…") + len(f"… +{total_events} more events")
        removed = 0
        while fields and (total + reserve > MAX_EMBED_CHARS
                          or len(fields) >= MAX_EMBED_FIELDS):
            name, value, count = fields.pop()
            total -= len(name) + len(value)
            removed += count
        fields.append(("…", f"… +{removed} more events", removed))

    for name, value, _ in fields:
        embed.add_field(name=name, value=value, inline=False)
    embed.set_footer(text=footer)
    return embed
