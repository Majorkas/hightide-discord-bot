from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger, Boolean, DateTime, Integer, String, Text, TypeDecorator, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from src import config


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TZDateTime(TypeDecorator):
    """Timezone-aware DateTime that ensures UTC storage and retrieval from SQLite."""
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(f"DateTime {value} must be timezone-aware")
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        # SQLite drops tzinfo; everything is stored as UTC.
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class GuildConfig(Base):
    __tablename__ = "guild_config"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    timezone: Mapped[str] = mapped_column(String, default=lambda: config.DEFAULT_TIMEZONE)
    this_week_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    this_month_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    daily_refresh_time: Mapped[str] = mapped_column(String, default="09:00")
    week_start_mode: Mapped[str] = mapped_column(String, default="monday")
    include_tba_section: Mapped[bool] = mapped_column(Boolean, default=True)
    delete_previous_month_message: Mapped[bool] = mapped_column(Boolean, default=True)
    weekly_image_url: Mapped[str | None] = mapped_column(String, nullable=True)
    weekly_seeded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    roles_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    roles_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    announce_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    daily_post_time: Mapped[str] = mapped_column(String, default="09:00", server_default="09:00")
    # Local (guild tz) YYYY-MM-DD of the last tonight post.
    last_daily_post_date: Mapped[str | None] = mapped_column(String, nullable=True)
    wants_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    new_stock_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Comma-separated search terms matched against product titles for new-product alerts.
    new_stock_terms: Mapped[str] = mapped_column(
        String, default="preorder,pre-order", server_default="preorder,pre-order")


class WeeklyRecurring(Base):
    __tablename__ = "weekly_recurring"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    title: Mapped[str] = mapped_column(String)
    days_text: Mapped[str] = mapped_column(String)  # display text, e.g. "Mon & Thu"
    time_text: Mapped[str] = mapped_column(String)  # display text, e.g. "6:00 PM"
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # ISO date (YYYY-MM-DD); the row stops showing once this date has passed.
    ends_on: Mapped[str | None] = mapped_column(String, nullable=True)


class EventSource(Base):
    __tablename__ = "event_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    url: Mapped[str] = mapped_column(String)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Event(Base):
    __tablename__ = "events"
    # AUTOINCREMENT stops rowid reuse from attaching stale signups/buttons to a different event.
    __table_args__ = (UniqueConstraint("guild_id", "external_id"), {"sqlite_autoincrement": True})

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    external_id: Mapped[str] = mapped_column(String)
    source_url: Mapped[str | None] = mapped_column(String, nullable=True)
    title: Mapped[str] = mapped_column(String)
    url: Mapped[str] = mapped_column(String)
    starts_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    time_known: Mapped[bool] = mapped_column(Boolean, default=False)
    price_text: Mapped[str | None] = mapped_column(String, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_url: Mapped[str | None] = mapped_column(String, nullable=True)
    fingerprint: Mapped[str | None] = mapped_column(String, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(TZDateTime(), default=_utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(TZDateTime(), default=_utcnow)
    missed_refreshes: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class BoardPost(Base):
    __tablename__ = "board_posts"
    __table_args__ = (UniqueConstraint("guild_id", "board_type"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    board_type: Mapped[str] = mapped_column(String)  # this_week | this_month
    period_key: Mapped[str] = mapped_column(String)  # e.g. 2026-W43 | 2026-10
    channel_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(BigInteger)  # header message for this_week
    # JSON list of the card message ids that follow the header (this_week only).
    message_ids: Mapped[str] = mapped_column(Text, default="[]", server_default="[]")
    render_hash: Mapped[str] = mapped_column(String, default="")
    updated_at: Mapped[datetime] = mapped_column(TZDateTime(), default=_utcnow)


class InterestRole(Base):
    __tablename__ = "interest_roles"
    # AUTOINCREMENT stops rowid reuse from remapping stale panel buttons to new roles.
    __table_args__ = (UniqueConstraint("guild_id", "role_id"), {"sqlite_autoincrement": True})

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    role_id: Mapped[int] = mapped_column(BigInteger)
    label: Mapped[str] = mapped_column(String)
    emoji: Mapped[str | None] = mapped_column(String, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class RoleKeyword(Base):
    __tablename__ = "role_keywords"
    __table_args__ = (UniqueConstraint("interest_role_id", "keyword"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    interest_role_id: Mapped[int] = mapped_column(Integer)
    keyword: Mapped[str] = mapped_column(String)


class EventRolePing(Base):
    __tablename__ = "event_role_pings"
    __table_args__ = (UniqueConstraint("event_id", "interest_role_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    event_id: Mapped[int] = mapped_column(Integer)
    interest_role_id: Mapped[int] = mapped_column(Integer)
    pinged_at: Mapped[datetime] = mapped_column(TZDateTime(), default=_utcnow)


class EventSignup(Base):
    __tablename__ = "event_signups"
    # AUTOINCREMENT stops rowid reuse from remapping stale signup buttons to new events.
    __table_args__ = (UniqueConstraint("event_id", "user_id"), {"sqlite_autoincrement": True})

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    event_id: Mapped[int] = mapped_column(Integer)
    user_id: Mapped[int] = mapped_column(BigInteger)
    signed_up_at: Mapped[datetime] = mapped_column(TZDateTime(), default=_utcnow)


class CardWant(Base):
    __tablename__ = "card_wants"
    # AUTOINCREMENT stops rowid reuse from letting /want remove <id> hit someone else's want.
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    user_id: Mapped[int] = mapped_column(BigInteger)
    card_name: Mapped[str] = mapped_column(String)
    scryfall_uri: Mapped[str | None] = mapped_column(String, nullable=True)
    note: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), default=_utcnow)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class ProductAlert(Base):
    __tablename__ = "product_alerts"
    __table_args__ = (UniqueConstraint("guild_id", "external_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    external_id: Mapped[str] = mapped_column(String)  # wp:{product_id}
    alerted_at: Mapped[datetime] = mapped_column(TZDateTime(), default=_utcnow)
