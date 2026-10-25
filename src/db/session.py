import logging

from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

log = logging.getLogger("hightide.db")


def get_engine(url: str):
    return create_engine(url, future=True)


def ensure_board_post_message_ids(engine) -> None:
    """Add board_posts.message_ids to pre-existing DBs (no migration framework in v1)."""
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE board_posts ADD COLUMN message_ids TEXT NOT NULL DEFAULT '[]'"))
    except OperationalError as exc:
        if "duplicate column" not in str(exc).lower():
            raise
        return
    log.info("added board_posts.message_ids column")


def ensure_guild_config_weekly_image_url(engine) -> None:
    """Add guild_config.weekly_image_url to pre-existing DBs."""
    try:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE guild_config ADD COLUMN weekly_image_url VARCHAR"))
    except OperationalError as exc:
        if "duplicate column" not in str(exc).lower():
            raise
        return
    log.info("added guild_config.weekly_image_url column")


def ensure_guild_config_weekly_seeded(engine) -> None:
    """Add guild_config.weekly_seeded to pre-existing DBs."""
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE guild_config ADD COLUMN weekly_seeded BOOLEAN NOT NULL DEFAULT 0"))
    except OperationalError as exc:
        if "duplicate column" not in str(exc).lower():
            raise
        return
    log.info("added guild_config.weekly_seeded column")


def ensure_guild_config_v2_columns(engine) -> None:
    """Add the v2 guild_config columns (roles, announce channel, daily post) to pre-existing DBs."""
    columns = [
        ("roles_channel_id", "BIGINT"),
        ("roles_message_id", "BIGINT"),
        ("announce_channel_id", "BIGINT"),
        ("daily_post_time", "VARCHAR NOT NULL DEFAULT '09:00'"),
        ("last_daily_post_date", "VARCHAR"),
        ("wants_channel_id", "BIGINT"),
        ("new_stock_channel_id", "BIGINT"),
        ("preorder_channel_id", "BIGINT"),
    ]
    for col_name, col_type in columns:
        try:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE guild_config ADD COLUMN {col_name} {col_type}"))
        except OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise
            continue
        log.info("added guild_config v2 column")


def ensure_card_wants_printing_columns(engine) -> None:
    """Add the printing columns (set/collector/image) to pre-existing card_wants tables."""
    for col_name in ("set_code", "collector_number", "image_url"):
        try:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE card_wants ADD COLUMN {col_name} VARCHAR"))
        except OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise
            continue
        log.info("added card_wants.%s column", col_name)


def get_session_factory(url: str) -> sessionmaker:
    return sessionmaker(bind=get_engine(url), expire_on_commit=False, future=True)
