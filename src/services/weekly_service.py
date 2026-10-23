from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.db.models import WeeklyRecurring

# (title, days_text, time_text, ends_on)
_DEFAULT_ROWS: list[tuple[str, str, str, str | None]] = [
    ("DND Fremantle", "Mon & Thu", "6:00 PM", None),
    ("Tabletop Freo — TCGs, Wargames & Board Games", "Wed", "4:00 PM", None),
    ("MTG Casual Commander", "Sun", "11:00 AM", None),
    ("Warhammer: The Old World — Friday Night Battle March (600 pts)", "Fri", "5:00 PM", None),
    ("Warhammer: The Horus Heresy — Heresy Fight Night (casual)", "Fri", "5:00 PM", None),
    ("Mordheim: The Cursed City Campaign", "Tue", "6:00 PM", "2026-12-09"),
]


def seed_weekly_recurring(session: Session, guild_id: int) -> int:
    """Insert the default roster only when the guild has no rows; returns rows inserted."""
    existing = session.execute(select(func.count()).select_from(WeeklyRecurring).where(
        WeeklyRecurring.guild_id == guild_id)).scalar_one()
    if existing:
        return 0
    for order, (title, days, time_text, ends_on) in enumerate(_DEFAULT_ROWS):
        session.add(WeeklyRecurring(guild_id=guild_id, title=title, days_text=days,
                                    time_text=time_text, sort_order=order, enabled=True,
                                    ends_on=ends_on))
    session.commit()
    return len(_DEFAULT_ROWS)


def get_weekly_rows(session: Session, guild_id: int) -> list[WeeklyRecurring]:
    stmt = select(WeeklyRecurring).where(WeeklyRecurring.guild_id == guild_id).order_by(
        WeeklyRecurring.sort_order, WeeklyRecurring.id)
    return list(session.execute(stmt).scalars())
