"""Tests for the Trello service."""
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src import config
from src.services import trello_service
from src.services.trello_service import build_card_description, create_card


def _client(*responses, side_effect=None):
    patcher = patch("src.services.trello_service.httpx.AsyncClient")
    cls = patcher.start()
    client = AsyncMock()
    if side_effect is not None:
        client.post.side_effect = side_effect
    else:
        client.post.side_effect = list(responses)
    cls.return_value.__aenter__.return_value = client
    return patcher, client


def _resp(status=200, body=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body or {}
    r.text = str(body)
    if status >= 500 or status == 429:
        r.raise_for_status.side_effect = httpx.HTTPStatusError(
            str(status), request=MagicMock(), response=r)
    return r


@pytest.fixture
def trello_configured(monkeypatch):
    monkeypatch.setattr(config, "TRELLO_API_KEY", "key")
    monkeypatch.setattr(config, "TRELLO_API_TOKEN", "token")


async def test_create_card_success(trello_configured):
    p, client = _client(_resp(200, {"id": "card123", "url": "https://trello.com/c/card123"}))
    try:
        card = await create_card("list1", "Decision: run drafts", "desc body")
    finally:
        p.stop()
    assert card is not None and card.card_id == "card123"
    assert card.url == "https://trello.com/c/card123"
    _, kwargs = client.post.call_args
    params = kwargs["params"]
    assert params["idList"] == "list1"
    assert params["key"] == "key" and params["token"] == "token"
    assert params["name"] == "Decision: run drafts"
    assert "idMembers" not in params  # no member


async def test_create_card_assigns_member(trello_configured):
    p, client = _client(_resp(200, {"id": "c1", "shortUrl": "https://trello.com/c/c1"}))
    try:
        card = await create_card("list1", "X", "d", member_id="member9")
    finally:
        p.stop()
    assert card.url == "https://trello.com/c/c1"  # falls back to shortUrl
    assert client.post.call_args.kwargs["params"]["idMembers"] == "member9"


async def test_create_card_not_configured_returns_none(monkeypatch):
    monkeypatch.setattr(config, "TRELLO_API_KEY", "")
    monkeypatch.setattr(config, "TRELLO_API_TOKEN", "")
    assert await create_card("list1", "X", "d") is None


async def test_create_card_missing_list_returns_none(trello_configured):
    assert await create_card("", "X", "d") is None


@pytest.mark.parametrize("status", [400, 401, 403, 404])
async def test_create_card_4xx_returns_none_no_retry(trello_configured, status):
    p, client = _client(_resp(status, {"error": "bad"}))
    try:
        assert await create_card("list1", "X", "d") is None
    finally:
        p.stop()
    assert client.post.call_count == 1  # 4xx not retried


async def test_create_card_5xx_retries_then_none(trello_configured):
    p, _ = _client(_resp(500), _resp(500), _resp(500))
    try:
        with patch("asyncio.sleep", new_callable=AsyncMock):
            assert await create_card("list1", "X", "d") is None
    finally:
        p.stop()


async def test_create_card_no_id_in_body_returns_none(trello_configured):
    p, _ = _client(_resp(200, {"url": "x"}))
    try:
        assert await create_card("list1", "X", "d") is None
    finally:
        p.stop()


async def test_create_card_transport_error_retries_then_none(trello_configured):
    p, _ = _client(side_effect=httpx.ConnectError("down"))
    try:
        with patch("asyncio.sleep", new_callable=AsyncMock):
            assert await create_card("list1", "X", "d") is None
    finally:
        p.stop()


async def test_create_card_caps_long_fields(trello_configured):
    p, client = _client(_resp(200, {"id": "c1"}))
    try:
        await create_card("list1", "n" * 20000, "d" * 20000)
    finally:
        p.stop()
    assert len(client.post.call_args.kwargs["params"]["name"]) == 16384
    assert len(client.post.call_args.kwargs["params"]["desc"]) == 16384


def test_build_card_description_full_and_minimal():
    full = build_card_description(decider_name="Sam", when="Oct 25 2026, 6:00 PM",
                                  channel_name="staff", message_url="https://discord.com/x",
                                  excerpt="we decided to run drafts")
    for part in ("Sam", "Oct 25 2026", "staff", "https://discord.com/x", "we decided"):
        assert part in full
    minimal = build_card_description(decider_name="Sam", when="w", channel_name="c",
                                     message_url=None, excerpt="e")
    assert "Source" not in minimal and "Sam" in minimal
