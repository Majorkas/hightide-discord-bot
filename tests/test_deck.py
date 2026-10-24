"""Tests for /deck (Moxfield + Archidekt)."""
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import httpx
import pytest

from src.services.deck_service import (
    DeckSummary, build_deck_embed, fetch_deck, parse_deck_url, scryfall_card_by_id,
)

FIXTURES = Path(__file__).parent / "fixtures"
MOX_ID = "oEWXWHM5eEGMmopExLWRCA"
SFID = "d0d33d52-3d28-4635-b985-51e126289259"


def _load(name):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def mox():
    return _load("moxfield_deck.json")


@pytest.fixture
def arch():
    return _load("archidekt_deck.json")


@pytest.fixture
def scry():
    return _load("scryfall_card.json")


def _resp(status=200, body=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body
    if status >= 500 or status == 429:
        r.raise_for_status.side_effect = httpx.HTTPStatusError(
            str(status), request=MagicMock(), response=r)
    return r


def _client(*responses, side_effect=None):
    """Patch AsyncClient so .get returns the responses in order (or raises side_effect)."""
    patcher = patch("src.services.deck_service.httpx.AsyncClient")
    cls = patcher.start()
    client = AsyncMock()
    if side_effect is not None:
        client.get.side_effect = side_effect
    else:
        client.get.side_effect = list(responses)
    cls.return_value.__aenter__.return_value = client
    return patcher, client


@pytest.fixture
def no_sleep():
    with patch("asyncio.sleep", new_callable=AsyncMock) as s:
        yield s


class TestParseDeckUrl:
    @pytest.mark.parametrize("url,expected", [
        (f"https://moxfield.com/decks/{MOX_ID}", ("Moxfield", MOX_ID)),
        (f"https://www.moxfield.com/decks/{MOX_ID}", ("Moxfield", MOX_ID)),
        (f"http://moxfield.com/decks/{MOX_ID}/", ("Moxfield", MOX_ID)),
        (f"moxfield.com/decks/{MOX_ID}", ("Moxfield", MOX_ID)),
        (f"https://moxfield.com/decks/{MOX_ID}?utm=1#x", ("Moxfield", MOX_ID)),
        (f"<https://moxfield.com/decks/{MOX_ID}>", ("Moxfield", MOX_ID)),
        (f"  https://MOXFIELD.com/decks/{MOX_ID}  ", ("Moxfield", MOX_ID)),
        ("https://archidekt.com/decks/123456", ("Archidekt", "123456")),
        ("https://www.archidekt.com/decks/123456/my-deck-slug", ("Archidekt", "123456")),
        ("archidekt.com/decks/123456#primer", ("Archidekt", "123456")),
    ])
    def test_valid(self, url, expected):
        assert parse_deck_url(url) == expected

    @pytest.mark.parametrize("url", [
        "",
        "garbage",
        "not a url at all",
        f"https://evil.com/decks/{MOX_ID}",
        f"https://moxfield.com.evil.com/decks/{MOX_ID}",
        f"https://evil.com/moxfield.com/decks/{MOX_ID}",
        f"https://user@evil.com:80@moxfield.com/decks/{MOX_ID}x",
        f"ftp://moxfield.com/decks/{MOX_ID}",
        f"javascript:moxfield.com/decks/{MOX_ID}",
        "https://moxfield.com/users/someone",
        "https://moxfield.com/decks/",
        "https://moxfield.com/decks/../../etc",
        "https://moxfield.com/decks/a%2Fb",
        "https://archidekt.com/decks/abc",
        "https://archidekt.com/decks/",
        "https://archidekt.com/api/decks/123/",
        "https://api2.moxfield.com/v2/decklists/abc",
    ])
    def test_invalid(self, url):
        assert parse_deck_url(url) is None


class TestFetchDeckArchidekt:
    async def test_fixture_summary(self, arch):
        p, client = _client(_resp(200, arch))
        try:
            d = await fetch_deck("https://archidekt.com/decks/1/fun")
        finally:
            p.stop()
        assert d.title == "Fun With Fungus"
        assert d.fmt == "Commander / EDH"
        assert d.card_count == 17
        assert d.source == "Archidekt"
        assert d.url == "https://archidekt.com/decks/1"
        assert d.image_url == arch["featured"]
        assert d.commander is not None
        assert client.get.call_args[0][0] == "https://archidekt.com/api/decks/1/"
        assert client.get.call_args[1]["headers"]["User-Agent"] == "hightide-events-bot/0.2"

    async def test_custom_featured_preferred(self, arch):
        arch["customFeatured"] = "https://example.com/custom.png"
        p, _ = _client(_resp(200, arch))
        try:
            d = await fetch_deck("https://archidekt.com/decks/1")
        finally:
            p.stop()
        assert d.image_url == "https://example.com/custom.png"

    async def test_missing_image(self, arch):
        arch["customFeatured"] = ""
        arch["featured"] = ""
        p, _ = _client(_resp(200, arch))
        try:
            d = await fetch_deck("https://archidekt.com/decks/1")
        finally:
            p.stop()
        assert d.image_url is None

    async def test_unsafe_image_dropped(self, arch):
        arch["customFeatured"] = "javascript:alert(1)"
        arch["featured"] = ""
        p, _ = _client(_resp(200, arch))
        try:
            d = await fetch_deck("https://archidekt.com/decks/1")
        finally:
            p.stop()
        assert d.image_url is None

    async def test_maybeboard_and_sideboard_not_counted(self, arch):
        arch["cards"].append({"quantity": 5, "categories": ["Maybeboard"],
                              "card": {"oracleCard": {"name": "X"}}})
        arch["cards"].append({"quantity": 3, "categories": ["Sideboard"],
                              "card": {"oracleCard": {"name": "Y"}}})
        p, _ = _client(_resp(200, arch))
        try:
            d = await fetch_deck("https://archidekt.com/decks/1")
        finally:
            p.stop()
        assert d.card_count == 17

    async def test_no_commander_and_unknown_format(self, arch):
        for c in arch["cards"]:
            c["categories"] = None
        arch["deckFormat"] = 999
        p, _ = _client(_resp(200, arch))
        try:
            d = await fetch_deck("https://archidekt.com/decks/1")
        finally:
            p.stop()
        assert d.commander is None
        assert d.fmt == "Unknown"


class TestFetchDeckMoxfield:
    async def test_fixture_summary_with_commander_image(self, mox, scry):
        p, client = _client(_resp(200, mox), _resp(200, scry))
        try:
            d = await fetch_deck(f"https://moxfield.com/decks/{MOX_ID}")
        finally:
            p.stop()
        assert d.title == mox["name"]
        assert d.fmt == "Commander"
        assert d.card_count == 100
        assert d.source == "Moxfield"
        assert d.url == f"https://moxfield.com/decks/{MOX_ID}"
        assert d.commander == "Atraxa, Praetors' Voice"
        assert d.image_url == scry["image_uris"]["normal"]
        urls = [c[0][0] for c in client.get.call_args_list]
        assert urls == [f"https://api2.moxfield.com/v2/decklists/{MOX_ID}",
                        f"https://api.scryfall.com/cards/{SFID}"]
        for c in client.get.call_args_list:
            assert c[1]["headers"]["User-Agent"] == "hightide-events-bot/0.2"

    async def test_no_commander_no_image_lookup(self, mox):
        mox["commanders"] = {}
        mox["commandersCount"] = 0
        mox["format"] = "standard"
        p, client = _client(_resp(200, mox))
        try:
            d = await fetch_deck(f"https://moxfield.com/decks/{MOX_ID}")
        finally:
            p.stop()
        assert d.image_url is None and d.commander is None
        assert d.card_count == 99
        assert client.get.call_count == 1

    async def test_scryfall_404_gives_no_image(self, mox):
        p, _ = _client(_resp(200, mox), _resp(404, {}))
        try:
            d = await fetch_deck(f"https://moxfield.com/decks/{MOX_ID}")
        finally:
            p.stop()
        assert d is not None and d.image_url is None

    async def test_scryfall_failure_is_best_effort(self, mox):
        p, _ = _client(side_effect=[_resp(200, mox), httpx.TransportError("down")])
        try:
            d = await fetch_deck(f"https://moxfield.com/decks/{MOX_ID}", retries=False)
        finally:
            p.stop()
        assert d is not None and d.image_url is None and d.title == mox["name"]

    async def test_bad_scryfall_id_not_requested(self, mox):
        key = next(iter(mox["commanders"]))
        mox["commanders"][key]["card"]["scryfall_id"] = "../../cards/random"
        p, client = _client(_resp(200, mox))
        try:
            d = await fetch_deck(f"https://moxfield.com/decks/{MOX_ID}")
        finally:
            p.stop()
        assert d.image_url is None
        assert client.get.call_count == 1


class TestFetchDeckFailures:
    @pytest.mark.parametrize("url", [
        "https://evil.com/decks/123", "garbage", "https://moxfield.com/users/x", ""])
    async def test_bad_url_no_network(self, url):
        p, client = _client()
        try:
            assert await fetch_deck(url) is None
        finally:
            p.stop()
        client.get.assert_not_called()

    @pytest.mark.parametrize("status", [404, 403, 401, 400, 410])
    async def test_non_200_is_none(self, status):
        p, client = _client(_resp(status, {}))
        try:
            assert await fetch_deck("https://archidekt.com/decks/1") is None
        finally:
            p.stop()
        assert client.get.call_count == 1

    async def test_malformed_json_is_none(self):
        r = _resp(200)
        r.json.side_effect = ValueError("bad json")
        p, _ = _client(r)
        try:
            assert await fetch_deck("https://archidekt.com/decks/1") is None
        finally:
            p.stop()

    async def test_non_dict_json_is_none(self):
        p, _ = _client(_resp(200, ["nope"]))
        try:
            assert await fetch_deck("https://archidekt.com/decks/1") is None
        finally:
            p.stop()

    async def test_transport_error_propagates(self, no_sleep):
        p, _ = _client(side_effect=httpx.TransportError("net"))
        try:
            with pytest.raises(httpx.TransportError):
                await fetch_deck("https://archidekt.com/decks/1")
        finally:
            p.stop()

    async def test_5xx_propagates(self, no_sleep):
        p, _ = _client(side_effect=lambda *a, **k: _resp(503))
        try:
            with pytest.raises(httpx.HTTPStatusError):
                await fetch_deck("https://archidekt.com/decks/1")
        finally:
            p.stop()


class TestRetryAndTimeout:
    async def test_5xx_retries_three_times(self, no_sleep):
        p, client = _client(side_effect=lambda *a, **k: _resp(503))
        try:
            with pytest.raises(httpx.HTTPStatusError):
                await fetch_deck("https://archidekt.com/decks/1", retries=True)
        finally:
            p.stop()
        assert client.get.call_count == 3
        assert no_sleep.await_count == 2

    async def test_retries_false_single_attempt(self, no_sleep):
        p, client = _client(side_effect=lambda *a, **k: _resp(503))
        try:
            with pytest.raises(httpx.HTTPStatusError):
                await fetch_deck("https://archidekt.com/decks/1", retries=False)
        finally:
            p.stop()
        assert client.get.call_count == 1
        no_sleep.assert_not_awaited()

    async def test_transport_error_retried(self, no_sleep, arch):
        p, client = _client(httpx.TransportError("x"), _resp(200, arch))
        try:
            d = await fetch_deck("https://archidekt.com/decks/1", retries=True)
        finally:
            p.stop()
        assert d is not None and client.get.call_count == 2

    async def test_404_never_retries(self, no_sleep):
        p, client = _client(_resp(404, {}))
        try:
            assert await fetch_deck("https://archidekt.com/decks/1", retries=True) is None
        finally:
            p.stop()
        assert client.get.call_count == 1

    async def test_timeout_passed_and_default(self, arch):
        p, client = _client(_resp(200, arch), _resp(200, arch))
        try:
            await fetch_deck("https://archidekt.com/decks/1", timeout=2.5)
            await fetch_deck("https://archidekt.com/decks/1")
        finally:
            p.stop()
        assert client.get.call_args_list[0][1]["timeout"] == 2.5
        assert client.get.call_args_list[1][1]["timeout"] == 15.0

    async def test_image_lookup_uses_remaining_budget(self, mox, scry):
        p, client = _client(_resp(200, mox), _resp(200, scry))
        try:
            with patch("src.services.deck_service._monotonic", side_effect=[0.0, 0.5]):
                await fetch_deck(f"https://moxfield.com/decks/{MOX_ID}", retries=False, timeout=2.5)
        finally:
            p.stop()
        deck_t = client.get.call_args_list[0][1]["timeout"]
        img_t = client.get.call_args_list[1][1]["timeout"]
        assert deck_t == 2.5 and img_t == 2.0

    async def test_image_lookup_skipped_when_budget_spent(self, mox):
        p, client = _client(_resp(200, mox))
        try:
            with patch("src.services.deck_service._monotonic", side_effect=[0.0, 3.0]):
                d = await fetch_deck(f"https://moxfield.com/decks/{MOX_ID}",
                                     retries=False, timeout=2.5)
        finally:
            p.stop()
        assert d.image_url is None and client.get.call_count == 1


class TestScryfallById:
    async def test_hit_and_miss(self, scry):
        p, client = _client(_resp(200, scry), _resp(404, {}))
        try:
            assert (await scryfall_card_by_id(SFID))["name"] == "Lightning Bolt"
            assert await scryfall_card_by_id(SFID) is None
        finally:
            p.stop()
        assert client.get.call_args[0][0] == f"https://api.scryfall.com/cards/{SFID}"

    async def test_invalid_id_returns_none_without_network(self):
        p, client = _client()
        try:
            assert await scryfall_card_by_id("not-a-uuid") is None
        finally:
            p.stop()
        client.get.assert_not_called()

    async def test_5xx_propagates(self, no_sleep):
        p, client = _client(side_effect=lambda *a, **k: _resp(500))
        try:
            with pytest.raises(httpx.HTTPStatusError):
                await scryfall_card_by_id(SFID, retries=True)
        finally:
            p.stop()
        assert client.get.call_count == 3


class TestBuildDeckEmbed:
    def _deck(self, **kw):
        base = dict(title="My Deck", fmt="Commander", card_count=100,
                    image_url="https://img.example.com/a.jpg",
                    url="https://moxfield.com/decks/abc", source="Moxfield")
        base.update(kw)
        return DeckSummary(**base)

    def test_basic(self):
        e = build_deck_embed(self._deck())
        assert isinstance(e, discord.Embed)
        assert e.title == "My Deck"
        assert e.url == "https://moxfield.com/decks/abc"
        assert e.image.url == "https://img.example.com/a.jpg"
        assert e.description == "Moxfield · Commander · 100 cards"

    def test_no_image(self):
        e = build_deck_embed(self._deck(image_url=None))
        assert e.image.url is None

    def test_commander_line(self):
        e = build_deck_embed(self._deck(commander="Atraxa"))
        assert e.description == "Moxfield · Commander · 100 cards\nCommander: Atraxa"

    def test_title_and_commander_escaped(self):
        e = build_deck_embed(self._deck(title="**x** @everyone [a](http://e.com)",
                                        commander="_c_ <@123>"))
        assert "**x**" not in e.title
        assert "@everyone" not in e.title.replace("@\u200beveryone", "")
        assert "[a](" not in e.title
        assert "<@123>" not in e.description

    def test_long_title_truncated(self):
        e = build_deck_embed(self._deck(title="*" * 1000))
        assert len(e.title) <= 256

    def test_unsafe_url_dropped(self):
        e = build_deck_embed(self._deck(url="javascript:x", image_url="javascript:y"))
        assert not e.url
        assert e.image.url is None

    def test_singular_card(self):
        e = build_deck_embed(self._deck(card_count=1))
        assert "1 card" in e.description and "1 cards" not in e.description

    async def test_fixture_embeds_both_sources(self, mox, arch, scry):
        p, _ = _client(_resp(200, mox), _resp(200, scry), _resp(200, arch))
        try:
            m = build_deck_embed(await fetch_deck(f"https://moxfield.com/decks/{MOX_ID}"))
            a = build_deck_embed(await fetch_deck("https://archidekt.com/decks/1"))
        finally:
            p.stop()
        assert m.description.startswith("Moxfield · Commander · 100 cards")
        assert m.image.url == scry["image_uris"]["normal"]
        assert a.description.startswith("Archidekt · Commander / EDH · 17 cards")
        assert a.image.url == arch["featured"]


class TestDeckCommandSurface:
    def test_deck_in_tree_public_guild_only(self):
        from src.bot import create_bot
        cmds = {c.name: c for c in create_bot().tree.get_commands()}
        assert "deck" in cmds
        assert cmds["deck"].default_permissions is None
        assert cmds["deck"].guild_only is True


def _interaction():
    i = AsyncMock(spec=discord.Interaction)
    i.response = AsyncMock()
    return i


class TestDeckCommandHandler:
    def _cog(self):
        from src.discord_ui.commands_roles import RolesCog
        return RolesCog(AsyncMock())

    def _summary(self):
        return DeckSummary(title="T", fmt="Commander", card_count=100, image_url=None,
                           url="https://moxfield.com/decks/abc", source="Moxfield")

    async def test_success_public(self):
        cog, i = self._cog(), _interaction()
        with patch("src.discord_ui.commands_roles.fetch_deck", new_callable=AsyncMock) as f:
            f.return_value = self._summary()
            await cog.deck_lookup.callback(cog, i, "https://moxfield.com/decks/abc")
        f.assert_called_once_with("https://moxfield.com/decks/abc", retries=False, timeout=2.5)
        kw = i.response.send_message.call_args[1]
        assert kw["ephemeral"] is False and isinstance(kw["embed"], discord.Embed)

    async def test_none_is_ephemeral_check_link(self):
        cog, i = self._cog(), _interaction()
        with patch("src.discord_ui.commands_roles.fetch_deck", new_callable=AsyncMock) as f:
            f.return_value = None
            await cog.deck_lookup.callback(cog, i, "https://moxfield.com/decks/abc")
        args, kw = i.response.send_message.call_args
        assert "check the link" in args[0] and kw["ephemeral"] is True

    @pytest.mark.parametrize("exc", [
        httpx.TransportError("x"),
        httpx.HTTPStatusError("500", request=MagicMock(), response=MagicMock()),
        RuntimeError("boom"),
    ])
    async def test_errors_ephemeral_trouble(self, exc):
        cog, i = self._cog(), _interaction()
        with patch("src.discord_ui.commands_roles.fetch_deck", new_callable=AsyncMock) as f:
            f.side_effect = exc
            await cog.deck_lookup.callback(cog, i, "https://archidekt.com/decks/1")
        args, kw = i.response.send_message.call_args
        assert "having trouble" in args[0] and kw["ephemeral"] is True
        i.response.send_message.assert_called_once()

    async def test_error_message_names_the_site(self):
        cog, i = self._cog(), _interaction()
        with patch("src.discord_ui.commands_roles.fetch_deck", new_callable=AsyncMock) as f:
            f.side_effect = httpx.TransportError("x")
            await cog.deck_lookup.callback(cog, i, "https://archidekt.com/decks/1")
        assert "Archidekt is having trouble" in i.response.send_message.call_args[0][0]
        i2 = _interaction()
        with patch("src.discord_ui.commands_roles.fetch_deck", new_callable=AsyncMock) as f:
            f.side_effect = httpx.TransportError("x")
            await cog.deck_lookup.callback(cog, i2, "https://moxfield.com/decks/1")
        assert "Moxfield is having trouble" in i2.response.send_message.call_args[0][0]

    async def test_url_too_long_no_fetch(self):
        cog, i = self._cog(), _interaction()
        with patch("src.discord_ui.commands_roles.fetch_deck", new_callable=AsyncMock) as f:
            await cog.deck_lookup.callback(cog, i, "https://moxfield.com/decks/" + "a" * 500)
        f.assert_not_called()
        args, kw = i.response.send_message.call_args
        assert "500" in args[0] and kw["ephemeral"] is True

    async def test_deck_wait_for_timeout_reply(self, monkeypatch):
        """Test that /deck handler wraps fetch_deck in asyncio.wait_for and replies quickly."""
        import asyncio
        # Patch the timeout constant to 0.05s for fast test execution
        monkeypatch.setattr("src.discord_ui.commands_roles._LOOKUP_TIMEOUT", 0.05)
        
        cog, i = self._cog(), _interaction()
        async def hang(*args, **kwargs):
            await asyncio.sleep(10)  # Simulate hanging fetch
        with patch("src.discord_ui.commands_roles.fetch_deck", new_callable=AsyncMock) as f:
            f.side_effect = hang
            start = asyncio.get_event_loop().time()
            await cog.deck_lookup.callback(cog, i, "https://moxfield.com/decks/abc")
            elapsed = asyncio.get_event_loop().time() - start
        # wait_for should cancel after ~0.05s, reply should be fast
        assert elapsed < 0.5
        args, kw = i.response.send_message.call_args
        assert "too long" in args[0] and kw["ephemeral"] is True

    async def test_deck_check_link_mentions_blocking(self):
        """Test that deck=None reply mentions site may be blocking us."""
        cog, i = self._cog(), _interaction()
        with patch("src.discord_ui.commands_roles.fetch_deck", new_callable=AsyncMock) as f:
            f.return_value = None
            await cog.deck_lookup.callback(cog, i, "https://moxfield.com/decks/abc")
        args, kw = i.response.send_message.call_args
        assert "blocking us" in args[0] and kw["ephemeral"] is True


class TestMoxfieldBlockLogging:
    async def test_non_200_non_404_logs_warning(self, caplog):
        """Test that Moxfield 403 logs a warning."""
        p, _ = _client(_resp(403, {}))
        try:
            with caplog.at_level(logging.WARNING):
                d = await fetch_deck("https://moxfield.com/decks/test123")
        finally:
            p.stop()
        assert d is None
        assert "returned 403" in caplog.text
        assert "test123" in caplog.text

    async def test_archidekt_non_200_does_not_log_warning(self, caplog):
        """Test that Archidekt 403 does not log a warning (per site-specific policy)."""
        p, _ = _client(_resp(403, {}))
        try:
            with caplog.at_level(logging.WARNING):
                d = await fetch_deck("https://archidekt.com/decks/1")
        finally:
            p.stop()
        assert d is None
        # Archidekt non-200 should not trigger extra logging (unlike Moxfield)
        assert "returned" not in caplog.text or "archidekt" not in caplog.text.lower()


class TestImageLookupRobustness:
    async def test_scryfall_bad_json_gives_no_image(self, mox):
        """Test that 200 with bad JSON from Scryfall doesn't raise."""
        r = _resp(200, mox)
        r_scry = _resp(200)
        r_scry.json.side_effect = ValueError("bad json")
        p, _ = _client(r, r_scry)
        try:
            d = await fetch_deck(f"https://moxfield.com/decks/{MOX_ID}", retries=False)
        finally:
            p.stop()
        assert d is not None
        assert d.image_url is None
        assert d.title == mox["name"]

    async def test_scryfall_http_error_caught(self, mox):
        """Test that httpx.HTTPError during image lookup is caught."""
        p, _ = _client(side_effect=[_resp(200, mox), httpx.ConnectError("no net")])
        try:
            d = await fetch_deck(f"https://moxfield.com/decks/{MOX_ID}", retries=False)
        finally:
            p.stop()
        assert d is not None and d.image_url is None

    async def test_scryfall_json_list_response_caught(self, mox):
        """Test that JSON list (non-dict) from Scryfall raises AttributeError and is caught."""
        r = _resp(200, mox)
        r_scry = _resp(200, ["card1", "card2"])
        p, _ = _client(r, r_scry)
        try:
            d = await fetch_deck(f"https://moxfield.com/decks/{MOX_ID}", retries=False)
        finally:
            p.stop()
        # Should handle the AttributeError and return deck without image
        assert d is not None
        assert d.image_url is None
        assert d.title == mox["name"]


class TestLengthCaps:
    async def test_commander_truncated_at_100_chars(self, mox, scry):
        """Test that commander name is capped at 100 characters."""
        # Get the actual key from the commanders dict (the card name)
        cmdr_key = next(iter(mox["commanders"]))
        mox["commanders"][cmdr_key]["card"]["name"] = "A" * 150
        p, _ = _client(_resp(200, mox), _resp(200, scry))
        try:
            d = await fetch_deck(f"https://moxfield.com/decks/{MOX_ID}")
        finally:
            p.stop()
        assert d.commander == "A" * 100
        assert len(d.commander) == 100

    async def test_format_truncated_at_40_chars(self, mox, scry):
        """Test that format name is capped at 40 characters."""
        # Set a very long format name that will exceed 40 chars after processing
        mox["format"] = "very_long_custom_format_name_that_is_definitely_over_forty_characters"
        p, _ = _client(_resp(200, mox), _resp(200, scry))
        try:
            d = await fetch_deck(f"https://moxfield.com/decks/{MOX_ID}")
        finally:
            p.stop()
        assert len(d.fmt) <= 40


class TestMoxfieldUrlVariants:
    async def test_moxfield_with_primer_segment(self):
        """Test that Moxfield URLs with trailing segment like /primer work."""
        result = parse_deck_url("https://moxfield.com/decks/oEWXWHM5eEGMmopExLWRCA/primer")
        assert result == ("Moxfield", "oEWXWHM5eEGMmopExLWRCA")

    async def test_moxfield_with_custom_segment(self):
        """Test that Moxfield URLs with any trailing segment work."""
        result = parse_deck_url("https://moxfield.com/decks/abc123/my-custom-slug")
        assert result == ("Moxfield", "abc123")

    async def test_moxfield_no_double_slash_in_segment(self):
        """Test that only one optional trailing segment is accepted."""
        # This should work: /decks/id/segment
        result = parse_deck_url("https://moxfield.com/decks/abc123/primer")
        assert result == ("Moxfield", "abc123")
        # But /decks/id/seg1/seg2 should not work (too many segments)
        result = parse_deck_url("https://moxfield.com/decks/abc123/primer/extra")
        assert result is None


class TestArchidektHttpsRequirement:
    async def test_archidekt_https_image_accepted(self, arch):
        """Test that HTTPS Archidekt image URLs are accepted."""
        arch["customFeatured"] = "https://example.com/custom.png"
        p, _ = _client(_resp(200, arch))
        try:
            d = await fetch_deck("https://archidekt.com/decks/1")
        finally:
            p.stop()
        assert d.image_url == "https://example.com/custom.png"

    async def test_archidekt_http_image_rejected(self, arch):
        """Test that HTTP (non-HTTPS) image URLs are rejected."""
        arch["customFeatured"] = "http://example.com/custom.png"
        arch["featured"] = "http://bad.com/image.png"
        p, _ = _client(_resp(200, arch))
        try:
            d = await fetch_deck("https://archidekt.com/decks/1")
        finally:
            p.stop()
        assert d.image_url is None
