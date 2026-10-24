"""Signup service for event signups."""
import logging

import discord
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import ObjectDeletedError, StaleDataError

from src import config
from src.db.models import EventSignup
from src.db.repositories import get_event_by_id
from src.db.session import get_session_factory

log = logging.getLogger(__name__)
_session_factory = None


def _session() -> Session:
    global _session_factory
    if _session_factory is None:
        _session_factory = get_session_factory(config.DATABASE_URL)
    return _session_factory()


def toggle_signup(session: Session, guild_id: int, event_id: int, user_id: int) -> tuple[bool, int]:
    """Toggle a user signup for an event.

    WARNING: Callers MUST check event existence (via get_event_by_id) and is_active=True
    BEFORE calling this function. This function does NOT validate the event.

    Race condition handling: If two concurrent sessions both attempt to add the same
    (event_id, user_id) pair, the first commit wins. The second will catch IntegrityError,
    rollback, and recount the existing row as "already added" (returns True semantics).

    Returns:
        (added, new_count): added=True if signup was added or already existed as concurrent
                            winner. False if removed.
                            new_count is the total signup count for the event after toggle.
    """
    # Check if signup already exists
    stmt = select(EventSignup).where(
        EventSignup.event_id == event_id,
        EventSignup.user_id == user_id
    )
    existing = session.execute(stmt).scalar_one_or_none()

    if existing is not None:
        # Remove the signup; a concurrent remove already did the job if the row is gone
        try:
            session.delete(existing)
            session.commit()
        except (StaleDataError, ObjectDeletedError):
            session.rollback()
        added = False
    else:
        # Add the signup; tolerate race condition from concurrent double-add
        try:
            signup = EventSignup(guild_id=guild_id, event_id=event_id, user_id=user_id)
            session.add(signup)
            session.commit()
            added = True
        except IntegrityError:
            session.rollback()
            added = True  # Row exists from concurrent add; treat as success

    # Get new count
    new_count = count_signups(session, event_id)
    return added, new_count


def count_signups(session: Session, event_id: int) -> int:
    """Count total signups for an event."""
    stmt = select(func.count()).select_from(EventSignup).where(
        EventSignup.event_id == event_id
    )
    return session.execute(stmt).scalar() or 0


def list_signups(session: Session, guild_id: int, event_id: int) -> list[EventSignup]:
    """List all signups for an event in a guild."""
    stmt = select(EventSignup).where(
        EventSignup.guild_id == guild_id,
        EventSignup.event_id == event_id
    )
    return list(session.execute(stmt).scalars())


async def _notify_ephemeral(interaction: discord.Interaction, text: str) -> None:
    """Best-effort ephemeral note; never raises."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except (discord.NotFound, discord.HTTPException):
        log.warning("could not send signup note to user", exc_info=True)


async def handle_signup_toggle(interaction: discord.Interaction, event_id: int) -> None:
    from src.discord_ui.role_views import SignupView
    try:
        with _session() as session:
            event = get_event_by_id(session, interaction.guild_id, event_id)
            if event is None or not event.is_active:
                await interaction.response.send_message("This event has ended.", ephemeral=True)
                return
            added, count = toggle_signup(
                session, interaction.guild_id, event_id, interaction.user.id)
    except SQLAlchemyError:
        log.exception("signup toggle failed for event %s", event_id)
        await _notify_ephemeral(
            interaction, "Couldn't save your signup right now. Please try again.")
        return
    try:
        await interaction.response.edit_message(view=SignupView(event_id, count))
    except (discord.NotFound, discord.HTTPException):
        log.warning("signup recorded but card edit failed for event %s", event_id, exc_info=True)
        await _notify_ephemeral(
            interaction,
            "Your signup was recorded; the count will catch up on the next board refresh.")
        return
    try:
        await interaction.followup.send(
            "You're on the list!" if added else "Removed you from the list.", ephemeral=True)
    except (discord.NotFound, discord.HTTPException):
        log.warning("signup confirmation followup failed for event %s", event_id, exc_info=True)
