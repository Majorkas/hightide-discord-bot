from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, Text, TypeDecorator, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TZDateTime(TypeDecorator):
    """Timezone-aware DateTime that ensures UTC storage and retrieval from SQLite."""
    impl = DateTime(timezone=True)
    cache_ok = True

    def bind_processor(self, dialect):
        def process_value(value):
            if value is None:
                return None
            if not isinstance(value, datetime):
                return value
            if value.tzinfo is None:
                raise ValueError(f"DateTime {value} must be timezone-aware")
            # Convert to UTC
            return value.astimezone(timezone.utc)
        return process_value

    def result_processor(self, dialect, coltype):
        def process_value(value):
            if value is None:
                return None
            # SQLite returns datetime strings; parse them
            if isinstance(value, str):
                # Parse ISO format string (e.g., "2026-10-23 14:30:00.000000")
                value = datetime.fromisoformat(value)
            if isinstance(value, datetime) and value.tzinfo is None:
                # SQLite returns naive datetimes; attach UTC
                return value.replace(tzinfo=timezone.utc)
            return value
        return process_value


class GuildConfig(Base):
    __tablename__ = "guild_config"

    guild_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timezone: Mapped[str] = mapped_column(String, default="Australia/Perth")
    this_week_channel_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    this_month_channel_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    daily_refresh_time: Mapped[str] = mapped_column(String, default="09:00")
    week_start_mode: Mapped[str] = mapped_column(String, default="today_start")
    include_tba_section: Mapped[bool] = mapped_column(Boolean, default=True)
    delete_previous_month_message: Mapped[bool] = mapped_column(Boolean, default=True)


class EventSource(Base):
    __tablename__ = "event_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(Integer)
    url: Mapped[str] = mapped_column(String)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (UniqueConstraint("guild_id", "external_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(Integer)
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

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(Integer)
    board_type: Mapped[str] = mapped_column(String)  # this_week | this_month
    period_key: Mapped[str] = mapped_column(String)  # e.g. 2026-W43 | 2026-10
    channel_id: Mapped[int] = mapped_column(Integer)
    message_id: Mapped[int] = mapped_column(Integer)
    render_hash: Mapped[str] = mapped_column(String, default="")
    updated_at: Mapped[datetime] = mapped_column(TZDateTime(), default=_utcnow)
