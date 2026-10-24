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
MOX_ID = "_1xlt0eAuEmUeV1DMleOQA"
SFID = "3d6d6944-a364-41c2-b824-7a1bf6ad0d1e"


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
        "https://api2.moxfield.com/v2/decks/all/abc",
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
        assert d.commander == "Thelon of Havenwood"
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
        assert d.commander == "The Wise Mothman"
        assert d.image_url == scry["image_uris"]["normal"]
        urls = [c[0][0] for c in client.get.call_args_list]
        assert urls == [f"https://api2.moxfield.com/v2/decks/all/{MOX_ID}",
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
        assert d.title == mox["name"] and d.image_url is None
        assert d.commander == "The Wise Mothman"

    async def test_scryfall_failure_is_best_effort(self, mox):
        p, _ = _client(side_effect=[_resp(200, mox), httpx.TransportError("down")])
        try:
            d = await fetch_deck(f"https://moxfield.com/decks/{MOX_ID}", retries=False)
        finally:
            p.stop()
        assert d.image_url is None and d.title == mox["name"]

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
        assert d.title == "Fun With Fungus" and client.get.call_count == 2

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
        # "[" is escaped twice by _md_escape (escape_markdown + bracket pass): "\\" then a literal "[".
        assert e.title == "\\*\\*x\\*\\* @\u200beveryone \\\\[a\\]\\(http://e.com\\)"
        assert e.description == ("Moxfield \u00b7 Commander \u00b7 100 cards\n"
                                 "Commander: \\_c\\_ \\<@123\\>")

    def test_title_capped_at_100_chars_before_escaping(self):
        assert build_deck_embed(self._deck(title="a" * 150)).title == "a" * 100
        assert build_deck_embed(self._deck(title="a" * 100)).title == "a" * 100
        assert build_deck_embed(self._deck(title="*" * 1000)).title == "\\*" * 100

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
        assert args[0] == "Couldn't read that deck \u2014 check the link (or the site may be blocking us)."
        assert kw == {"ephemeral": True}

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


def _deck_records(caplog):
    return [(r.levelname, r.getMessage()) for r in caplog.records if r.name == "hightide.deck_service"]


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
        assert _deck_records(caplog) == [("WARNING", "Moxfield API returned 403 for deck test123")]

    async def test_archidekt_non_200_logs_warning(self, caplog):
        """Test that Archidekt 403 logs a warning (consistent policy for both sources)."""
        p, _ = _client(_resp(403, {}))
        try:
            with caplog.at_level(logging.WARNING):
                d = await fetch_deck("https://archidekt.com/decks/1")
        finally:
            p.stop()
        assert d is None
        assert _deck_records(caplog) == [("WARNING", "Archidekt API returned 403 for deck 1")]


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
        assert d.title == mox["name"] and d.image_url is None

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

    async def test_format_truncated_at_30_chars(self, mox, scry):
        """Test that format name is capped at 30 characters."""
        # Set a very long format name that will exceed 30 chars after processing
        mox["format"] = "very_long_custom_format_name_that_is_definitely_over_thirty_characters"
        p, _ = _client(_resp(200, mox), _resp(200, scry))
        try:
            d = await fetch_deck(f"https://moxfield.com/decks/{MOX_ID}")
        finally:
            p.stop()
        assert d.fmt == "Very Long Custom Format Name T"


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


class TestParseDeckUrlBoundaries:
    @pytest.mark.parametrize("n,ok", [(1, True), (64, True), (65, False)])
    def test_moxfield_id_length(self, n, ok):
        url = "https://moxfield.com/decks/" + "a" * n
        assert parse_deck_url(url) == (("Moxfield", "a" * n) if ok else None)

    @pytest.mark.parametrize("n,ok", [(1, True), (12, True), (13, False)])
    def test_archidekt_id_length(self, n, ok):
        url = "https://archidekt.com/decks/" + "7" * n
        assert parse_deck_url(url) == (("Archidekt", "7" * n) if ok else None)

    def test_moxfield_allows_dash_and_underscore(self):
        assert parse_deck_url("https://moxfield.com/decks/abc_DEF-1") == ("Moxfield", "abc_DEF-1")

    @pytest.mark.parametrize("url", [
        "https://archidekt.com/decks/123/slug/extra",
        "https://archidekt.com/decks/123/a/b/",
        "https://moxfield.com/decks/abc/primer/extra",
        "https://archidekt.com/decks/123/../456",
    ])
    def test_extra_path_segments_rejected(self, url):
        assert parse_deck_url(url) is None

    @pytest.mark.parametrize("url", [
        f"https://moxfield.com:443/decks/{MOX_ID}",
        f"https://moxfield.com:8080/decks/{MOX_ID}",
        "https://archidekt.com:443/decks/123",
        f"https://moxfield.com:abc/decks/{MOX_ID}",
        "https://archidekt.com:99999/decks/123",
    ])
    def test_explicit_ports_rejected(self, url):
        assert parse_deck_url(url) is None

    @pytest.mark.parametrize("digits", [
        "\u0661\u0662\u0663",  # Arabic-Indic
        "\uff11\uff12\uff13",  # fullwidth
        "12\u0663",
    ])
    def test_unicode_digits_rejected_for_archidekt(self, digits):
        assert parse_deck_url(f"https://archidekt.com/decks/{digits}") is None

    def test_ascii_leading_zeros_kept_verbatim(self):
        assert parse_deck_url("https://archidekt.com/decks/0123") == ("Archidekt", "0123")

    @pytest.mark.parametrize("url,expected", [
        ("https://moxfield.com/decks/abc\n/x", ("Moxfield", "abc")),  # urlparse drops raw newlines
        ("https://moxfield.com/decks/abc%0a", None),
        ("https://moxfield.com/decks/abc%00", None),
        ("https://moxfield.com/decks/ab c", None),
    ])
    def test_control_characters(self, url, expected):
        assert parse_deck_url(url) == expected

    @pytest.mark.parametrize("url", [
        "https://moxfield.com@evil.com/decks/abc",
        "https://user:pw@moxfield.com/decks/abc",
        "https://evil.com@moxfield.com/decks/abc",
    ])
    def test_userinfo_rejected(self, url):
        assert parse_deck_url(url) is None

    def test_whitespace_and_angle_brackets_stripped(self):
        assert parse_deck_url("  <https://archidekt.com/decks/5>  ") == ("Archidekt", "5")


class TestScryfallByIdTrailingNewline:
    async def test_trailing_newline_uuid_rejected_without_request(self):
        p, client = _client()
        try:
            assert await scryfall_card_by_id(SFID + "\n") is None
        finally:
            p.stop()
        client.get.assert_not_called()


class TestFetchDeckErrorStatuses:
    """fetch_deck: the deck request's 429 is retried; other 4xx are a quiet None (+ warning)."""

    async def test_429_retried_then_success(self, no_sleep, arch):
        p, client = _client(_resp(429), _resp(200, arch))
        try:
            d = await fetch_deck("https://archidekt.com/decks/1", retries=True)
        finally:
            p.stop()
        assert d.title == "Fun With Fungus"
        assert client.get.call_count == 2 and no_sleep.await_count == 1

    async def test_429_exhausted_raises(self, no_sleep):
        p, client = _client(side_effect=lambda *a, **k: _resp(429))
        try:
            with pytest.raises(httpx.HTTPStatusError) as ei:
                await fetch_deck("https://archidekt.com/decks/1", retries=True)
        finally:
            p.stop()
        assert ei.value.response.status_code == 429 and client.get.call_count == 3

    async def test_5xx_then_success(self, no_sleep, arch):
        p, client = _client(_resp(502), _resp(503), _resp(200, arch))
        try:
            d = await fetch_deck("https://archidekt.com/decks/1")
        finally:
            p.stop()
        assert d.card_count == 17 and client.get.call_count == 3

    @pytest.mark.parametrize("status", [400, 403])
    async def test_deck_4xx_is_none_logged_not_retried(self, no_sleep, caplog, status):
        p, client = _client(_resp(status, {}))
        try:
            with caplog.at_level(logging.DEBUG, logger="hightide.deck_service"):
                assert await fetch_deck(f"https://moxfield.com/decks/{MOX_ID}") is None
        finally:
            p.stop()
        assert client.get.call_count == 1
        assert _deck_records(caplog) == [("WARNING", f"Moxfield API returned {status} for deck {MOX_ID}")]

    async def test_404_does_not_log(self, caplog):
        p, _ = _client(_resp(404, {}))
        try:
            with caplog.at_level(logging.DEBUG, logger="hightide.deck_service"):
                assert await fetch_deck("https://archidekt.com/decks/1") is None
        finally:
            p.stop()
        assert _deck_records(caplog) == []

    @pytest.mark.parametrize("status", [400, 403, 429])
    async def test_image_lookup_status_errors_are_best_effort(self, mox, no_sleep, status):
        p, _ = _client(_resp(200, mox), *[_resp(status, {})] * 3)
        try:
            d = await fetch_deck(f"https://moxfield.com/decks/{MOX_ID}", retries=False)
        finally:
            p.stop()
        assert d.title == mox["name"] and d.image_url is None

    async def test_image_lookup_failure_logs_warning_with_repr(self, mox, caplog):
        p, _ = _client(side_effect=[_resp(200, mox), httpx.ConnectError("no net")])
        try:
            with caplog.at_level(logging.DEBUG, logger="hightide.deck_service"):
                await fetch_deck(f"https://moxfield.com/decks/{MOX_ID}", retries=False)
        finally:
            p.stop()
        assert _deck_records(caplog) == [(
            "WARNING", f"Scryfall image lookup failed for deck {MOX_ID}: ConnectError('no net')")]

    async def test_image_lookup_success_and_404_do_not_log(self, mox, scry, caplog):
        p, _ = _client(_resp(200, mox), _resp(404, {}), _resp(200, mox), _resp(200, scry))
        try:
            with caplog.at_level(logging.DEBUG, logger="hightide.deck_service"):
                await fetch_deck(f"https://moxfield.com/decks/{MOX_ID}", retries=False)
                await fetch_deck(f"https://moxfield.com/decks/{MOX_ID}", retries=False)
        finally:
            p.stop()
        assert _deck_records(caplog) == []


class TestImageBudget:
    URL = f"https://moxfield.com/decks/{MOX_ID}"

    async def _run(self, mox, scry, elapsed, **kw):
        p, client = _client(_resp(200, mox), _resp(200, scry))
        try:
            with patch("src.services.deck_service._monotonic", side_effect=[0.0, elapsed]):
                d = await fetch_deck(self.URL, timeout=2.5, **kw)
        finally:
            p.stop()
        return d, client

    def test_min_image_budget_value(self):
        from src.services import deck_service
        assert deck_service._MIN_IMAGE_BUDGET == 0.25

    async def test_remaining_exactly_min_budget_does_lookup(self, mox, scry):
        d, client = await self._run(mox, scry, 2.25, retries=False)
        assert client.get.call_count == 2
        assert client.get.call_args_list[1][1]["timeout"] == 0.25
        assert d.image_url == scry["image_uris"]["normal"]

    async def test_remaining_just_below_min_budget_skips_lookup(self, mox, scry):
        d, client = await self._run(mox, scry, 2.2501, retries=False)
        assert client.get.call_count == 1
        assert d.image_url is None and d.commander == "The Wise Mothman"

    async def test_negative_remaining_skips_lookup(self, mox, scry):
        _, client = await self._run(mox, scry, 10.0, retries=False)
        assert client.get.call_count == 1

    async def test_retries_true_image_lookup_retries_with_same_remaining_timeout(self, mox, scry, no_sleep):
        # Pins the documented behavior: the remainder is computed once; each retry attempt reuses it.
        p, client = _client(_resp(200, mox), _resp(503), _resp(503), _resp(200, scry))
        try:
            with patch("src.services.deck_service._monotonic", side_effect=[0.0, 0.5]):
                d = await fetch_deck(self.URL, retries=True, timeout=2.5)
        finally:
            p.stop()
        assert d.image_url == scry["image_uris"]["normal"]
        timeouts = [c[1]["timeout"] for c in client.get.call_args_list]
        assert timeouts == [2.5, 2.0, 2.0, 2.0]

    async def test_retries_true_image_failures_exhaust_then_best_effort(self, mox, no_sleep):
        p, client = _client(_resp(200, mox), *[_resp(503)] * 3)
        try:
            d = await fetch_deck(self.URL, retries=True)
        finally:
            p.stop()
        assert d.image_url is None and d.title == mox["name"]
        assert client.get.call_count == 4

    def test_fetch_deck_docstring_describes_per_request_budget(self):
        assert "per-request budget" in fetch_deck.__doc__
        assert "can exceed" in fetch_deck.__doc__


class TestMoxfieldSummaryRobustness:
    URL = f"https://moxfield.com/decks/{MOX_ID}"

    async def _deck(self, mox, *responses, **kw):
        # Default 404 covers the commander-image lookup when a test doesn't supply one.
        p, client = _client(_resp(200, mox), *(responses or (_resp(404, {}),)))
        try:
            d = await fetch_deck(self.URL, retries=False, **kw)
        finally:
            p.stop()
        return d, client

    def _cmdr(self, mox):
        return mox["commanders"][next(iter(mox["commanders"]))]["card"]

    @pytest.mark.parametrize("title", [None, "", "   ", "\t\n", 123, ["x"]])
    async def test_blank_or_non_string_title_is_untitled(self, mox, title):
        mox["name"] = title
        d, _ = await self._deck(mox)
        assert d.title == "Untitled deck"

    @pytest.mark.parametrize("raw,shown", [
        ("weirdformat", "Weirdformat"), ("double_trouble", "Double Trouble"),
        ("two-words", "Two Words"), ("  STANDARD  ", "Standard"),
        ("historicbrawl", "Historic Brawl"), ("", "Unknown"), (None, "Unknown"),
        ("___", "Unknown"), (5, "5"),
    ])
    async def test_format_values(self, mox, raw, shown):
        mox["format"] = raw
        d, _ = await self._deck(mox)
        assert d.fmt == shown

    @pytest.mark.parametrize("bad", [True, False, -1, "5", 5.0, None, [], {}])
    async def test_bad_counts_count_as_zero(self, mox, bad):
        mox["mainboardCount"] = bad
        d, _ = await self._deck(mox)
        assert d.card_count == mox["commandersCount"]

    async def test_bad_commanders_count_also_zero(self, mox):
        mox["commandersCount"] = True
        d, _ = await self._deck(mox)
        assert d.card_count == 99

    def test_as_int(self):
        from src.services.deck_service import _as_int
        assert [_as_int(v) for v in (0, 7, True, False, -3, "4", 4.0, None)] == [0, 7, 0, 0, 0, 0, 0, 0]

    @pytest.mark.parametrize("name", [123, None, "", ["x"], {"a": 1}])
    async def test_non_string_commander_name_is_none(self, mox, name):
        self._cmdr(mox)["name"] = name
        d, _ = await self._deck(mox)
        assert d.commander is None

    @pytest.mark.parametrize("sfid", [123, None, ["x"], {"a": 1}, True])
    async def test_non_string_scryfall_id_skips_lookup(self, mox, sfid):
        self._cmdr(mox)["scryfall_id"] = sfid
        d, client = await self._deck(mox)
        assert d.image_url is None and client.get.call_count == 1
        assert d.commander == "The Wise Mothman"

    async def test_missing_scryfall_id_skips_lookup(self, mox):
        del self._cmdr(mox)["scryfall_id"]
        d, client = await self._deck(mox)
        assert client.get.call_count == 1 and d.image_url is None

    @pytest.mark.parametrize("cmdrs", [None, [], "x", 5, {"a": "notadict"}, {"a": {"card": "x"}}, {"a": {}}])
    async def test_malformed_commanders_tolerated(self, mox, cmdrs):
        mox["commanders"] = cmdrs
        d, client = await self._deck(mox)
        assert d.commander is None and d.image_url is None and client.get.call_count == 1

    async def test_malformed_first_commander_skipped_in_favor_of_next(self, mox):
        good = {"card": {"name": "Tymna the Weaver", "scryfall_id": SFID}}
        mox["commanders"] = {"x": "bad", "Tymna the Weaver": good}
        d, _ = await self._deck(mox, _resp(404, {}))
        assert d.commander == "Tymna the Weaver"

    async def test_partner_commanders_first_wins_single_lookup(self, mox, scry):
        mox["commanders"] = {
            "Tymna the Weaver": {"card": {"name": "Tymna the Weaver", "scryfall_id": SFID}},
            "Thrasios": {"card": {"name": "Thrasios, Triton Hero",
                                  "scryfall_id": "11111111-1111-1111-1111-111111111111"}},
        }
        mox["commandersCount"] = 2
        mox["mainboardCount"] = 98
        d, client = await self._deck(mox, _resp(200, scry))
        assert d.commander == "Tymna the Weaver"
        assert d.card_count == 100
        assert client.get.call_count == 2
        assert client.get.call_args_list[1][0][0] == f"https://api.scryfall.com/cards/{SFID}"

    async def test_commander_capped_at_100_exact_boundary(self, mox):
        self._cmdr(mox)["name"] = "B" * 100
        d, _ = await self._deck(mox, _resp(404, {}))
        assert d.commander == "B" * 100
        self._cmdr(mox)["name"] = "B" * 101
        d, _ = await self._deck(mox, _resp(404, {}))
        assert d.commander == "B" * 100

    async def test_summary_url_is_rebuilt_not_echoed(self, mox):
        p, _ = _client(_resp(200, mox), _resp(404, {}))
        try:
            d = await fetch_deck(f"https://www.moxfield.com/decks/{MOX_ID}/primer?x=1", retries=False)
        finally:
            p.stop()
        assert d.url == f"https://moxfield.com/decks/{MOX_ID}"


class TestMoxfieldDfcCommander:
    async def test_dfc_commander_uses_front_face_image(self, mox):
        dfc = _load("scryfall_dfc.json")
        assert "image_uris" not in dfc
        key = next(iter(mox["commanders"]))
        mox["commanders"][key]["card"].update(name=dfc["name"], scryfall_id=dfc["id"])
        p, client = _client(_resp(200, mox), _resp(200, dfc))
        try:
            d = await fetch_deck(f"https://moxfield.com/decks/{MOX_ID}")
        finally:
            p.stop()
        assert d.commander == "Delver of Secrets // Insectile Aberration"
        assert d.image_url == dfc["card_faces"][0]["image_uris"]["normal"]
        assert client.get.call_args_list[1][0][0] == f"https://api.scryfall.com/cards/{dfc['id']}"
        assert build_deck_embed(d).image.url == d.image_url

    async def test_dfc_with_unsafe_front_face_gives_no_image(self, mox):
        card = {"card_faces": [{"image_uris": {"normal": "javascript:x"}}]}
        p, _ = _client(_resp(200, mox), _resp(200, card))
        try:
            d = await fetch_deck(f"https://moxfield.com/decks/{MOX_ID}")
        finally:
            p.stop()
        assert d.image_url is None


class TestArchidektSummaryRobustness:
    async def _deck(self, data):
        p, _ = _client(_resp(200, data))
        try:
            return await fetch_deck("https://archidekt.com/decks/1")
        finally:
            p.stop()

    @staticmethod
    def _card(qty, cats, name="X"):
        return {"quantity": qty, "categories": cats, "card": {"oracleCard": {"name": name}}}

    @pytest.mark.parametrize("cards", [None, [], "nope", 5, {}])
    async def test_cards_null_or_malformed(self, arch, cards):
        arch["cards"] = cards
        d = await self._deck(arch)
        assert d.card_count == 0 and d.commander is None

    async def test_cards_null_key(self, arch):
        arch["cards"] = None
        d = await self._deck(arch)
        assert (d.card_count, d.commander, d.title) == (0, None, "Fun With Fungus")

    async def test_non_dict_entries_skipped(self, arch):
        arch["cards"] = ["x", None, 3, self._card(2, None)]
        assert (await self._deck(arch)).card_count == 2

    @pytest.mark.parametrize("qty", [True, False, -1, "3", 2.0, None])
    async def test_bad_quantities_count_as_zero(self, arch, qty):
        arch["cards"] = [self._card(qty, None), self._card(4, None)]
        assert (await self._deck(arch)).card_count == 4

    async def test_partner_commanders_first_wins(self, arch):
        arch["cards"] = [self._card(1, ["Commander"], "Tymna the Weaver"),
                         self._card(1, ["Commander"], "Thrasios, Triton Hero")]
        d = await self._deck(arch)
        assert d.commander == "Tymna the Weaver" and d.card_count == 2

    async def test_commander_category_case_insensitive(self, arch):
        arch["cards"] = [self._card(1, ["commander"], "Atraxa")]
        assert (await self._deck(arch)).commander == "Atraxa"

    @pytest.mark.parametrize("name", [123, None, "", ["x"]])
    async def test_non_string_commander_name(self, arch, name):
        arch["cards"] = [self._card(1, ["Commander"], name)]
        assert (await self._deck(arch)).commander is None

    @pytest.mark.parametrize("card", [None, "x", {}, {"oracleCard": None}, {"oracleCard": "x"}])
    async def test_malformed_card_object(self, arch, card):
        arch["cards"] = [{"quantity": 1, "categories": ["Commander"], "card": card}]
        d = await self._deck(arch)
        assert d.commander is None and d.card_count == 1

    async def test_commander_capped_at_100(self, arch):
        arch["cards"] = [self._card(1, ["Commander"], "C" * 100)]
        assert (await self._deck(arch)).commander == "C" * 100
        arch["cards"] = [self._card(1, ["Commander"], "C" * 101)]
        assert (await self._deck(arch)).commander == "C" * 100

    @pytest.mark.parametrize("title", [None, "", "  ", 5])
    async def test_blank_title(self, arch, title):
        arch["name"] = title
        assert (await self._deck(arch)).title == "Untitled deck"

    async def test_unknown_format_exact(self, arch):
        arch["deckFormat"] = None
        assert (await self._deck(arch)).fmt == "Unknown"
        arch["deckFormat"] = "3"
        assert (await self._deck(arch)).fmt == "Unknown"

    @pytest.mark.parametrize("key,fmt", [(1, "Standard"), (3, "Commander / EDH"), (13, "Brawl")])
    async def test_known_format_exact(self, arch, key, fmt):
        arch["deckFormat"] = key
        assert (await self._deck(arch)).fmt == fmt

    async def test_included_in_deck_false_category_excluded(self, arch):
        arch["categories"] = [{"name": "Tokens", "includedInDeck": False},
                              {"name": "Ramp", "includedInDeck": True}]
        arch["cards"] = [self._card(1, ["Ramp"]), self._card(4, ["Tokens"]),
                         self._card(2, ["Ramp", "Tokens"]), self._card(1, None)]
        assert (await self._deck(arch)).card_count == 2

    async def test_flag_beats_name_when_present(self, arch):
        arch["categories"] = [{"name": "Sideboard", "includedInDeck": True}]
        arch["cards"] = [self._card(3, ["Sideboard"]), self._card(1, None)]
        assert (await self._deck(arch)).card_count == 4

    async def test_name_fallback_when_flag_absent(self, arch):
        arch["categories"] = [{"name": "Maybeboard"}, {"name": "Sideboard", "includedInDeck": None},
                              {"name": "Ramp"}]
        arch["cards"] = [self._card(5, ["Maybeboard"]), self._card(3, ["sideboard"]),
                         self._card(2, ["Ramp"])]
        assert (await self._deck(arch)).card_count == 2

    async def test_name_fallback_when_categories_missing(self, arch):
        arch["cards"] = [self._card(5, ["Maybeboard"]), self._card(3, ["Sideboard"]), self._card(2, None)]
        assert (await self._deck(arch)).card_count == 2

    async def test_excluded_category_name_matching_is_case_insensitive(self, arch):
        arch["categories"] = [{"name": "Tokens", "includedInDeck": False}]
        arch["cards"] = [self._card(4, ["tokens"]), self._card(1, None)]
        assert (await self._deck(arch)).card_count == 1

    @pytest.mark.parametrize("cats", ["x", 5, [None, 3, "str"], {"a": 1}])
    async def test_malformed_categories_ignored(self, arch, cats):
        arch["categories"] = cats
        arch["cards"] = [self._card(5, ["Maybeboard"]), self._card(2, None)]
        assert (await self._deck(arch)).card_count == 2

    async def test_commander_in_excluded_category_not_picked(self, arch):
        arch["categories"] = [{"name": "Considering", "includedInDeck": False}]
        arch["cards"] = [self._card(1, ["Commander", "Considering"], "Hidden"),
                         self._card(1, ["Commander"], "Shown")]
        d = await self._deck(arch)
        assert d.commander == "Shown" and d.card_count == 1

    async def test_featured_fallback_order(self, arch):
        arch["customFeatured"] = "http://insecure.example/a.png"
        arch["featured"] = "https://good.example/b.png"
        assert (await self._deck(arch)).image_url == "https://good.example/b.png"
        arch["customFeatured"] = 5
        assert (await self._deck(arch)).image_url == "https://good.example/b.png"


class TestDeckHandlerEdges:
    def _cog(self):
        from src.discord_ui.commands_roles import RolesCog
        return RolesCog(AsyncMock())

    async def _run(self, url, **kw):
        cog, i = self._cog(), _interaction()
        with patch("src.discord_ui.commands_roles.fetch_deck", new_callable=AsyncMock, **kw) as f:
            await cog.deck_lookup.callback(cog, i, url)
        return f, i

    async def test_url_of_exactly_500_chars_is_fetched(self):
        url = "https://moxfield.com/decks/" + "a" * (500 - len("https://moxfield.com/decks/"))
        assert len(url) == 500
        f, i = await self._run(url, return_value=None)
        f.assert_called_once_with(url, retries=False, timeout=2.5)

    async def test_url_of_501_chars_rejected(self):
        url = "https://moxfield.com/decks/" + "a" * (501 - len("https://moxfield.com/decks/"))
        f, i = await self._run(url)
        f.assert_not_called()
        args, kw = i.response.send_message.call_args
        assert args[0] == "Deck link must be \u2264500 characters." and kw == {"ephemeral": True}

    async def test_padding_does_not_count_toward_500(self):
        url = "https://moxfield.com/decks/" + "a" * 473
        f, _ = await self._run("  " + url + "\n", return_value=None)
        f.assert_called_once_with(url, retries=False, timeout=2.5)

    @pytest.mark.parametrize("blank", ["", " ", "\t\n  ", " " * 600])
    async def test_blank_url_never_fetched(self, blank):
        f, i = await self._run(blank)
        f.assert_not_called()
        args, kw = i.response.send_message.call_args
        assert args[0] == "Give me a deck link." and kw == {"ephemeral": True}

    async def test_unparseable_url_error_uses_generic_site_name(self):
        f, i = await self._run("https://example.com/decks/1", side_effect=RuntimeError("boom"))
        args, kw = i.response.send_message.call_args
        assert args[0] == "The deck site is having trouble \u2014 try again shortly."
        assert kw == {"ephemeral": True}

    async def test_failure_logs_warning_with_site_and_repr(self, caplog):
        with caplog.at_level(logging.DEBUG, logger="hightide.discord_ui.commands_roles"):
            await self._run("https://archidekt.com/decks/1", side_effect=RuntimeError("boom"))
        recs = [(r.levelname, r.getMessage()) for r in caplog.records
                if r.name == "hightide.discord_ui.commands_roles"]
        assert recs == [("WARNING", "/deck lookup failed for Archidekt: RuntimeError('boom')")]

    async def test_none_and_success_do_not_log(self, caplog):
        with caplog.at_level(logging.DEBUG, logger="hightide.discord_ui.commands_roles"):
            await self._run("https://archidekt.com/decks/1", return_value=None)
        assert [r for r in caplog.records if r.name == "hightide.discord_ui.commands_roles"] == []

    async def test_service_timeout_below_wait_for_cap(self):
        from src.discord_ui import commands_roles
        f, _ = await self._run("https://archidekt.com/decks/1", return_value=None)
        assert f.call_args[1] == {"retries": False, "timeout": 2.5}
        assert f.call_args[1]["timeout"] < commands_roles._LOOKUP_TIMEOUT == 2.6

    async def test_direct_callback_twice_not_rate_limited(self):
        cog = self._cog()
        i = _interaction()
        with patch("src.discord_ui.commands_roles.fetch_deck", new_callable=AsyncMock) as f:
            f.return_value = None
            await cog.deck_lookup.callback(cog, i, "https://archidekt.com/decks/1")
            await cog.deck_lookup.callback(cog, i, "https://archidekt.com/decks/1")
        assert f.await_count == 2
