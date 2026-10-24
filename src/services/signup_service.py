"""Signup service for event signups."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from src import config
from src.db.models import EventSignup
from src.db.session import get_session_factory

_session_factory = None


def _session() -> Session:
    global _session_factory
    if _session_factory is None:
        _session_factory = get_session_factory(config.DATABASE_URL)
    return _session_factory()


def toggle_signup(session: Session, guild_id: int, event_id: int, user_id: int) -> tuple[bool, int]:
    """Toggle a user signup for an event.
    
    Returns:
        (added, new_count): added=True if signup was added, False if removed.
                            new_count is the total signup count for the event after toggle.
    """
    # Check if signup already exists
    stmt = select(EventSignup).where(
        EventSignup.event_id == event_id,
        EventSignup.user_id == user_id
    )
    existing = session.execute(stmt).scalar_one_or_none()
    
    if existing is not None:
        # Remove the signup
        session.delete(existing)
        session.commit()
        added = False
    else:
        # Add the signup
        signup = EventSignup(guild_id=guild_id, event_id=event_id, user_id=user_id)
        session.add(signup)
        session.commit()
        added = True
    
    # Get new count
    new_count = count_signups(session, event_id)
    return added, new_count


def count_signups(session: Session, event_id: int) -> int:
    """Count total signups for an event."""
    stmt = select(EventSignup).where(EventSignup.event_id == event_id)
    return len(list(session.execute(stmt).scalars()))


def list_signups(session: Session, guild_id: int, event_id: int) -> list[EventSignup]:
    """List all signups for an event in a guild."""
    stmt = select(EventSignup).where(
        EventSignup.guild_id == guild_id,
        EventSignup.event_id == event_id
    )
    return list(session.execute(stmt).scalars())
