from unittest.mock import AsyncMock

import discord
import pytest
from sqlalchemy import inspect

from src import config
from src.bot import create_bot
from src.db.session import get_engine
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


def test_main_without_token_exits_before_running(monkeypatch):
    from src import bot as bot_module

    monkeypatch.setattr(config, "DISCORD_TOKEN", "")
    create = []
    monkeypatch.setattr(bot_module, "create_bot", lambda: create.append(1))

    with pytest.raises(SystemExit) as exc:
        bot_module.main()

    assert str(exc.value) == "DISCORD_TOKEN is not set — copy .env.example to .env and fill it in"
    assert create == []
