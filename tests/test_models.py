from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError, StatementError

from src.db.models import BoardPost, Event, GuildConfig


def test_models_create_and_insert(session):
    session.add(GuildConfig(guild_id=1))
    session.add(Event(guild_id=1, external_id="wp:1", title="Test", url="https://x"))
    session.commit()
    assert session.query(GuildConfig).count() == 1
    assert session.query(Event).first().external_id == "wp:1"


def test_tz_datetime_round_trip(session):
    """Timezone-aware datetimes round-trip through SQLite correctly."""
    test_time = datetime(2026, 10, 23, 14, 30, 0, tzinfo=timezone.utc)
    event = Event(
        guild_id=1,
        external_id="wp:tz-test",
        title="TZ Test",
        url="https://x",
        starts_at=test_time,
    )
    session.add(event)
    session.commit()
    event_id = event.id

    session.expire_all()

    retrieved = session.query(Event).filter_by(id=event_id).first()
    assert retrieved is not None
    assert retrieved.starts_at.tzinfo == timezone.utc
    assert retrieved.starts_at == test_time


def test_tz_datetime_non_utc_input_normalized_to_utc(session):
    plus8 = timezone(timedelta(hours=8))
    event = Event(
        guild_id=1, external_id="wp:tz8", title="T", url="u",
        starts_at=datetime(2026, 10, 23, 14, 30, tzinfo=plus8),
    )
    session.add(event)
    session.commit()
    session.expire_all()

    got = session.query(Event).filter_by(external_id="wp:tz8").one()
    assert got.starts_at.tzinfo == timezone.utc
    assert got.starts_at == datetime(2026, 10, 23, 6, 30, tzinfo=timezone.utc)
    assert got.starts_at.hour == 6


def test_tz_datetime_naive_rejected(session):
    session.add(Event(
        guild_id=1, external_id="wp:naive", title="T", url="u",
        starts_at=datetime(2026, 10, 23, 14, 30),
    ))
    # SQLAlchemy wraps the bind-time ValueError in StatementError.
    with pytest.raises(StatementError) as exc:
        session.commit()
    assert isinstance(exc.value.orig, ValueError)
    session.rollback()


def test_starts_at_none_allowed(session):
    session.add(Event(guild_id=1, external_id="wp:none", title="T", url="u", starts_at=None))
    session.commit()
    session.expire_all()
    assert session.query(Event).filter_by(external_id="wp:none").one().starts_at is None


def test_event_guild_external_id_unique(session):
    session.add(Event(guild_id=1, external_id="wp:1", title="A", url="u"))
    session.commit()
    session.add(Event(guild_id=1, external_id="wp:1", title="B", url="u"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_same_external_id_in_different_guilds_allowed(session):
    session.add(Event(guild_id=1, external_id="wp:1", title="A", url="u"))
    session.add(Event(guild_id=2, external_id="wp:1", title="B", url="u"))
    session.commit()
    assert session.query(Event).count() == 2


def test_board_post_guild_board_type_unique(session):
    def post():
        return BoardPost(guild_id=1, board_type="this_week", period_key="k",
                         channel_id=1, message_id=1)
    session.add(post())
    session.commit()
    session.add(post())
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_snowflake_guild_id_round_trip(session):
    gid = 1234567890123456789
    session.add(GuildConfig(guild_id=gid, this_week_channel_id=gid + 1))
    session.add(Event(guild_id=gid, external_id="wp:big", title="T", url="u"))
    session.commit()
    session.expire_all()
    cfg = session.get(GuildConfig, gid)
    assert cfg.guild_id == gid
    assert cfg.this_week_channel_id == gid + 1
    assert session.query(Event).filter_by(guild_id=gid).one().guild_id == gid
