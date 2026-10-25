"""Tests for the two-bot launcher (src/__main__.py)."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src import __main__ as launcher
from src import config


def _stub_bots(monkeypatch, *, token="tok", decision_token=""):
    """Stub both bots to start-and-return; return (events_bot, decision_run, started)."""
    monkeypatch.setattr(config, "DISCORD_TOKEN", token)
    monkeypatch.setattr(config, "DECISION_BOT_TOKEN", decision_token)
    started = []
    events_bot = MagicMock()
    events_bot.start = AsyncMock(
        side_effect=lambda *a: started.append("events"))
    decision_run = AsyncMock(side_effect=lambda: started.append("decision"))
    monkeypatch.setattr("src.bot.create_bot", lambda: events_bot)
    monkeypatch.setattr("src.decision_bot.run_decision_bot", decision_run)
    return started, events_bot, decision_run


def test_launcher_runs_events_only_without_decision_token(monkeypatch):
    started, events_bot, decision_run = _stub_bots(monkeypatch, decision_token="")
    launcher.main()
    assert started == ["events"]
    decision_run.assert_not_awaited()


def test_launcher_runs_both_when_decision_token_set(monkeypatch):
    monkeypatch.setattr(config, "DISCORD_TOKEN", "tok")
    monkeypatch.setattr(config, "DECISION_BOT_TOKEN", "dtok")
    # drive the real main() but stub the bots to return immediately
    events_bot = MagicMock()
    events_bot.start = AsyncMock(return_value=None)
    decision_run = AsyncMock(return_value=None)
    monkeypatch.setattr("src.bot.create_bot", lambda: events_bot)
    monkeypatch.setattr("src.decision_bot.run_decision_bot", decision_run)
    launcher.main()
    events_bot.start.assert_awaited_once()
    decision_run.assert_awaited_once()


def test_launcher_exits_without_events_token(monkeypatch):
    monkeypatch.setattr(config, "DISCORD_TOKEN", "")
    monkeypatch.setattr(config, "DECISION_BOT_TOKEN", "dtok")
    with pytest.raises(SystemExit):
        launcher.main()


def test_launcher_events_bot_constructed_via_create_bot(monkeypatch):
    # sanity: the events task builds via create_bot (not a bare Client)
    monkeypatch.setattr(config, "DISCORD_TOKEN", "tok")
    monkeypatch.setattr(config, "DECISION_BOT_TOKEN", "")
    events_bot = MagicMock()
    events_bot.start = AsyncMock(return_value=None)
    create = MagicMock(return_value=events_bot)
    monkeypatch.setattr("src.bot.create_bot", create)
    launcher.main()
    create.assert_called_once()
