from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.db.models import (
    BoardPost, Event, EventRolePing, EventSignup, EventSource, GuildConfig,
    InterestRole, RoleKeyword,
)


def get_or_create_config(session: Session, guild_id: int) -> GuildConfig:
    cfg = session.get(GuildConfig, guild_id)
    if cfg is None:
        cfg = GuildConfig(guild_id=guild_id)
        session.add(cfg)
        session.commit()
    return cfg


def upsert_event(session: Session, guild_id: int,
                 data: dict) -> tuple[Event, bool, bool]:
    stmt = select(Event).where(
        Event.guild_id == guild_id, Event.external_id == data["external_id"]
    )
    ev = session.execute(stmt).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    created = ev is None
    if created:
        ev = Event(guild_id=guild_id, external_id=data["external_id"])
        session.add(ev)
    old_fingerprint = ev.fingerprint
    for key in (
        "source_url", "title", "url", "starts_at", "time_known",
        "price_text", "summary", "image_url", "fingerprint",
    ):
        if key in data:
            setattr(ev, key, data[key])
    ev.last_seen_at = now
    ev.missed_refreshes = 0
    ev.is_active = True
    changed = created or old_fingerprint != ev.fingerprint
    session.commit()
    return ev, created, changed


def get_active_events(session: Session, guild_id: int) -> list[Event]:
    stmt = select(Event).where(Event.guild_id == guild_id, Event.is_active.is_(True))
    return list(session.execute(stmt).scalars())


def mark_absent_events_stale(session: Session, guild_id: int, seen_external_ids: set[str]) -> int:
    actives = get_active_events(session, guild_id)
    deactivated = 0
    for ev in actives:
        if ev.external_id not in seen_external_ids:
            ev.missed_refreshes += 1
            if ev.missed_refreshes >= 2:
                ev.is_active = False
                deactivated += 1
    session.commit()
    return deactivated


def get_board_post(session: Session, guild_id: int, board_type: str) -> BoardPost | None:
    stmt = select(BoardPost).where(
        BoardPost.guild_id == guild_id, BoardPost.board_type == board_type
    )
    return session.execute(stmt).scalar_one_or_none()


def save_board_post(session, guild_id, board_type, period_key, channel_id,
                    message_id, render_hash, message_ids: str = "[]") -> BoardPost:
    post = get_board_post(session, guild_id, board_type)
    if post is None:
        post = BoardPost(guild_id=guild_id, board_type=board_type)
        session.add(post)
    post.period_key = period_key
    post.channel_id = channel_id
    post.message_id = message_id
    post.message_ids = message_ids
    post.render_hash = render_hash
    post.updated_at = datetime.now(timezone.utc)
    session.commit()
    return post


def add_interest_role(session: Session, guild_id: int, role_id: int, label: str, emoji: str | None = None) -> InterestRole:
    row = InterestRole(guild_id=guild_id, role_id=role_id, label=label, emoji=emoji)
    stmt = select(func.max(InterestRole.sort_order)).where(InterestRole.guild_id == guild_id)
    max_order = session.execute(stmt).scalar()
    max_order = -1 if max_order is None else max_order
    row.sort_order = max_order + 1
    session.add(row)
    session.commit()
    return row


def remove_interest_role(session: Session, guild_id: int, row_id: int) -> bool:
    row = session.get(InterestRole, row_id)
    if row is None or row.guild_id != guild_id:
        return False
    session.execute(delete(RoleKeyword).where(RoleKeyword.interest_role_id == row.id))
    session.execute(delete(EventRolePing).where(EventRolePing.interest_role_id == row.id))
    session.delete(row)
    session.commit()
    return True


def list_interest_roles(session: Session, guild_id: int) -> list[InterestRole]:
    stmt = select(InterestRole).where(InterestRole.guild_id == guild_id).order_by(InterestRole.sort_order)
    return list(session.execute(stmt).scalars())


def add_keyword(session: Session, interest_role_id: int, keyword: str) -> RoleKeyword:
    stripped = keyword.strip().lower()
    if not stripped:
        raise ValueError("Keyword cannot be empty or whitespace-only")
    row = RoleKeyword(interest_role_id=interest_role_id, keyword=stripped)
    session.add(row)
    session.commit()
    return row


def remove_keyword(session: Session, interest_role_id: int, keyword: str) -> bool:
    stmt = select(RoleKeyword).where(
        RoleKeyword.interest_role_id == interest_role_id,
        RoleKeyword.keyword == keyword.strip().lower())
    row = session.execute(stmt).scalar_one_or_none()
    if row is None:
        return False
    session.delete(row)
    session.commit()
    return True


def get_keywords_by_role(session: Session, guild_id: int) -> dict[int, list[str]]:
    roles = list_interest_roles(session, guild_id)
    out: dict[int, list[str]] = {r.id: [] for r in roles}
    if not roles:
        return out
    stmt = select(RoleKeyword).where(
        RoleKeyword.interest_role_id.in_([r.id for r in roles])).order_by(RoleKeyword.id)
    for k in session.execute(stmt).scalars():
        out[k.interest_role_id].append(k.keyword)
    return out


def has_ping(session: Session, event_id: int, interest_role_id: int) -> bool:
    stmt = select(EventRolePing).where(
        EventRolePing.event_id == event_id,
        EventRolePing.interest_role_id == interest_role_id)
    return session.execute(stmt).scalar_one_or_none() is not None


def record_ping(session: Session, guild_id: int, event_id: int, interest_role_id: int) -> None:
    if has_ping(session, event_id, interest_role_id):
        return
    session.add(EventRolePing(guild_id=guild_id, event_id=event_id,
                              interest_role_id=interest_role_id))
    try:
        session.commit()
    except IntegrityError:
        session.rollback()  # concurrent record of the same ping


def get_event_by_id(session: Session, guild_id: int, event_id: int) -> Event | None:
    """Get an event by its internal id and guild id."""
    stmt = select(Event).where(Event.guild_id == guild_id, Event.id == event_id)
    return session.execute(stmt).scalar_one_or_none()
