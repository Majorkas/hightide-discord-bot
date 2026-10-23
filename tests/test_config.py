import importlib
import os


def test_config_defaults(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "test-token")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DEFAULT_TIMEZONE", raising=False)
    monkeypatch.delenv("GUILD_ID", raising=False)
    import src.config as config
    importlib.reload(config)
    assert config.DISCORD_TOKEN == "test-token"
    assert config.DATABASE_URL == "sqlite:///./hightide.db"
    assert config.DEFAULT_TIMEZONE == "Australia/Perth"
    assert config.GUILD_ID is None
