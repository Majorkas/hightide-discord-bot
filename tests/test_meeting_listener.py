"""Tests for the meeting-upload listener branch in the decision bot."""
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from sqlalchemy import select

from src import decision_bot
from src.db.models import Base, Decision, DecisionConfig, Meeting
from src.db.session import get_engine, get_session_factory
from src.services import decision_service, meeting_service

GUILD = 4242
MEET_CH = 777


@pytest.fixture
def db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'ml.db'}"
    engine = get_engine(url)
    Base.metadata.create_all(engine)
    factory = get_session_factory(url)
    monkeypatch.setattr(decision_service, "_session_factory", factory)
    yield factory
    engine.dispose()
    factory.kw["bind"].dispose()


def _enabled(db, meetings_channel=MEET_CH, enabled=True):
    with db() as s:
        s.add(DecisionConfig(guild_id=GUILD, enabled=enabled,
                             meetings_channel_id=meetings_channel))
        s.commit()


def _attachment(filename="meeting.m4a", ctype="audio/mp4", url="https://cdn/x.m4a",
                size=1000):
    a = MagicMock()
    a.filename = filename
    a.content_type = ctype
    a.url = url
    a.size = size
    return a


def _message(attachments, channel_id=MEET_CH, in_guild=True, author_bot=False,
             manage_guild=True):
    m = MagicMock(spec=discord.Message)
    m.content = ""
    m.id = 5000
    m.attachments = attachments
    m.author = MagicMock(bot=author_bot, id=7, display_name="Sam")
    m.author.guild_permissions = MagicMock(manage_guild=manage_guild)
    m.guild = MagicMock(id=GUILD) if in_guild else None
    m.channel = MagicMock(id=channel_id)
    m.channel.send = AsyncMock()
    return m


def _pipeline(monkeypatch, notes=None, decisions=None):
    notes = notes if notes is not None else meeting_service.MeetingNotes(
        "- key point", decisions or [])
    monkeypatch.setattr(meeting_service, "download_attachment", AsyncMock(return_value=b"data"))
    monkeypatch.setattr(meeting_service, "transcribe_audio",
                        AsyncMock(return_value=meeting_service.TranscriptResult("a transcript", 1)))
    monkeypatch.setattr(meeting_service, "extract_notes", AsyncMock(return_value=notes))


# --- _is_meeting_upload filter ---

def test_is_meeting_upload_true_for_audio(db):
    m = _message([_attachment()])
    assert decision_bot._is_meeting_upload(m) is True


@pytest.mark.parametrize("attachments,bot,guild", [
    ([_attachment()], True, True),          # bot author
    ([_attachment()], False, False),        # DM
    ([], False, True),                       # no attachment
    ([_attachment("notes.txt", "text/plain")], False, True),  # not audio
])
def test_is_meeting_upload_false_cases(db, attachments, bot, guild):
    m = _message(attachments, author_bot=bot, in_guild=guild)
    assert decision_bot._is_meeting_upload(m) is False


# --- _process_meeting ---

async def test_process_meeting_posts_notes_and_decision_card(db, monkeypatch):
    _enabled(db)
    _pipeline(monkeypatch, decisions=["Run Friday drafts at 7pm"])
    m = _message([_attachment()])
    await decision_bot._process_meeting(MagicMock(), m)
    # notes card + one decision card
    assert m.channel.send.await_count == 2
    embeds = [c.kwargs["embed"] for c in m.channel.send.call_args_list if "embed" in c.kwargs]
    assert any(e.title.startswith("Meeting notes") for e in embeds)
    assert any(e.title == "Decision detected — confirm?" for e in embeds)
    with db() as s:
        assert s.execute(select(Meeting)).scalar_one().filename == "meeting.m4a"
        assert s.execute(select(Decision)).scalar_one().summary == "Run Friday drafts at 7pm"


async def test_process_meeting_non_staff_rejected(db, monkeypatch):
    _enabled(db)
    _pipeline(monkeypatch)
    m = _message([_attachment()], manage_guild=False)
    await decision_bot._process_meeting(MagicMock(), m)
    # staff-gate reply only; no transcription, no meeting stored
    m.channel.send.assert_awaited_once()
    assert "Only staff" in m.channel.send.call_args.args[0]
    meeting_service.download_attachment.assert_not_awaited()
    with db() as s:
        assert s.execute(select(Meeting)).scalars().all() == []


# --- on_message routing (audio in meetings channel vs elsewhere) ---

async def test_on_message_routes_meetings_channel(db, monkeypatch):
    _enabled(db)
    _pipeline(monkeypatch)
    m = _message([_attachment()], channel_id=MEET_CH)
    bot = decision_bot.create_decision_bot()
    with patch.object(decision_bot, "_process_meeting", new=AsyncMock()) as proc:
        await bot.on_message(m)
    proc.assert_awaited_once()


async def test_on_message_audio_elsewhere_falls_to_detection(db, monkeypatch):
    # Audio attachment in a monitored NON-meetings channel must NOT be swallowed by
    # the meeting pipeline; it falls through to decision detection.
    _enabled(db, meetings_channel=MEET_CH)
    m = _message([_attachment()], channel_id=999)  # not the meetings channel
    bot = decision_bot.create_decision_bot()
    with patch.object(decision_bot, "_process_meeting", new=AsyncMock()) as proc, \
         patch.object(decision_bot, "_maybe_detect", new=AsyncMock()) as det:
        await bot.on_message(m)
    proc.assert_not_awaited()
    det.assert_awaited_once()


async def test_process_meeting_notes_only_when_no_decisions(db, monkeypatch):
    _enabled(db)
    _pipeline(monkeypatch, decisions=[])
    m = _message([_attachment()])
    await decision_bot._process_meeting(MagicMock(), m)
    assert m.channel.send.await_count == 1  # notes only
    with db() as s:
        assert s.execute(select(Decision)).scalars().all() == []


async def test_process_meeting_failure_posts_friendly_message(db, monkeypatch):
    _enabled(db)
    monkeypatch.setattr(meeting_service, "download_attachment",
                        AsyncMock(side_effect=meeting_service.MeetingError("too large")))
    m = _message([_attachment()])
    await decision_bot._process_meeting(MagicMock(), m)
    m.channel.send.assert_awaited_once()
    assert "Couldn't process" in m.channel.send.call_args.args[0]
    with db() as s:
        assert s.execute(select(Meeting)).scalars().all() == []


async def test_process_meeting_unexpected_error_posts_generic(db, monkeypatch):
    _enabled(db)
    monkeypatch.setattr(meeting_service, "download_attachment",
                        AsyncMock(side_effect=RuntimeError("boom")))
    m = _message([_attachment()])
    await decision_bot._process_meeting(MagicMock(), m)  # must not raise
    assert "went wrong" in m.channel.send.call_args.args[0]
