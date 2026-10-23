import importlib

import pytest

import src.config as config


@pytest.fixture(autouse=True)
def _restore_config(monkeypatch):
    yield
    monkeypatch.undo()
    importlib.reload(config)


def _reload():
    return importlib.reload(config)


def test_config_defaults(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "test-token")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DEFAULT_TIMEZONE", raising=False)
    monkeypatch.delenv("GUILD_ID", raising=False)
    _reload()
    assert config.DISCORD_TOKEN == "test-token"
    assert config.DATABASE_URL == "sqlite:///./hightide.db"
    assert config.DEFAULT_TIMEZONE == "Australia/Perth"
    assert config.GUILD_ID is None


def test_guild_id_numeric(monkeypatch):
    monkeypatch.setenv("GUILD_ID", "123")
    _reload()
    assert config.GUILD_ID == 123


def test_guild_id_empty_is_none(monkeypatch):
    monkeypatch.setenv("GUILD_ID", "")
    _reload()
    assert config.GUILD_ID is None


def test_guild_id_non_numeric_raises(monkeypatch):
    monkeypatch.setenv("GUILD_ID", "abc")
    with pytest.raises(ValueError):
        _reload()
