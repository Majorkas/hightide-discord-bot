import re

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from src.db.models import GuildConfig, WeeklyRecurring
from src.db.repositories import get_or_create_config

# (title, days_text, time_text, ends_on)
_DEFAULT_ROWS: list[tuple[str, str, str, str | None]] = [
    ("DND Fremantle", "Mon & Thu", "6:00 PM", None),
    ("Mordheim: The Cursed City Campaign", "Tue", "6:00 PM", "2026-12-09"),
    ("Tabletop Freo — TCGs, Wargames & Board Games", "Wed", "4:00 PM", None),
    ("Warhammer: The Old World — Friday Night Battle March (600 pts)", "Fri", "5:00 PM", None),
    ("Warhammer: The Horus Heresy — Heresy Fight Night (casual)", "Fri", "5:00 PM", None),
    ("MTG Casual Commander", "Sun", "11:00 AM", None),
]

_DAY_NAMES = {
    "mon": 0, "monday": 0,
    "tue": 1, "tuesday": 1,
    "wed": 2, "wednesday": 2,
    "thu": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}


def _extract_weekday_index(days_text: str) -> int:
    """Extract the earliest weekday index from days_text (0=Mon, 6=Sun). Returns 7 if no day found."""
    words = re.findall(r"\b(\w+)\b", days_text.lower())
    indices = [_DAY_NAMES[w] for w in words if w in _DAY_NAMES]
    return min(indices) if indices else 7


def seed_weekly_recurring(session: Session, guild_id: int) -> int:
    """Insert the default roster once per guild (guarded by weekly_seeded); returns rows inserted."""
    get_or_create_config(session, guild_id)
    # Atomic claim: only one concurrent caller flips the flag and gets to insert.
    claimed = session.execute(update(GuildConfig).where(
        GuildConfig.guild_id == guild_id, GuildConfig.weekly_seeded.is_(False),
    ).values(weekly_seeded=True).execution_options(synchronize_session=False)).rowcount
    if not claimed:
        return 0
    existing = session.execute(select(func.count()).select_from(WeeklyRecurring).where(
        WeeklyRecurring.guild_id == guild_id)).scalar_one()
    if existing:  # pre-marker guild that already has a roster: just mark it seeded
        session.commit()
        session.refresh(session.get(GuildConfig, guild_id))
        return 0
    for order, (title, days, time_text, ends_on) in enumerate(_DEFAULT_ROWS):
        session.add(WeeklyRecurring(guild_id=guild_id, title=title, days_text=days,
                                    time_text=time_text, sort_order=order, enabled=True,
                                    ends_on=ends_on))
    session.commit()
    session.refresh(session.get(GuildConfig, guild_id))
    return len(_DEFAULT_ROWS)


def get_weekly_rows(session: Session, guild_id: int) -> list[WeeklyRecurring]:
    stmt = select(WeeklyRecurring).where(WeeklyRecurring.guild_id == guild_id).order_by(
        WeeklyRecurring.sort_order, WeeklyRecurring.id)
    return list(session.execute(stmt).scalars())
