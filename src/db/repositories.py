from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.db.models import BoardPost, Event, EventSource, GuildConfig


def get_or_create_config(session: Session, guild_id: int) -> GuildConfig:
    cfg = session.get(GuildConfig, guild_id)
    if cfg is None:
        cfg = GuildConfig(guild_id=guild_id)
        session.add(cfg)
        session.commit()
    return cfg


def upsert_event(session: Session, guild_id: int, data: dict) -> tuple[Event, bool]:
    stmt = select(Event).where(
        Event.guild_id == guild_id, Event.external_id == data["external_id"]
    )
    ev = session.execute(stmt).scalar_one_or_none()
    now = datetime.now(timezone.utc)
    created = ev is None
    if created:
        ev = Event(guild_id=guild_id, external_id=data["external_id"])
        session.add(ev)
    for key in (
        "source_url", "title", "url", "starts_at", "time_known",
        "price_text", "summary", "image_url", "fingerprint",
    ):
        if key in data:
            setattr(ev, key, data[key])
    ev.last_seen_at = now
    ev.missed_refreshes = 0
    ev.is_active = True
    session.commit()
    return ev, created


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
                    message_id, render_hash) -> BoardPost:
    post = get_board_post(session, guild_id, board_type)
    if post is None:
        post = BoardPost(guild_id=guild_id, board_type=board_type)
        session.add(post)
    post.period_key = period_key
    post.channel_id = channel_id
    post.message_id = message_id
    post.render_hash = render_hash
    post.updated_at = datetime.now(timezone.utc)
    session.commit()
    return post
