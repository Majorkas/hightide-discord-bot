"""Tests for AI decision detection and the decision lifecycle."""
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src import config
from src.services import decision_service
from src.services.decision_service import (
    DetectionResult, _parse_detection, confirm_decision, create_pending_decision,
    detect_decision, dismiss_decision, pending_decisions, unsynced_confirmed,
)

GUILD = 4242


def _openai_resp(payload: dict, status=200):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = {"choices": [{"message": {"content": payload}}]}
    if status >= 500 or status == 429:
        r.raise_for_status.side_effect = httpx.HTTPStatusError(
            str(status), request=MagicMock(), response=r)
    return r


def _client(*responses, side_effect=None):
    patcher = patch("src.services.decision_service.httpx.AsyncClient")
    cls = patcher.start()
    client = AsyncMock()
    if side_effect is not None:
        client.post.side_effect = side_effect
    else:
        client.post.side_effect = list(responses)
    cls.return_value.__aenter__.return_value = client
    return patcher, client


# --- _parse_detection ---

@pytest.mark.parametrize("raw,expected", [
    ('{"is_decision": true, "summary": "Run Friday drafts", "decider_hint": "Sam"}',
     DetectionResult(True, "Run Friday drafts", "Sam")),
    ('{"is_decision": false, "summary": "", "decider_hint": ""}',
     DetectionResult(False, "", "")),
    ('{"is_decision": true}', DetectionResult(True, "", "")),
])
def test_parse_detection_valid(raw, expected):
    got = _parse_detection(raw)
    assert got == expected


@pytest.mark.parametrize("raw", [
    "not json", '{"is_decision": "yes"}', "[]", '{"summary": "x"}', "null", "42",
])
def test_parse_detection_rejects_malformed(raw):
    assert _parse_detection(raw) is None


def test_parse_detection_non_string_summary_coerced_empty():
    # a non-string summary (model noise) is dropped, not fatal
    got = _parse_detection('{"is_decision": true, "summary": 5}')
    assert got is not None and got.summary == ""


def test_parse_detection_caps_long_summary():
    raw = '{"is_decision": true, "summary": "' + "x" * 500 + '"}'
    got = _parse_detection(raw)
    assert len(got.summary) == decision_service.MAX_SUMMARY_CHARS


# --- detect_decision ---

async def test_detect_returns_parsed_result(monkeypatch):
    monkeypatch.setattr(config, "OPENAI_API_KEY", "key")
    p, client = _client(_openai_resp(
        '{"is_decision": true, "summary": "Order the new set", "decider_hint": "Jo"}'))
    try:
        got = await detect_decision("lets order the new set")
    finally:
        p.stop()
    assert got.is_decision is True and got.summary == "Order the new set"
    # called the chat completions endpoint with the key
    args, kwargs = client.post.call_args
    assert "chat/completions" in args[0]
    assert kwargs["headers"]["Authorization"] == "Bearer key"
    assert kwargs["json"]["response_format"] == {"type": "json_object"}


async def test_detect_no_key_returns_none(monkeypatch):
    monkeypatch.setattr(config, "OPENAI_API_KEY", "")
    assert await detect_decision("we decided") is None


async def test_detect_blank_text_returns_none(monkeypatch):
    monkeypatch.setattr(config, "OPENAI_API_KEY", "key")
    assert await detect_decision("   ") is None
    assert await detect_decision("") is None


async def test_detect_non200_returns_none(monkeypatch):
    monkeypatch.setattr(config, "OPENAI_API_KEY", "key")
    p, _ = _client(_openai_resp({}, status=400))
    try:
        assert await detect_decision("hi") is None
    finally:
        p.stop()


async def test_detect_bad_json_body_returns_none(monkeypatch):
    monkeypatch.setattr(config, "OPENAI_API_KEY", "key")
    p, _ = _client(_openai_resp("not json"))
    try:
        assert await detect_decision("we decided X") is None
    finally:
        p.stop()


async def test_detect_transport_error_retries_then_none(monkeypatch):
    monkeypatch.setattr(config, "OPENAI_API_KEY", "key")
    p, _ = _client(side_effect=httpx.ConnectError("down"))
    try:
        with patch("asyncio.sleep", new_callable=AsyncMock):
            assert await detect_decision("we decided") is None
    finally:
        p.stop()


# --- lifecycle ---

def test_create_pending_caps_and_defaults(session):
    d = create_pending_decision(session, GUILD, 1, 2, 3, "raw", "We decided to run drafts")
    assert d.status == "pending"
    assert d.summary == "We decided to run drafts"
    assert pending_decisions(session, GUILD)[0].id == d.id


def test_confirm_then_unsynced(session):
    d = create_pending_decision(session, GUILD, 1, 2, 3, "raw", "Decided X")
    out = confirm_decision(session, GUILD, d.id, confirmed_by=99)
    assert out.status == "confirmed" and out.confirmed_by == 99
    assert unsynced_confirmed(session, GUILD)[0].id == d.id


def test_dismiss_marks_rejected(session):
    d = create_pending_decision(session, GUILD, 1, 2, 3, "raw", "Decided X")
    out = dismiss_decision(session, GUILD, d.id)
    assert out.status == "rejected"
    assert unsynced_confirmed(session, GUILD) == []


def test_confirm_unknown_returns_none(session):
    assert confirm_decision(session, GUILD, 9999, 1) is None
    assert dismiss_decision(session, GUILD, 9999) is None
