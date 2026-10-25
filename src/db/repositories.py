from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.db.models import (
    BoardPost, Decision, DecisionChannel, DecisionConfig, Event, EventRolePing,
    EventSignup, EventSource, GuildConfig, InterestRole, ProductAlert, RoleKeyword,
    TrelloMemberMap, TrelloSync,
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


def get_alerted_product_ids(session: Session, guild_id: int) -> set[str]:
    stmt = select(ProductAlert.external_id).where(ProductAlert.guild_id == guild_id)
    return set(session.execute(stmt).scalars())


def record_product_alerts(session: Session, guild_id: int, external_ids) -> None:
    """Idempotently record alerted products for a guild in one commit."""
    new = set(external_ids) - get_alerted_product_ids(session, guild_id)
    if not new:
        return
    session.add_all(ProductAlert(guild_id=guild_id, external_id=x) for x in sorted(new))
    try:
        session.commit()
    except IntegrityError:
        session.rollback()  # concurrent record of the same alerts


def get_event_by_id(session: Session, guild_id: int, event_id: int) -> Event | None:
    """Get an event by its internal id and guild id."""
    stmt = select(Event).where(Event.guild_id == guild_id, Event.id == event_id)
    return session.execute(stmt).scalar_one_or_none()


# --- decision tracking bot ---


def get_or_create_decision_config(session: Session, guild_id: int) -> DecisionConfig:
    cfg = session.get(DecisionConfig, guild_id)
    if cfg is None:
        cfg = DecisionConfig(guild_id=guild_id)
        session.add(cfg)
        session.commit()
    return cfg


def add_decision(session: Session, guild_id: int, channel_id: int, message_id: int,
                 user_id: int, content: str, summary: str) -> Decision:
    d = Decision(guild_id=guild_id, channel_id=channel_id, message_id=message_id,
                 user_id=user_id, content=content, summary=summary, status="pending")
    session.add(d)
    session.commit()
    return d


def get_decision(session: Session, guild_id: int, decision_id: int) -> Decision | None:
    stmt = select(Decision).where(Decision.guild_id == guild_id, Decision.id == decision_id)
    return session.execute(stmt).scalar_one_or_none()


def set_decision_status(session: Session, guild_id: int, decision_id: int, status: str,
                        confirmed_by: int | None = None) -> Decision | None:
    d = get_decision(session, guild_id, decision_id)
    if d is None:
        return None
    d.status = status
    if status == "confirmed":
        d.confirmed_by = confirmed_by
        d.confirmed_at = datetime.now(timezone.utc)
    session.commit()
    return d


def list_decisions(session: Session, guild_id: int, status: str | None = None) -> list[Decision]:
    stmt = select(Decision).where(Decision.guild_id == guild_id)
    if status is not None:
        stmt = stmt.where(Decision.status == status)
    stmt = stmt.order_by(Decision.created_at.desc(), Decision.id.desc())
    return list(session.execute(stmt).scalars())


def get_confirmed_unsynced_decisions(session: Session, guild_id: int) -> list[Decision]:
    """Confirmed decisions with no trello_sync row yet."""
    synced = select(TrelloSync.decision_id)
    stmt = select(Decision).where(
        Decision.guild_id == guild_id, Decision.status == "confirmed",
        Decision.id.not_in(synced))
    return list(session.execute(stmt).scalars())


def record_trello_sync(session: Session, decision_id: int, card_id: str,
                       card_url: str | None) -> None:
    try:
        session.add(TrelloSync(decision_id=decision_id, trello_card_id=card_id,
                               trello_card_url=card_url))
        session.commit()
    except IntegrityError:
        session.rollback()  # already synced (concurrent sync)


def get_trello_sync(session: Session, decision_id: int) -> TrelloSync | None:
    return session.get(TrelloSync, decision_id) or session.execute(
        select(TrelloSync).where(TrelloSync.decision_id == decision_id)).scalar_one_or_none()


def add_decision_channel(session: Session, guild_id: int, channel_id: int) -> None:
    try:
        session.add(DecisionChannel(guild_id=guild_id, channel_id=channel_id))
        session.commit()
    except IntegrityError:
        session.rollback()  # already monitored


def remove_decision_channel(session: Session, guild_id: int, channel_id: int) -> bool:
    stmt = delete(DecisionChannel).where(
        DecisionChannel.guild_id == guild_id, DecisionChannel.channel_id == channel_id)
    result = session.execute(stmt)
    session.commit()
    return result.rowcount > 0


def list_decision_channels(session: Session, guild_id: int) -> list[int]:
    stmt = select(DecisionChannel.channel_id).where(DecisionChannel.guild_id == guild_id)
    return [r[0] for r in session.execute(stmt)]


def set_trello_member(session: Session, guild_id: int, discord_user_id: int,
                      trello_member_id: str) -> None:
    stmt = select(TrelloMemberMap).where(
        TrelloMemberMap.guild_id == guild_id,
        TrelloMemberMap.discord_user_id == discord_user_id)
    row = session.execute(stmt).scalar_one_or_none()
    if row is None:
        session.add(TrelloMemberMap(guild_id=guild_id, discord_user_id=discord_user_id,
                                    trello_member_id=trello_member_id))
    else:
        row.trello_member_id = trello_member_id
    session.commit()


def get_trello_member(session: Session, guild_id: int, discord_user_id: int) -> str | None:
    stmt = select(TrelloMemberMap.trello_member_id).where(
        TrelloMemberMap.guild_id == guild_id,
        TrelloMemberMap.discord_user_id == discord_user_id)
    return session.execute(stmt).scalar_one_or_none()


def remove_trello_member(session: Session, guild_id: int, discord_user_id: int) -> bool:
    stmt = delete(TrelloMemberMap).where(
        TrelloMemberMap.guild_id == guild_id,
        TrelloMemberMap.discord_user_id == discord_user_id)
    result = session.execute(stmt)
    session.commit()
    return result.rowcount > 0
