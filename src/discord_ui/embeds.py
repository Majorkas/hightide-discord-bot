import re
from collections import defaultdict
from datetime import date, datetime
from urllib.parse import urlparse

import discord

from src.db.models import Event, WeeklyRecurring

# Discord embed limits
MAX_EMBED_FIELDS = 25
MAX_FIELD_VALUE_CHARS = 1024
MAX_EMBED_CHARS = 6000

_MD_EXTRA = re.compile(r"([\[\]()<>])")
_BIDI = re.compile("[\u202a-\u202e\u2066-\u2069]")
_URL_UNSAFE = re.compile(r"[()<>\s\x00-\x1f\x7f]")


def _md_escape(s: str) -> str:
    """Neutralise markdown, links and mentions in scraped text."""
    s = _BIDI.sub("", s)
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


MAX_EVENT_CARDS = 7
_COLOR = 0x1F8B4C
MAX_SUMMARY_CHARS = 1000
MAX_TITLE_CHARS = 256
_COMPACT_BUDGET = 3500  # each board message is its own Discord message (6000-char cap)


def _card_description(e: Event, tz) -> str:
    local = e.starts_at.astimezone(tz)
    time_part = local.strftime("%H:%M") if e.time_known else "time TBC"
    info = f"{local.strftime('%a %d %b')} · {time_part}"
    if e.price_text:
        info += f" · {_md_escape(e.price_text)}"
    summary = e.summary or ""
    if len(summary) > MAX_SUMMARY_CHARS:
        summary = summary[:MAX_SUMMARY_CHARS].rstrip() + "…"
    return f"{info}\n{_md_escape(summary)}" if summary else info


def build_event_card(e: Event, tz) -> discord.Embed:
    """Rich card for one dated event (images only render on single-embed messages)."""
    card = discord.Embed(title=e.title[:MAX_TITLE_CHARS], url=_safe_url(e.url),
                         description=_card_description(e, tz), color=_COLOR)
    image = _safe_url(e.image_url)
    if image:
        card.set_image(url=image)
    return card


def _compact_embed(title: str, lines: list[str]) -> discord.Embed:
    text, _ = _truncate_lines(lines, _COMPACT_BUDGET)
    return discord.Embed(title=title, description=text, color=_COLOR)


def _parse_iso_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _weekly_line(row: WeeklyRecurring) -> str:
    line = f"{_md_escape(row.days_text)} · {_md_escape(row.time_text)} — {_md_escape(row.title)}"
    ends = _parse_iso_date(row.ends_on)
    return f"{line} (until {ends.strftime('%b %d')})" if ends else line


def current_weekly_rows(rows, tz, now: datetime | None = None) -> list[WeeklyRecurring]:
    """Enabled rows whose ends_on (if any) hasn't passed in the guild timezone, in display order."""
    today = (now or datetime.now(tz)).astimezone(tz).date()
    keep = []
    for r in rows:
        ends = _parse_iso_date(r.ends_on)
        if r.enabled and (ends is None or ends >= today):
            keep.append(r)
    return sorted(keep, key=lambda r: (r.sort_order or 0, r.id or 0))


def build_weekly_embed(rows, image_url: str | None, tz) -> discord.Embed:
    subtitle = "Recurring door-ticket-only events"
    lines = [_weekly_line(r) for r in rows]
    description = subtitle
    if lines:
        text, _ = _truncate_lines(lines, _COMPACT_BUDGET - len(subtitle) - 1)
        description = f"{subtitle}\n{text}"
    embed = discord.Embed(title="Every week at Hightide", description=description, color=_COLOR)
    image = _safe_url(image_url)
    if image:
        embed.set_image(url=image)
    return embed


def build_week_board_items(events: list[Event], title: str, tz,
                           include_tba: bool, weekly_rows=None, weekly_image_url: str | None = None,
                           now: datetime | None = None) -> list[tuple[str, discord.Embed]]:
    """(key, embed) per board message: header, up to 7 event cards, overflow, weekly, TBA.

    Event cards are keyed by external_id so the board can be diffed message by message.
    """
    dated = sorted((e for e in events if e.starts_at is not None),
                   key=lambda e: (e.starts_at, e.title))
    tba = sorted((e for e in events if e.starts_at is None), key=lambda e: e.title)
    show_tba = include_tba and bool(tba)

    header = discord.Embed(title=title, color=_COLOR)
    if dated:
        skim = [f"{e.starts_at.astimezone(tz).strftime('%a %d')} — {_link(e.title, e.url)}"
                for e in dated]
        header.description, _ = _truncate_lines(skim, _COMPACT_BUDGET)
    elif show_tba:
        header.description = "No dated events this period."
    else:
        header.description = "No events found for this period."
    header.set_footer(text=f"Last updated {datetime.now(tz).strftime('%H:%M')} AWST")
    items: list[tuple[str, discord.Embed]] = [("header", header)]

    items.extend((e.external_id, build_event_card(e, tz)) for e in dated[:MAX_EVENT_CARDS])

    overflow = dated[MAX_EVENT_CARDS:]
    if overflow:
        by_day: dict[date, list[Event]] = defaultdict(list)
        for e in overflow:
            by_day[e.starts_at.astimezone(tz).date()].append(e)
        lines: list[str] = []
        for day in sorted(by_day):
            lines.append(f"**{day.strftime('%a %d %b %Y')}**")
            lines.extend(_event_line(e, tz) for e in by_day[day])
        items.append(("overflow", _compact_embed("Also this week", lines)))

    current = current_weekly_rows(weekly_rows or [], tz, now)
    if current or _safe_url(weekly_image_url):
        items.append(("weekly", build_weekly_embed(current, weekly_image_url, tz)))

    if show_tba:
        items.append(("tba", _compact_embed("Date TBA", [_link(e.title, e.url) for e in tba])))
    return items


def build_week_board_payload(events: list[Event], title: str, tz,
                             include_tba: bool) -> list[discord.Embed]:
    """Header embed first, then one embed per card/overflow/TBA message (at most 10)."""
    return [embed for _, embed in build_week_board_items(events, title, tz, include_tba)]
