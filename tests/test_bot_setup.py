from unittest.mock import AsyncMock

import discord
import pytest
from sqlalchemy import inspect, text

from src import config
from src.bot import create_bot
from src.db.models import Base
from src.db.session import (
    ensure_board_post_message_ids, ensure_guild_config_weekly_image_url, get_engine,
)
from src.services import scheduler


@pytest.fixture
def env(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'setup.db'}"
    monkeypatch.setattr(config, "DATABASE_URL", url)
    monkeypatch.setattr(scheduler, "_session_factory", None)
    # RefreshScheduler.start() would spawn a loop that waits for a live Discord
    # connection, so record the call instead of starting it.
    started = []
    monkeypatch.setattr(scheduler.RefreshScheduler, "start",
                        lambda self: started.append(self))
    bot = create_bot()
    bot.tree.sync = AsyncMock()
    bot.tree.copy_global_to = lambda **kw: None
    return bot, url, started


async def test_setup_hook_creates_tables_and_starts_scheduler(env, monkeypatch):
    monkeypatch.setattr(config, "GUILD_ID", None)
    bot, url, started = env
    await bot.setup_hook()
    engine = get_engine(url)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert {"guild_config", "events", "board_posts", "event_sources"} <= tables
    assert len(started) == 1
    assert started[0].bot is bot


async def test_setup_hook_syncs_globally_without_guild(env, monkeypatch):
    monkeypatch.setattr(config, "GUILD_ID", None)
    bot, _, _ = env
    await bot.setup_hook()
    bot.tree.sync.assert_awaited_once_with()


async def test_setup_hook_syncs_to_guild_when_set(env, monkeypatch):
    monkeypatch.setattr(config, "GUILD_ID", 4242)
    bot, _, _ = env
    copied = []
    bot.tree.copy_global_to = lambda **kw: copied.append(kw)
    await bot.setup_hook()
    bot.tree.sync.assert_awaited_once()
    args, kwargs = bot.tree.sync.call_args
    assert not args
    guild = kwargs["guild"]
    assert isinstance(guild, discord.Object) and guild.id == 4242
    assert copied == [{"guild": guild}]


_LEGACY_DDL = (
    "CREATE TABLE board_posts (id INTEGER PRIMARY KEY, guild_id BIGINT, board_type VARCHAR, "
    "period_key VARCHAR, channel_id BIGINT, message_id BIGINT, render_hash VARCHAR, "
    "updated_at DATETIME, UNIQUE (guild_id, board_type))"
)


def _legacy_db(url):
    engine = get_engine(url)
    with engine.begin() as conn:
        conn.execute(text(_LEGACY_DDL))
        conn.execute(text("INSERT INTO board_posts (guild_id, board_type, period_key, channel_id, "
                          "message_id, render_hash) VALUES (1, 'this_week', 'k', 2, 3, 'h')"))
    return engine


def test_ensure_message_ids_column_idempotent_on_legacy_table(tmp_path):
    engine = _legacy_db(f"sqlite:///{tmp_path / 'legacy.db'}")
    try:
        ensure_board_post_message_ids(engine)
        ensure_board_post_message_ids(engine)  # duplicate column is tolerated
        assert "message_ids" in {c["name"] for c in inspect(engine).get_columns("board_posts")}
        with engine.connect() as conn:
            assert conn.execute(text("SELECT message_ids FROM board_posts")).scalar_one() == "[]"
    finally:
        engine.dispose()


def test_ensure_message_ids_column_noop_on_fresh_schema(tmp_path):
    engine = get_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    try:
        Base.metadata.create_all(engine)
        ensure_board_post_message_ids(engine)
        ensure_board_post_message_ids(engine)
    finally:
        engine.dispose()


async def test_setup_hook_upgrades_legacy_board_posts_table(env, monkeypatch):
    monkeypatch.setattr(config, "GUILD_ID", None)
    bot, url, _ = env
    _legacy_db(url).dispose()
    await bot.setup_hook()
    engine = get_engine(url)
    try:
        assert "message_ids" in {c["name"] for c in inspect(engine).get_columns("board_posts")}
    finally:
        engine.dispose()


_LEGACY_GUILD_CONFIG_DDL = (
    "CREATE TABLE guild_config (guild_id BIGINT PRIMARY KEY, timezone VARCHAR, "
    "this_week_channel_id BIGINT, this_month_channel_id BIGINT, daily_refresh_time VARCHAR, "
    "week_start_mode VARCHAR, include_tba_section BOOLEAN, delete_previous_month_message BOOLEAN)"
)


async def test_setup_hook_ddl_twice_adds_weekly_schema_idempotently(env, monkeypatch):
    monkeypatch.setattr(config, "GUILD_ID", None)
    bot, url, _ = env
    engine = get_engine(url)
    with engine.begin() as conn:
        conn.execute(text(_LEGACY_GUILD_CONFIG_DDL))
        conn.execute(text("INSERT INTO guild_config (guild_id) VALUES (7)"))
    engine.dispose()

    await bot.setup_hook()
    await bot.setup_hook()  # second run is a no-op

    engine = get_engine(url)
    try:
        insp = inspect(engine)
        assert "weekly_image_url" in {c["name"] for c in insp.get_columns("guild_config")}
        assert "weekly_seeded" in {c["name"] for c in insp.get_columns("guild_config")}
        assert "weekly_recurring" in insp.get_table_names()
        with engine.connect() as conn:
            assert conn.execute(text("SELECT weekly_seeded FROM guild_config")).scalar_one() == 0
            assert conn.execute(text("SELECT weekly_image_url FROM guild_config")).scalar_one() is None
    finally:
        engine.dispose()


def test_ensure_weekly_image_url_noop_on_fresh_schema(tmp_path):
    engine = get_engine(f"sqlite:///{tmp_path / 'fresh2.db'}")
    try:
        Base.metadata.create_all(engine)
        ensure_guild_config_weekly_image_url(engine)
        ensure_guild_config_weekly_image_url(engine)
    finally:
        engine.dispose()


def test_main_without_token_exits_before_running(monkeypatch):
    from src import bot as bot_module

    monkeypatch.setattr(config, "DISCORD_TOKEN", "")
    create = []
    monkeypatch.setattr(bot_module, "create_bot", lambda: create.append(1))

    with pytest.raises(SystemExit) as exc:
        bot_module.main()

    assert str(exc.value) == "DISCORD_TOKEN is not set — copy .env.example to .env and fill it in"
    assert create == []
