"""Tests for the decision-bot listener (on_message -> detect -> confirmation card)."""
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from src import config
from src.db.models import Base, Decision, DecisionChannel, DecisionConfig
from src.db.session import get_engine, get_session_factory
from src.services import decision_service
from src import decision_bot
from src.decision_bot import MIN_MESSAGE_CHARS, _maybe_detect, create_decision_bot

GUILD = 4242
CHANNEL = 555


@pytest.fixture
def db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'dbot.db'}"
    engine = get_engine(url)
    Base.metadata.create_all(engine)
    factory = get_session_factory(url)
    monkeypatch.setattr(decision_service, "_session_factory", factory)
    yield factory
    engine.dispose()
    factory.kw["bind"].dispose()


def _enabled(db, monitored=(CHANNEL,)):
    with db() as s:
        cfg = DecisionConfig(guild_id=GUILD, enabled=True)
        s.add(cfg)
        for cid in monitored:
            s.add(DecisionChannel(guild_id=GUILD, channel_id=cid))
        s.commit()


def _message(content, author_bot=False, channel_id=CHANNEL, in_guild=True):
    m = MagicMock(spec=discord.Message)
    m.content = content
    m.id = 1000
    m.author = MagicMock(bot=author_bot, id=7)
    m.guild = MagicMock(id=GUILD) if in_guild else None
    m.channel = MagicMock(id=channel_id)
    m.channel.send = AsyncMock()
    return m


def _client():
    return MagicMock()


async def test_detects_decision_in_monitored_channel(db, monkeypatch):
    _enabled(db)
    monkeypatch.setattr(decision_service, "detect_decision",
                        AsyncMock(return_value=decision_service.DetectionResult(
                            True, "Run Friday drafts", "")))
    m = _message("Okay team, we decided to run Friday drafts at 7pm going forward.")
    await _maybe_detect(_client(), m)
    m.channel.send.assert_awaited_once()
    kw = m.channel.send.call_args.kwargs
    assert kw["embed"].title == "Decision detected — confirm?"
    assert kw["view"] is not None
    assert kw["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()
    with db() as s:
        d = s.execute(__import__("sqlalchemy").select(Decision)).scalar_one()
        assert d.status == "pending" and d.summary == "Run Friday drafts"


async def test_ignores_when_no_decision_detected(db, monkeypatch):
    _enabled(db)
    monkeypatch.setattr(decision_service, "detect_decision",
                        AsyncMock(return_value=decision_service.DetectionResult(False, "", "")))
    m = _message("Just chatting about nothing in particular here, how was your weekend?")
    await _maybe_detect(_client(), m)
    m.channel.send.assert_not_awaited()


async def test_ignores_bot_messages(db, monkeypatch):
    _enabled(db)
    detect = AsyncMock()
    monkeypatch.setattr(decision_service, "detect_decision", detect)
    m = _message("we decided something important here today", author_bot=True)
    await _maybe_detect(_client(), m)
    detect.assert_not_awaited()
    m.channel.send.assert_not_awaited()


async def test_ignores_dms(db, monkeypatch):
    _enabled(db)
    detect = AsyncMock()
    monkeypatch.setattr(decision_service, "detect_decision", detect)
    m = _message("we decided something important here today", in_guild=False)
    await _maybe_detect(_client(), m)
    detect.assert_not_awaited()


async def test_ignores_short_messages(db, monkeypatch):
    _enabled(db)
    detect = AsyncMock()
    monkeypatch.setattr(decision_service, "detect_decision", detect)
    m = _message("ok")  # below MIN_MESSAGE_CHARS
    await _maybe_detect(_client(), m)
    detect.assert_not_awaited()


async def test_ignores_unmonitored_channel(db, monkeypatch):
    _enabled(db, monitored=(CHANNEL,))
    detect = AsyncMock()
    monkeypatch.setattr(decision_service, "detect_decision", detect)
    m = _message("we decided something important here today", channel_id=999)
    await _maybe_detect(_client(), m)
    detect.assert_not_awaited()


async def test_ignores_when_disabled(db, monkeypatch):
    with db() as s:  # enabled=False
        s.add(DecisionConfig(guild_id=GUILD, enabled=False))
        s.add(DecisionChannel(guild_id=GUILD, channel_id=CHANNEL))
        s.commit()
    detect = AsyncMock()
    monkeypatch.setattr(decision_service, "detect_decision", detect)
    m = _message("we decided something important here today")
    await _maybe_detect(_client(), m)
    detect.assert_not_awaited()


async def test_detect_failure_posts_nothing(db, monkeypatch):
    _enabled(db)
    monkeypatch.setattr(decision_service, "detect_decision", AsyncMock(return_value=None))
    m = _message("we decided something important here today")
    await _maybe_detect(_client(), m)
    m.channel.send.assert_not_awaited()
    with db() as s:
        assert s.execute(__import__("sqlalchemy").select(Decision)).scalars().all() == []


async def test_send_failure_is_tolerated(db, monkeypatch):
    _enabled(db)
    monkeypatch.setattr(decision_service, "detect_decision",
                        AsyncMock(return_value=decision_service.DetectionResult(
                            True, "X", "")))
    m = _message("we decided something important here today")
    m.channel.send.side_effect = discord.Forbidden(MagicMock(status=403), "no")
    await _maybe_detect(_client(), m)  # must not raise


def test_create_decision_bot_has_message_content_intent():
    client = create_decision_bot()
    assert client.intents.message_content is True


async def test_decision_bot_registers_decision_action_in_setup_hook(tmp_path, monkeypatch):
    # The decision bot authors the cards, so IT must register DecisionAction (else the
    # buttons go dead after a restart). Covers the wrong-client regression.
    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{tmp_path / 'dbt.db'}")
    client = create_decision_bot()
    client.add_dynamic_items = MagicMock()
    await client.setup_hook()
    from src.discord_ui.decision_views import DecisionAction
    client.add_dynamic_items.assert_called_once_with(DecisionAction)
    # and the decision tables now exist on a fresh DB (listener works before events bot)
    from sqlalchemy import text
    from src.db.session import get_engine
    engine = get_engine(config.DATABASE_URL)
    with engine.connect() as conn:
        names = {r[0] for r in conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'"))}
    assert {"decisions", "trello_sync", "decision_config", "decision_channels",
            "trello_member_map"} <= names
    engine.dispose()


def test_min_message_chars_is_reasonable():
    assert 10 <= MIN_MESSAGE_CHARS <= 100


async def test_empty_summary_falls_back_to_excerpt(db, monkeypatch):
    _enabled(db)
    monkeypatch.setattr(decision_service, "detect_decision",
                        AsyncMock(return_value=decision_service.DetectionResult(True, "", "")))
    m = _message("we decided something important here today and it matters a lot")
    await _maybe_detect(_client(), m)
    with db() as s:
        d = s.execute(__import__("sqlalchemy").select(Decision)).scalar_one()
        assert d.summary == m.content[:200]  # empty summary -> excerpt fallback


async def test_throttle_caps_detections_per_channel(db, monkeypatch):
    _enabled(db)
    monkeypatch.setattr(decision_service, "detect_decision",
                        AsyncMock(return_value=decision_service.DetectionResult(True, "X", "")))
    decision_bot._channel_windows.clear()
    msg = "we decided something important here today"
    sent = 0
    for _ in range(decision_bot._MAX_DETECTIONS_PER_MINUTE + 3):
        m = _message(msg)
        await _maybe_detect(_client(), m)
        sent += m.channel.send.await_count
    assert sent == decision_bot._MAX_DETECTIONS_PER_MINUTE  # capped
    decision_bot._channel_windows.clear()
