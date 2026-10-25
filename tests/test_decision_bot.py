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


@pytest.fixture(autouse=True)
def _clear_throttle():
    decision_bot._channel_windows.clear()
    yield
    decision_bot._channel_windows.clear()


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


def _history_message(text, author_name):
    m = MagicMock()
    m.content = text
    m.author = MagicMock(bot=False, display_name=author_name)
    return m


class _History:
    """Async-iterable channel.history returning the given messages (already oldest-last)."""

    def __init__(self, messages):
        self._messages = messages

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)


async def test_recent_context_includes_back_and_forth(db, monkeypatch):
    """An agreement message is judged with the proposals it resolves."""
    _enabled(db)
    captured = []
    monkeypatch.setattr(decision_service, "detect_decision",
                        AsyncMock(side_effect=lambda conv: captured.append(list(conv)) or
                                    decision_service.DetectionResult(True, "Price set at $175", "")))
    m = _message("ok, let's settle on 175 for it then")
    m.author.bot = False
    m.author.display_name = "Sam"
    # Discord's history(oldest_first=False) yields newest first; _recent_context reverses it.
    m.channel.history = lambda **kw: _History([
        _history_message("nah too low, 200", "Sam"),   # newest of the two first
        _history_message("I reckon 150 for it", "Jo"),
    ])
    await _maybe_detect(_client(), m)
    assert captured, "detect_decision was not called"
    conv = captured[0]
    # oldest first, current message last, names attached
    assert conv[0].startswith("Jo:") and "150" in conv[0]
    assert conv[1].startswith("Sam:") and "200" in conv[1]
    assert conv[-1].startswith("Sam:") and "175" in conv[-1]
    m.channel.send.assert_awaited_once()  # decision posted


async def test_recent_context_falls_back_to_current_when_history_fails(db, monkeypatch):
    _enabled(db)
    captured = []
    monkeypatch.setattr(decision_service, "detect_decision",
                        AsyncMock(side_effect=lambda conv: captured.append(list(conv)) or
                                    decision_service.DetectionResult(True, "X", "")))
    m = _message("we decided to do the thing today for sure")
    m.channel.history = MagicMock(side_effect=discord.Forbidden(MagicMock(status=403), "no"))
    await _maybe_detect(_client(), m)
    assert captured and captured[0][-1].endswith("the thing today for sure")


async def test_context_stops_at_last_bot_confirmation_card(db, monkeypatch):
    """A new topic after a settled decision must not inherit the old thread as context."""
    _enabled(db)
    captured = []
    monkeypatch.setattr(decision_service, "detect_decision",
                        AsyncMock(side_effect=lambda conv: captured.append(list(conv)) or
                                    decision_service.DetectionResult(False, "", "")))
    bot_id = 999
    # history newest-first: current new topic, then the bot's old confirmation card, then the
    # OLD settled thread (150/200/175) which must be cut off.
    bot_card = MagicMock()
    bot_card.author = MagicMock(bot=True, id=bot_id)  # the bot's own card
    bot_card.content = ""  # card content lives in the embed
    old1 = _history_message("I reckon 150", "Jo")
    old2 = _history_message("ok 175 it is", "Sam")

    m = _message("what do you think of a new event on sundays for commander nights")
    m.author.bot = False
    client = MagicMock()
    client.user = MagicMock(id=bot_id)
    m.channel.history = lambda **kw: _History([bot_card, old2, old1])  # newest first
    await _maybe_detect(client, m)
    assert captured
    conv = captured[0]
    # only the new-topic message is in context; the settled 175 thread is cut at the bot card
    assert len(conv) == 1 and "commander nights" in conv[0]
    assert not any("175" in line for line in conv)


async def test_short_agreement_still_detected(db, monkeypatch):
    """A short agreement ('yes $90 is good') must reach the AI, not be length-filtered."""
    _enabled(db)
    monkeypatch.setattr(decision_service, "detect_decision",
                        AsyncMock(return_value=decision_service.DetectionResult(
                            True, "Price set at $90", "")))
    m = _message("yes $90 is good")  # 14 chars — under the old 25 floor
    await _maybe_detect(_client(), m)
    m.channel.send.assert_awaited_once()


async def test_second_decision_while_one_pending_is_suppressed(db, monkeypatch):
    """A back-and-forth that resolves over several messages posts one card, not two."""
    _enabled(db)
    monkeypatch.setattr(decision_service, "detect_decision",
                        AsyncMock(return_value=decision_service.DetectionResult(
                            True, "Price set at $90", "")))
    m1 = _message("yes $90 is good")
    await _maybe_detect(_client(), m1)
    assert m1.channel.send.await_count == 1
    # a follow-up agreement in the same channel while the first is still pending
    m2 = _message("so we agree $90 then, locked in")
    await _maybe_detect(_client(), m2)
    m2.channel.send.assert_not_awaited()  # no duplicate card


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


def test_min_message_chars_admits_short_agreements():
    # Short agreements must reach the AI; only content-free chatter is skipped.
    assert MIN_MESSAGE_CHARS <= len("yes $90 is good")
    assert MIN_MESSAGE_CHARS >= len("ok")  # "ok" alone is still skipped


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
    # throttle the AI call directly so the pending-dedupe (separate concern) can't mask it
    detect = AsyncMock(return_value=decision_service.DetectionResult(False, "", ""))
    monkeypatch.setattr(decision_service, "detect_decision", detect)
    decision_bot._channel_windows.clear()
    msg = "we decided something important here today"
    for _ in range(decision_bot._MAX_DETECTIONS_PER_MINUTE + 3):
        await _maybe_detect(_client(), _message(msg))
    # AI was consulted only up to the per-minute cap; the rest were throttled first
    assert detect.await_count == decision_bot._MAX_DETECTIONS_PER_MINUTE
    decision_bot._channel_windows.clear()
