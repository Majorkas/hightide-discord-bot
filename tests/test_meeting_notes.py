"""Tests for meeting notes + decisions extraction (GPT)."""
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src import config
from src.services import meeting_service
from src.services.meeting_service import (
    MeetingError, MeetingNotes, _parse_notes, extract_notes,
)


def _client(payload, status=200):
    patcher = patch("src.services.meeting_service.httpx.AsyncClient")
    cls = patcher.start()
    client = AsyncMock()
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = {"choices": [{"message": {"content": payload}}]}
    if status >= 500 or status == 429:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            str(status), request=MagicMock(), response=resp)
    client.post.return_value = resp
    cls.return_value.__aenter__.return_value = client
    return patcher, client


# --- _parse_notes ---

@pytest.mark.parametrize("raw,expected", [
    ('{"notes": "- a\\n- b", "decisions": ["Run drafts at 7"]}',
     MeetingNotes("- a\n- b", ["Run drafts at 7"])),
    ('{"notes": "some notes", "decisions": []}', MeetingNotes("some notes", [])),
    ('{"notes": "", "decisions": ["X"]}', MeetingNotes("", ["X"])),
])
def test_parse_notes_valid(raw, expected):
    assert _parse_notes(raw) == expected


@pytest.mark.parametrize("raw", [
    "not json", "[]", "null", '{"notes": "", "decisions": []}', "42",
    '{"notes": "   ", "decisions": []}',
])
def test_parse_notes_rejects_empty_or_malformed(raw):
    assert _parse_notes(raw) is None


def test_parse_notes_caps_and_filters_decisions():
    raw = '{"notes": "n", "decisions": ["ok", "", "  ", 5, {"x":1}, "' + "y" * 300 + '"]}'
    got = _parse_notes(raw)
    assert got.decisions[0] == "ok" and got.decisions[1] == "y" * 200
    assert len(got.decisions) == 2  # blank/non-string dropped


def test_parse_notes_caps_long_notes():
    got = _parse_notes('{"notes": "' + "x" * 5000 + '", "decisions": []}')
    assert len(got.notes) == meeting_service.MAX_NOTES_CHARS


# --- extract_notes ---

async def test_extract_notes_success(monkeypatch):
    monkeypatch.setattr(config, "OPENAI_API_KEY", "key")
    p, client = _client('{"notes": "- key point", "decisions": ["Decided X"]}')
    try:
        got = await extract_notes("a transcript")
    finally:
        p.stop()
    assert got.notes == "- key point" and got.decisions == ["Decided X"]
    assert client.post.call_args.kwargs["json"]["response_format"] == {"type": "json_object"}


async def test_extract_notes_empty_transcript(monkeypatch):
    monkeypatch.setattr(config, "OPENAI_API_KEY", "key")
    with pytest.raises(MeetingError, match="nothing to summarise"):
        await extract_notes("   ")


async def test_extract_notes_unconfigured(monkeypatch):
    monkeypatch.setattr(config, "OPENAI_API_KEY", "")
    with pytest.raises(MeetingError, match="isn't configured"):
        await extract_notes("a transcript")


async def test_extract_notes_non200(monkeypatch):
    monkeypatch.setattr(config, "OPENAI_API_KEY", "key")
    p, _ = _client("{}", status=400)
    try:
        with pytest.raises(MeetingError, match="note extraction failed"):
            await extract_notes("a transcript")
    finally:
        p.stop()


async def test_extract_notes_bad_json(monkeypatch):
    monkeypatch.setattr(config, "OPENAI_API_KEY", "key")
    p, _ = _client("not json")
    try:
        with pytest.raises(MeetingError, match="unreadable"):
            await extract_notes("a transcript")
    finally:
        p.stop()


async def test_extract_notes_retry_then_success(monkeypatch):
    monkeypatch.setattr(config, "OPENAI_API_KEY", "key")
    patcher = patch("src.services.meeting_service.httpx.AsyncClient")
    cls = patcher.start()
    client = AsyncMock()
    bad = MagicMock(); bad.status_code = 500
    bad.raise_for_status.side_effect = httpx.HTTPStatusError("500", request=MagicMock(), response=bad)
    good = MagicMock(); good.status_code = 200
    good.json.return_value = {"choices": [{"message": {"content": '{"notes":"n","decisions":[]}'}}]}
    client.post.side_effect = [bad, good]
    cls.return_value.__aenter__.return_value = client
    try:
        with patch("asyncio.sleep", new_callable=AsyncMock):
            got = await extract_notes("a transcript")
    finally:
        patcher.stop()
    assert got.notes == "n"
    assert client.post.call_count == 2  # retried once
