from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import discord
import httpx
import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError

from src.db import repositories
from src.db.models import Base, GuildConfig, ProductAlert
from src.db.session import (
    ensure_guild_config_v2_columns, get_engine, get_session_factory,
)
from src.discord_ui import commands_admin
from src.discord_ui.commands_admin import AdminCog
from src.scraping import fetcher
from src.scraping.fetcher import FetchError, fetch_products_by_search
from src.services import product_service, scheduler
from src.services.product_service import (
    check_new_products, parse_search_terms, post_product_alerts,
)

GUILD = 999
OTHER_GUILD = 1000
CHANNEL_ID = 555
BASE = "https://shop.example.com"
TZ = ZoneInfo("Australia/Perth")


def _product(pid, title="Preorder Foo Set", link=None, content="", image=None):
    p = {
        "id": pid,
        "title": {"rendered": title},
        "link": link or f"{BASE}/product/p{pid}/",
        "content": {"rendered": content},
    }
    if image:
        p["_embedded"] = {"wp:featuredmedia": [{"source_url": image}]}
    return p


def _shop(catalog: dict[str, list[dict]], requests=None):
    """Mock WP search API: term -> products. Unknown term -> empty list."""

    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        term = request.url.params.get("search", "")
        return httpx.Response(200, json=catalog.get(term, []))

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def patch_fetch(monkeypatch):
    """Route the service's search fetch through a mutable in-memory catalog."""
    state = {"catalog": {"preorder": [], "pre-order": []}, "requests": [],
             "store": [], "store_calls": []}
    real = fetch_products_by_search

    def fake(base_url, term, client=None):
        with _shop(state["catalog"], state["requests"]) as c:
            return real(base_url, term, client=c)

    def fake_store(base_url, term, client=None):
        state["store_calls"].append(term)
        if isinstance(state["store"], Exception):
            raise state["store"]
        return state["store"]

    monkeypatch.setattr(product_service, "fetch_products_by_search", fake)
    monkeypatch.setattr(product_service, "fetch_store_products_by_search", fake_store)
    return state


def _store(pid, price="1500", minor=2, prefix="$"):
    prices = {"currency_minor_unit": minor, "currency_prefix": prefix, "currency_suffix": ""}
    if price is not None:
        prices["price"] = price
    return {"id": pid, "name": "x", "prices": prices}


@pytest.fixture
def db(session):
    session.add(GuildConfig(guild_id=GUILD, new_stock_channel_id=CHANNEL_ID))
    session.commit()
    return session


def _alerted(session, guild=GUILD):
    return {r.external_id for r in session.execute(
        select(ProductAlert).where(ProductAlert.guild_id == guild)).scalars()
        if r.external_id.startswith("wp:")}


def _markers(session, guild=GUILD):
    return {r.external_id for r in session.execute(
        select(ProductAlert).where(ProductAlert.guild_id == guild)).scalars()
        if r.external_id.startswith("seeded")}


def _set_terms(session, terms):
    session.get(GuildConfig, GUILD).new_stock_terms = terms
    session.commit()


# --- fetcher: search ---

def test_fetch_products_by_search_uses_term_and_paginates():
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        page = int(request.url.params["page"])
        return httpx.Response(200, json=[{"id": page}], headers={"X-WP-TotalPages": "2"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        got = fetch_products_by_search(BASE + "/", "preorder", client=c)

    assert [p["id"] for p in got] == [1, 2]
    reqs = [r for r in seen if r.url.path.endswith("/product")]
    assert all(r.url.params["search"] == "preorder" for r in reqs)
    assert all(r.url.params["_embed"] == "1" for r in reqs)


def test_fetch_products_by_search_requires_https():
    with pytest.raises(FetchError):
        fetch_products_by_search("http://shop.example.com", "preorder")


def test_fetch_products_by_search_bad_shape_raises():
    with httpx.Client(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"code": "x"}))) as c:
        with pytest.raises(FetchError):
            fetch_products_by_search(BASE, "preorder", client=c)


def test_fetch_events_delegates_with_events_slug(monkeypatch):
    calls = []
    monkeypatch.setattr(fetcher, "fetch_category_products",
                        lambda base, slug, client=None: calls.append((base, slug)) or [])
    fetcher.fetch_events(BASE)
    assert calls == [(BASE, "events")]


# --- term parsing ---

def test_parse_terms_basic_and_normalised():
    assert parse_search_terms("Preorder, Pre-Order ,preorder") == [
        "preorder", "pre-order"]


@pytest.mark.parametrize("raw", [
    "", " , ", "a/b", "sealed!", "-lead", "x" * 51, ",".join(f"s{i}" for i in range(11)),
    "ok,<script>", "ok;drop", '"quoted"',
])
def test_parse_terms_rejects_invalid(raw):
    with pytest.raises(ValueError):
        parse_search_terms(raw)


def test_parse_terms_allows_spaces_and_punctuation():
    assert parse_search_terms("pre order, D&D, pre-order.") == ["pre order", "d&d", "pre-order."]


def test_parse_terms_limits_are_inclusive():
    assert parse_search_terms("x" * 50) == ["x" * 50]
    assert len(parse_search_terms(",".join(f"s{i}" for i in range(10)))) == 10


# --- check_new_products ---

def test_first_run_seeds_silently(db, patch_fetch):
    patch_fetch["catalog"]["preorder"] = [_product(1), _product(2)]
    assert check_new_products(db, GUILD, BASE, TZ) == []
    assert _alerted(db) == {"wp:1", "wp:2"}
    assert _markers(db) == {"seeded"}


def test_second_run_one_new_product_one_alert(db, patch_fetch):
    patch_fetch["catalog"]["preorder"] = [_product(1)]
    check_new_products(db, GUILD, BASE, TZ)
    patch_fetch["catalog"]["preorder"] = [_product(1), _product(2, "Preorder New Set")]
    got = check_new_products(db, GUILD, BASE, TZ)
    assert [p["external_id"] for p in got] == ["wp:2"]
    assert got[0]["title"] == "Preorder New Set"
    # Detection alone does not record; only a successful send does.
    assert _alerted(db) == {"wp:1"}
    assert [p["external_id"] for p in check_new_products(db, GUILD, BASE, TZ)] == ["wp:2"]


def test_first_run_with_empty_results_still_seeds_so_later_product_alerts(db, patch_fetch):
    patch_fetch["catalog"]["preorder"] = []
    assert check_new_products(db, GUILD, BASE, TZ) == []
    patch_fetch["catalog"]["preorder"] = [_product(5)]
    assert [p["external_id"] for p in check_new_products(db, GUILD, BASE, TZ)] == ["wp:5"]


def test_channel_unset_skips_without_fetching_or_seeding(session, patch_fetch):
    session.add(GuildConfig(guild_id=GUILD))
    session.commit()
    patch_fetch["catalog"]["preorder"] = [_product(1)]
    assert check_new_products(session, GUILD, BASE, TZ) == []
    assert patch_fetch["requests"] == []
    assert session.execute(select(ProductAlert)).scalars().all() == []


def test_fetches_each_term_and_dedupes_across_terms(db, patch_fetch):
    _set_terms(db, "preorder,pre-order")
    patch_fetch["catalog"] = {"preorder": [_product(1)], "pre-order": [_product(1), _product(2)]}
    check_new_products(db, GUILD, BASE, TZ)  # seed
    patch_fetch["catalog"] = {"preorder": [_product(1), _product(3)],
                              "pre-order": [_product(3), _product(4)]}
    got = check_new_products(db, GUILD, BASE, TZ)
    assert [p["external_id"] for p in got] == ["wp:3", "wp:4"]


def test_default_terms_are_preorder_spellings(db, patch_fetch):
    patch_fetch["catalog"]["preorder"] = []
    check_new_products(db, GUILD, BASE, TZ)
    terms = {r.url.params["search"] for r in patch_fetch["requests"]
             if r.url.path.endswith("/product")}
    assert terms == {"preorder", "pre-order"}


def test_description_only_match_is_not_alerted(db, patch_fetch):
    # A product whose title lacks the term but whose body mentions it must not alert.
    body_only = _product(7, "Membership Standard", content="<p>preorder perks</p>")
    patch_fetch["catalog"]["preorder"] = [body_only]
    assert check_new_products(db, GUILD, BASE, TZ) == []  # seeds (nothing title-matched)
    assert _alerted(db) == set()
    assert check_new_products(db, GUILD, BASE, TZ) == []


def test_term_match_is_case_insensitive(db, patch_fetch):
    patch_fetch["catalog"]["preorder"] = [_product(1, "[PREORDER] Big Set")]
    check_new_products(db, GUILD, BASE, TZ)
    assert _alerted(db) == {"wp:1"}


def test_failing_term_does_not_block_others(db, patch_fetch, monkeypatch):
    patch_fetch["catalog"] = {"preorder": [_product(1)], "pre-order": []}
    check_new_products(db, GUILD, BASE, TZ)  # seed
    real = product_service.fetch_products_by_search

    def flaky(base_url, term, client=None):
        if term == "pre-order":
            raise httpx.ConnectError("down")
        return real(base_url, term, client)

    monkeypatch.setattr(product_service, "fetch_products_by_search", flaky)
    patch_fetch["catalog"]["preorder"] = [_product(1), _product(2)]
    assert [p["external_id"] for p in check_new_products(db, GUILD, BASE, TZ)] == ["wp:2"]


def test_all_terms_failing_on_first_run_raises_and_records_nothing(db, patch_fetch, monkeypatch):
    def boom(base_url, term, client=None):
        raise FetchError("down")

    monkeypatch.setattr(product_service, "fetch_products_by_search", boom)
    with pytest.raises(FetchError):
        check_new_products(db, GUILD, BASE, TZ)
    assert db.execute(select(ProductAlert)).scalars().all() == []


@pytest.mark.parametrize("exc", [ValueError("bad json"), KeyError("id"), TypeError("shape")])
def test_malformed_term_response_aborts_only_that_term(db, patch_fetch, monkeypatch, exc):
    _set_terms(db, "preorder,pre-order")
    patch_fetch["catalog"] = {"preorder": [], "pre-order": []}
    check_new_products(db, GUILD, BASE, TZ)
    real = product_service.fetch_products_by_search

    def flaky(base_url, term, client=None):
        if term == "pre-order":
            raise exc
        return real(base_url, term, client)

    patch_fetch["catalog"]["preorder"] = [_product(8)]
    monkeypatch.setattr(product_service, "fetch_products_by_search", flaky)
    assert [p["external_id"] for p in check_new_products(db, GUILD, BASE, TZ)] == ["wp:8"]


# --- real prices via Store API ---

@pytest.mark.parametrize("prices,expected", [
    ({"price": "1500", "currency_minor_unit": 2, "currency_prefix": "$"}, "$15.00"),
    ({"price": "1999", "currency_minor_unit": 2, "currency_prefix": "$"}, "$19.99"),
    ({"price": "150000", "currency_minor_unit": 2, "currency_prefix": "$"}, "$1,500.00"),
    ({"price": "500", "currency_minor_unit": 0, "currency_prefix": "",
      "currency_suffix": " JPY"}, "500 JPY"),
    ({"price": "1500"}, "15.00"),  # defaults to 2 decimals, no symbol
    ({"price": "0", "currency_minor_unit": 2, "currency_prefix": "$"}, None),
    ({"price": "", "currency_minor_unit": 2, "currency_prefix": "$"}, None),
    ({"currency_minor_unit": 2, "currency_prefix": "$"}, None),
    ({"price": "abc"}, None),
    ({"price": "-5"}, None),
    ({"price": 1500}, None),
    ({"price": "1500", "currency_minor_unit": "x"}, None),
    ({"price": "1500", "currency_minor_unit": 99}, None),
    (None, None),
    ("garbage", None),
])
def test_format_store_price(prices, expected):
    assert product_service.format_store_price(prices) == expected


@pytest.mark.parametrize("prices,expected", [
    ({"price": "١٢٣"}, None),  # Arabic-Indic digits
    ({"price": "１２３"}, None),  # fullwidth digits
    ({"price": "1" * 13}, None),
    ({"price": "1" * 12, "currency_prefix": "$"}, "$1,111,111,111.11"),
    ({"price": "1500", "currency_minor_unit": -1}, None),
    ({"price": "1500", "currency_minor_unit": 5}, None),
    ({"price": "15000", "currency_minor_unit": 4, "currency_prefix": "$"}, "$1.5000"),
    ({"price": "1500", "currency_prefix": 5, "currency_suffix": ["x"]}, "15.00"),
    ({"price": "1500", "currency_prefix": "A" * 20, "currency_suffix": "B" * 20},
     "A" * 8 + "15.00" + "B" * 8),
])
def test_format_store_price_edges(prices, expected):
    assert product_service.format_store_price(prices) == expected


def _armed(db, patch_fetch, catalog=None):
    """Seed the default terms, then set `catalog` as what the next check sees."""
    patch_fetch["catalog"]["preorder"] = []
    patch_fetch["catalog"]["pre-order"] = []
    check_new_products(db, GUILD, BASE, TZ)
    if catalog is not None:
        patch_fetch["catalog"]["preorder"] = catalog


def test_alert_price_comes_from_store_api_matched_by_id(db, patch_fetch):
    _armed(db, patch_fetch)
    patch_fetch["catalog"]["preorder"] = [_product(1, "Preorder A"), _product(2, "Preorder B")]
    patch_fetch["store"] = [_store(2, "2500"), _store(1, "1500"), _store(99, "1")]
    got = {p["external_id"]: p for p in check_new_products(db, GUILD, BASE, TZ)}
    assert got["wp:1"]["price"] == "$15.00"
    assert got["wp:2"]["price"] == "$25.00"
    assert patch_fetch["store_calls"] == ["preorder", "pre-order"]


def test_store_api_skipped_when_nothing_to_alert(db, patch_fetch):
    patch_fetch["catalog"]["preorder"] = [_product(1)]
    check_new_products(db, GUILD, BASE, TZ)
    check_new_products(db, GUILD, BASE, TZ)
    assert patch_fetch["store_calls"] == []


def test_store_api_product_missing_or_zero_price_shows_none(db, patch_fetch):
    _armed(db, patch_fetch)
    patch_fetch["catalog"]["preorder"] = [_product(1), _product(2), _product(3)]
    patch_fetch["store"] = [_store(1, price=None), _store(2, "0")]
    got = check_new_products(db, GUILD, BASE, TZ)
    assert [p["price"] for p in got] == [None, None, None]


def test_store_price_wins_over_content_price(db, patch_fetch):
    _armed(db, patch_fetch, [_product(1, content="<p>→ Ticket: $25.00</p>")])
    patch_fetch["store"] = [_store(1, "1500")]
    assert [p["price"] for p in check_new_products(db, GUILD, BASE, TZ)] == ["$15.00"]


def test_store_api_failure_falls_back_with_warning_and_still_alerts(db, patch_fetch, caplog):
    _armed(db, patch_fetch)
    patch_fetch["catalog"]["preorder"] = [
        _product(1), _product(2, "Preorder B", content="<p>→ Ticket: $25.00</p>")]
    patch_fetch["store"] = FetchError("boom")
    with caplog.at_level("WARNING", logger="hightide.products"):
        got = check_new_products(db, GUILD, BASE, TZ)
    assert [p["external_id"] for p in got] == ["wp:1", "wp:2"]
    assert got[0]["price"] is None
    assert got[1]["price"] == "$25.00"  # content-extracted price is not regressed
    assert any("store price fetch" in r.message for r in caplog.records)


@pytest.mark.parametrize("garbage", [
    ["x", None, 5, {"id": 1, "prices": "nope"}, {"prices": {"price": "1"}},
     {"id": 1, "prices": {"price": "abc"}}],
    [{"id": 1}],
    [],
])
def test_store_api_garbage_does_not_regress_content_price(db, patch_fetch, garbage):
    _armed(db, patch_fetch, [_product(1, content="<p>→ Ticket: $25.00</p>")])
    patch_fetch["store"] = garbage
    assert [p["price"] for p in check_new_products(db, GUILD, BASE, TZ)] == ["$25.00"]


def test_store_httpx_error_falls_back(db, patch_fetch):
    _armed(db, patch_fetch, [_product(1, content="<p>→ Ticket: $25.00</p>")])
    patch_fetch["store"] = httpx.ConnectError("down")
    assert [p["price"] for p in check_new_products(db, GUILD, BASE, TZ)] == ["$25.00"]


def test_store_api_decode_error_degrades(db, patch_fetch):
    _armed(db, patch_fetch)
    patch_fetch["catalog"]["preorder"] = [_product(1)]
    patch_fetch["store"] = ValueError("bad json")
    got = check_new_products(db, GUILD, BASE, TZ)
    assert [p["price"] for p in got] == [None]


def test_store_partial_failure_across_terms(db, patch_fetch, monkeypatch):
    _set_terms(db, "preorder,pre-order")
    patch_fetch["catalog"] = {"preorder": [], "pre-order": []}
    check_new_products(db, GUILD, BASE, TZ)
    patch_fetch["catalog"] = {"preorder": [_product(3, content="<p>→ Ticket: $9.00</p>")],
                              "pre-order": [_product(4)]}

    def fake_store(base_url, term, client=None):
        if term == "preorder":
            raise FetchError("boom")
        return [_store(4, "2500")]

    monkeypatch.setattr(product_service, "fetch_store_products_by_search", fake_store)
    got = {p["external_id"]: p["price"] for p in check_new_products(db, GUILD, BASE, TZ)}
    assert got == {"wp:3": "$9.00", "wp:4": "$25.00"}


def test_seeded_run_transient_fetch_failure_is_silent(db, patch_fetch, monkeypatch):
    _armed(db, patch_fetch, [_product(1)])
    before = {r.external_id for r in db.execute(select(ProductAlert)).scalars()}

    def down(base_url, term, client=None):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(product_service, "fetch_products_by_search", down)
    assert check_new_products(db, GUILD, BASE, TZ) == []
    assert {r.external_id for r in db.execute(select(ProductAlert)).scalars()} == before


# --- store fetcher ---

def test_fetch_store_products_by_search_params_and_pagination():
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        page = int(request.url.params["page"])
        return httpx.Response(200, json=[{"id": page}], headers={"X-WP-TotalPages": "2"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        got = fetcher.fetch_store_products_by_search(BASE + "/", "preorder", client=c)
    assert [p["id"] for p in got] == [1, 2]
    assert all(r.url.path == "/wp-json/wc/store/v1/products" for r in seen)
    assert all(r.url.params["search"] == "preorder" for r in seen)
    assert all(r.headers["User-Agent"] == fetcher.UA["User-Agent"] for r in seen)


def test_fetch_store_products_by_search_requires_https_and_list_shape():
    with pytest.raises(FetchError):
        fetcher.fetch_store_products_by_search("http://shop.example.com", "preorder")
    with httpx.Client(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"code": "x"}))) as c:
        with pytest.raises(FetchError):
            fetcher.fetch_store_products_by_search(BASE, "preorder", client=c)


def test_fetch_store_products_by_search_caps_pages():
    seen = []

    def handler(request):
        seen.append(request)
        page = int(request.url.params["page"])
        return httpx.Response(200, json=[{"id": page}], headers={"X-WP-TotalPages": "500"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        got = fetcher.fetch_store_products_by_search(BASE, "preorder", client=c)
    assert len(seen) == fetcher.MAX_PAGES
    assert len(got) == fetcher.MAX_PAGES


def test_fetch_store_products_by_search_truncates_to_max_products(monkeypatch):
    monkeypatch.setattr(fetcher, "MAX_PRODUCTS", 150)
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=[{"id": i} for i in range(100)],
                              headers={"X-WP-TotalPages": "5"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        got = fetcher.fetch_store_products_by_search(BASE, "preorder", client=c)
    assert len(got) == 150
    assert len(seen) == 2


# --- per-guild isolation + product fields ---

def test_per_guild_isolation(db, patch_fetch):
    db.add(GuildConfig(guild_id=OTHER_GUILD, new_stock_channel_id=1))
    db.commit()
    patch_fetch["catalog"]["preorder"] = [_product(1)]
    check_new_products(db, GUILD, BASE, TZ)
    patch_fetch["catalog"]["preorder"] = [_product(1), _product(2)]
    # other guild's first run seeds independently of GUILD's history
    assert check_new_products(db, OTHER_GUILD, BASE, TZ) == []
    assert _alerted(db, OTHER_GUILD) == {"wp:1", "wp:2"}
    assert _alerted(db, GUILD) == {"wp:1"}
    assert [p["external_id"] for p in check_new_products(db, GUILD, BASE, TZ)] == ["wp:2"]


def test_product_fields_and_malformed_products(db, patch_fetch):
    patch_fetch["catalog"]["preorder"] = []
    check_new_products(db, GUILD, BASE, TZ)
    patch_fetch["catalog"]["preorder"] = [
        _product(1, "Preorder A &amp; B", content="<p>x</p><p>→ Ticket: $25.00</p>",
                 image="https://img.example.com/a.jpg"),
        {"id": 2},  # malformed: skipped, not fatal
        _product(3),
    ]
    got = check_new_products(db, GUILD, BASE, TZ)
    assert [p["external_id"] for p in got] == ["wp:1", "wp:3"]
    assert got[0]["title"] == "Preorder A & B"
    assert got[0]["image_url"] == "https://img.example.com/a.jpg"
    assert got[1]["image_url"] is None


def test_malformed_product_on_first_run_alerts_once_fixed(db, patch_fetch):
    bad = _product(2)
    bad["link"] = ["nope"]
    patch_fetch["catalog"]["preorder"] = [_product(1), bad]
    assert check_new_products(db, GUILD, BASE, TZ) == []
    assert _alerted(db) == {"wp:1"}  # malformed product is not swallowed by the seed
    patch_fetch["catalog"]["preorder"] = [_product(1), _product(2, "Preorder Fixed")]
    assert [p["external_id"] for p in check_new_products(db, GUILD, BASE, TZ)] == ["wp:2"]


def test_poison_products_mid_list_do_not_block_later_ones(db, patch_fetch):
    _armed(db, patch_fetch)
    list_link, bool_id, str_id = _product(2), _product(True), _product("4")
    list_link["link"] = ["x"]
    patch_fetch["catalog"]["preorder"] = [_product(1), list_link, bool_id, str_id, _product(5)]
    assert [p["external_id"] for p in check_new_products(db, GUILD, BASE, TZ)] == [
        "wp:1", "wp:5"]


# --- post_product_alerts ---

def _bot(channel):
    bot = MagicMock()
    bot.get_channel.return_value = channel
    bot.fetch_channel = AsyncMock(return_value=channel)
    return bot


def _channel():
    ch = MagicMock()
    ch.send = AsyncMock()
    return ch


def _prod(pid, title="Foo", url="https://shop.example.com/p/1", price=None, image=None):
    return {"external_id": f"wp:{pid}", "title": title, "url": url,
            "price": price, "image_url": image}


async def test_post_sends_one_message_per_product_and_records(db):
    ch = _channel()
    n = await post_product_alerts(
        _bot(ch), db, GUILD,
        [_prod(1, "Set A", price="$30", image="https://img.example.com/a.jpg"),
         _prod(2, "Set B")])
    assert n == 2
    assert ch.send.await_count == 2
    first = ch.send.await_args_list[0].kwargs
    embed = first["embed"]
    assert embed.description == "[Set A](https://shop.example.com/p/1) · $30"
    assert embed.thumbnail.url == "https://img.example.com/a.jpg"
    assert ch.send.await_args_list[1].kwargs["embed"].thumbnail.url is None
    assert _alerted(db) == {"wp:1", "wp:2"}


async def test_post_price_fallback_dash(db):
    ch = _channel()
    await post_product_alerts(_bot(ch), db, GUILD, [_prod(1, "Set A")])
    assert ch.send.await_args.kwargs["embed"].description.endswith(" · —")


async def test_post_uses_no_mentions(db):
    ch = _channel()
    await post_product_alerts(_bot(ch), db, GUILD, [_prod(1)])
    am = ch.send.await_args.kwargs["allowed_mentions"]
    assert am.everyone is False and am.users is False and am.roles is False


async def test_post_escapes_hostile_content(db):
    ch = _channel()
    await post_product_alerts(
        _bot(ch), db, GUILD,
        [_prod(1, "@everyone [x](http://evil) **b**", url="javascript:alert(1)",
               price="<@123> $5", image="javascript:alert(1)")])
    embed = ch.send.await_args.kwargs["embed"]
    desc = embed.description
    assert "@everyone" not in desc.replace("@​everyone", "")
    assert "](http://evil)" not in desc
    assert "javascript:" not in desc
    assert "<@123>" not in desc
    assert embed.thumbnail.url is None


async def test_post_url_with_parens_cannot_break_link(db):
    ch = _channel()
    await post_product_alerts(_bot(ch), db, GUILD,
                              [_prod(1, "T", url="https://x.example.com/a)b c")])
    desc = ch.send.await_args.kwargs["embed"].description
    assert desc.startswith("[T](https://x.example.com/a%29b%20c)")


async def test_failed_send_not_recorded_and_retried(db, patch_fetch):
    patch_fetch["catalog"]["preorder"] = []
    check_new_products(db, GUILD, BASE, TZ)  # seed
    patch_fetch["catalog"]["preorder"] = [_product(1, "Preorder New")]

    ch = _channel()
    ch.send.side_effect = discord.HTTPException(MagicMock(status=500), "boom")
    todo = check_new_products(db, GUILD, BASE, TZ)
    assert await post_product_alerts(_bot(ch), db, GUILD, todo) == 0
    assert _alerted(db) == set()

    ch2 = _channel()
    todo = check_new_products(db, GUILD, BASE, TZ)
    assert [p["external_id"] for p in todo] == ["wp:1"]
    assert await post_product_alerts(_bot(ch2), db, GUILD, todo) == 1
    assert _alerted(db) == {"wp:1"}
    assert check_new_products(db, GUILD, BASE, TZ) == []


async def test_one_failed_send_does_not_block_others(db):
    ch = _channel()
    ch.send.side_effect = [discord.HTTPException(MagicMock(status=400), "bad"), None]
    n = await post_product_alerts(_bot(ch), db, GUILD, [_prod(1), _prod(2)])
    assert n == 1
    assert _alerted(db) == {"wp:2"}


async def test_post_channel_unset_skips(session):
    session.add(GuildConfig(guild_id=GUILD))
    session.commit()
    ch = _channel()
    assert await post_product_alerts(_bot(ch), session, GUILD, [_prod(1)]) == 0
    ch.send.assert_not_awaited()
    assert _alerted(session) == set()


async def test_post_channel_unavailable_skips(db):
    bot = MagicMock()
    bot.get_channel.return_value = None
    bot.fetch_channel = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "gone"))
    assert await post_product_alerts(bot, db, GUILD, [_prod(1)]) == 0
    assert _alerted(db) == set()


async def test_post_empty_list_is_noop(db):
    ch = _channel()
    assert await post_product_alerts(_bot(ch), db, GUILD, []) == 0
    ch.send.assert_not_awaited()


async def test_alert_cap_posts_ten_plus_digest_and_leaves_overflow_unrecorded(db, patch_fetch):
    _armed(db, patch_fetch, [_product(i) for i in range(1, 13)])
    ch = _channel()
    todo = check_new_products(db, GUILD, BASE, TZ)
    assert len(todo) == 12
    assert await post_product_alerts(_bot(ch), db, GUILD, todo) == 10
    assert ch.send.await_count == 11
    digest = ch.send.await_args_list[-1]
    assert digest.args == ("➕ 2 more new products — check the shop.",)
    am = digest.kwargs["allowed_mentions"]
    assert am.everyone is False and am.users is False and am.roles is False
    assert _alerted(db) == {f"wp:{i}" for i in range(1, 11)}
    # next run drains the rest
    nxt = check_new_products(db, GUILD, BASE, TZ)
    assert [p["external_id"] for p in nxt] == ["wp:11", "wp:12"]
    ch2 = _channel()
    assert await post_product_alerts(_bot(ch2), db, GUILD, nxt) == 2
    assert ch2.send.await_count == 2  # no digest when nothing overflows


async def test_exactly_cap_posts_no_digest(db):
    ch = _channel()
    n = await post_product_alerts(_bot(ch), db, GUILD, [_prod(i) for i in range(10)])
    assert n == 10 and ch.send.await_count == 10


# --- scheduler wiring ---

@pytest.fixture
def sched_db(temp_db, monkeypatch):
    monkeypatch.setattr("src.services.scheduler.refresh_events",
                        MagicMock(return_value={"added": 0}))
    monkeypatch.setattr("src.services.scheduler.upsert_board",
                        AsyncMock(side_effect=["created", "created"]))
    return temp_db


def _seed_cfg(temp_db, **kw):
    with temp_db() as s:
        s.add(GuildConfig(guild_id=GUILD, timezone="Australia/Perth",
                          this_week_channel_id=1, this_month_channel_id=2, **kw))
        s.commit()


async def test_scheduler_runs_product_step_when_channel_set(sched_db, monkeypatch):
    _seed_cfg(sched_db, new_stock_channel_id=CHANNEL_ID)
    check = MagicMock(return_value=[_prod(1)])
    post = AsyncMock(return_value=1)
    monkeypatch.setattr("src.services.scheduler.check_new_products", check)
    monkeypatch.setattr("src.services.scheduler.post_product_alerts", post)
    result = await scheduler.run_daily_refresh(MagicMock(), GUILD)
    check.assert_called_once()
    assert check.call_args.args[1] == GUILD
    post.assert_awaited_once()
    assert result["products"] == 1


async def test_scheduler_skips_product_step_when_channel_unset(sched_db, monkeypatch):
    _seed_cfg(sched_db)
    check = MagicMock()
    monkeypatch.setattr("src.services.scheduler.check_new_products", check)
    result = await scheduler.run_daily_refresh(MagicMock(), GUILD)
    check.assert_not_called()
    assert "products" not in result


async def test_scheduler_skips_product_step_when_fetch_false(sched_db, monkeypatch):
    _seed_cfg(sched_db, new_stock_channel_id=CHANNEL_ID)
    check = MagicMock()
    monkeypatch.setattr("src.services.scheduler.check_new_products", check)
    result = await scheduler.run_daily_refresh(MagicMock(), GUILD, fetch=False)
    check.assert_not_called()
    assert "products" not in result


async def test_scheduler_product_failure_isolated_and_non_driving(sched_db, monkeypatch):
    _seed_cfg(sched_db, new_stock_channel_id=CHANNEL_ID)
    monkeypatch.setattr("src.services.scheduler.check_new_products",
                        MagicMock(side_effect=ConnectionError("net")))
    result = await scheduler.run_daily_refresh(MagicMock(), GUILD)
    assert result["products"] == "error:ConnectionError"
    assert result["this_week"] == "created"
    assert "products" in scheduler._NON_DRIVING_KEYS


async def test_scheduler_real_check_to_post_wiring(sched_db, patch_fetch, monkeypatch):
    _seed_cfg(sched_db, new_stock_channel_id=CHANNEL_ID)
    monkeypatch.setattr("src.services.scheduler.upsert_board", AsyncMock(return_value="created"))
    check = MagicMock(wraps=product_service.check_new_products)
    post = AsyncMock(wraps=product_service.post_product_alerts)
    monkeypatch.setattr("src.services.scheduler.check_new_products", check)
    monkeypatch.setattr("src.services.scheduler.post_product_alerts", post)
    ch = _channel()
    bot = _bot(ch)

    patch_fetch["catalog"]["preorder"] = [_product(1)]
    assert (await scheduler.run_daily_refresh(bot, GUILD))["products"] == 0  # silent seed
    ch.send.assert_not_awaited()

    patch_fetch["catalog"]["preorder"] = [_product(1), _product(2, "Preorder New Set")]
    patch_fetch["store"] = [_store(2, "2500")]
    result = await scheduler.run_daily_refresh(bot, GUILD)
    assert check.call_args.args[2] == "https://hightidetabletop.com.au"
    assert check.call_args.args[3] == ZoneInfo("Australia/Perth")
    assert {r.url.host for r in patch_fetch["requests"]} == {"hightidetabletop.com.au"}
    handed = post.await_args.args[3]
    assert [(p["external_id"], p["price"]) for p in handed] == [("wp:2", "$25.00")]
    assert result["products"] == 1
    assert ch.send.await_args.kwargs["embed"].description == (
        "[Preorder New Set](https://shop.example.com/product/p2/) · $25.00")


# --- /config commands ---

@pytest.fixture
def admin_db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'admin.db'}"
    engine = get_engine(url)
    Base.metadata.create_all(engine)
    factory = get_session_factory(url)
    monkeypatch.setattr(commands_admin, "_session_factory", factory)
    yield factory
    engine.dispose()
    factory.kw["bind"].dispose()


def _interaction():
    i = MagicMock()
    i.guild_id = GUILD
    i.response.send_message = AsyncMock()
    return i


async def test_config_new_stock_channel(admin_db):
    cog = AdminCog(MagicMock())
    i = _interaction()
    channel = MagicMock()
    channel.id = 4242
    channel.mention = "<#4242>"
    await cog.set_new_stock_channel.callback(cog, i, channel)
    with admin_db() as s:
        assert s.get(GuildConfig, GUILD).new_stock_channel_id == 4242
    assert i.response.send_message.call_args.args[0] == "new-stock channel set to <#4242>"
    assert i.response.send_message.call_args.kwargs["ephemeral"] is True


async def test_config_new_stock_terms_saves_normalised(admin_db):
    cog = AdminCog(MagicMock())
    i = _interaction()
    await cog.set_new_stock_terms.callback(cog, i, "Preorder, Pre-Order")
    with admin_db() as s:
        assert s.get(GuildConfig, GUILD).new_stock_terms == "preorder,pre-order"
    assert i.response.send_message.call_args.args[0] == (
        "watching product titles for: preorder, pre-order")
    assert i.response.send_message.call_args.kwargs["ephemeral"] is True


async def test_config_new_stock_terms_rejects_invalid_without_saving(admin_db):
    cog = AdminCog(MagicMock())
    i = _interaction()
    await cog.set_new_stock_terms.callback(cog, i, "bad term!")
    with admin_db() as s:
        assert s.get(GuildConfig, GUILD) is None  # nothing created on rejection
    assert i.response.send_message.call_args.args[0].startswith("Invalid terms.")
    assert i.response.send_message.call_args.kwargs["ephemeral"] is True


async def test_config_new_stock_terms_invalid_keeps_existing_value(admin_db):
    with admin_db() as s:
        s.add(GuildConfig(guild_id=GUILD, new_stock_terms="restock"))
        s.commit()
    cog = AdminCog(MagicMock())
    i = _interaction()
    await cog.set_new_stock_terms.callback(cog, i, "bad term!")
    with admin_db() as s:
        assert s.get(GuildConfig, GUILD).new_stock_terms == "restock"
    assert i.response.send_message.call_args.args[0].startswith("Invalid terms.")


def test_new_guild_config_defaults(session):
    cfg = GuildConfig(guild_id=1)
    session.add(cfg)
    session.commit()
    assert cfg.new_stock_channel_id is None
    assert cfg.new_stock_terms == "preorder,pre-order"


def test_product_alerts_unique_per_guild_and_ddl(session):
    session.add(ProductAlert(guild_id=1, external_id="wp:1"))
    session.commit()
    session.add(ProductAlert(guild_id=1, external_id="wp:1"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
    session.add(ProductAlert(guild_id=2, external_id="wp:1"))
    session.commit()


def test_ensure_v2_columns_adds_new_stock_terms_and_migrates(tmp_path):
    engine = get_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE guild_config (guild_id BIGINT PRIMARY KEY)"))
            conn.execute(text("INSERT INTO guild_config (guild_id) VALUES (1)"))
        ensure_guild_config_v2_columns(engine)
        ensure_guild_config_v2_columns(engine)
        cols = {c["name"] for c in inspect(engine).get_columns("guild_config")}
        assert {"new_stock_channel_id", "new_stock_cat_slugs", "new_stock_terms"} <= cols
        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT new_stock_channel_id, new_stock_terms FROM guild_config")).one()
        assert tuple(row) == (None, "preorder,pre-order")
    finally:
        engine.dispose()


def test_ensure_v2_columns_migrates_custom_slugs_to_terms(tmp_path):
    engine = get_engine(f"sqlite:///{tmp_path / 'custom.db'}")
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE guild_config (guild_id BIGINT PRIMARY KEY, "
                "new_stock_cat_slugs VARCHAR NOT NULL DEFAULT 'pre-orders')"))
            conn.execute(text(
                "INSERT INTO guild_config (guild_id, new_stock_cat_slugs) VALUES (1, 'sealed')"))
            conn.execute(text(
                "INSERT INTO guild_config (guild_id, new_stock_cat_slugs) VALUES (2, 'pre-orders')"))
        ensure_guild_config_v2_columns(engine)
        with engine.connect() as conn:
            rows = dict(conn.execute(text("SELECT guild_id, new_stock_terms FROM guild_config")).all())
        assert rows[1] == "sealed"          # custom slug preserved as a term
        assert rows[2] == "preorder,pre-order"  # default slug left at the default terms
    finally:
        engine.dispose()


# --- record_product_alerts ---

def test_record_product_alerts_idempotent(session):
    repositories.record_product_alerts(session, GUILD, ["wp:1", "wp:2"])
    repositories.record_product_alerts(session, GUILD, ["wp:2", "wp:1"])
    rows = session.execute(select(ProductAlert)).scalars().all()
    assert sorted(r.external_id for r in rows) == ["wp:1", "wp:2"]


def test_record_product_alerts_integrity_error_rolls_back(session, monkeypatch):
    repositories.record_product_alerts(session, GUILD, ["wp:1"])
    # Simulate a concurrent writer: the pre-check misses the existing row.
    monkeypatch.setattr(repositories, "get_alerted_product_ids", lambda s, g: set())
    repositories.record_product_alerts(session, GUILD, ["wp:1", "wp:2"])  # must not raise
    monkeypatch.undo()
    assert repositories.get_alerted_product_ids(session, GUILD) == {"wp:1"}
    repositories.record_product_alerts(session, GUILD, ["wp:3"])  # session still usable
    assert repositories.get_alerted_product_ids(session, GUILD) == {"wp:1", "wp:3"}


# --- _to_alert caps + validation ---

def test_to_alert_truncates_title_url_price():
    alert = product_service._to_alert(_product(
        1, title="T" * 300, link="https://shop.example.com/" + "a" * 600,
        content="<p>→ Ticket: $" + "9" * 100 + "</p>"))
    assert len(alert["title"]) == product_service.MAX_TITLE_CHARS
    assert len(alert["url"]) == product_service.MAX_URL_CHARS
    assert len(alert["price"]) == product_service.MAX_PRICE_CHARS


def test_to_alert_int_id_ok_and_string_id_rejected():
    assert product_service._to_alert(_product(5))["external_id"] == "wp:5"
    assert product_service._to_alert(_product("5")) is None


@pytest.mark.parametrize("bad_id", [True, False, 5.0, None])
def test_to_alert_rejects_non_int_ids(bad_id):
    assert product_service._to_alert(_product(bad_id)) is None


@pytest.mark.parametrize("bad_link", [["https://x.example.com"], {"u": 1}, 5, None])
def test_to_alert_rejects_non_string_link(bad_link):
    p = _product(1)
    p["link"] = bad_link
    assert product_service._to_alert(p) is None


@pytest.mark.parametrize("bad_src", [["https://i.example.com/a.jpg"], {"u": 1}, 5,
                                     "https://i.example.com/" + "a" * 5000],
                         ids=["list", "dict", "int", "5000-chars"])
def test_to_alert_drops_bad_image_but_keeps_product(bad_src):
    p = _product(1)
    p["_embedded"] = {"wp:featuredmedia": [{"source_url": bad_src}]}
    alert = product_service._to_alert(p)
    assert alert is not None and alert["image_url"] is None


def test_to_alert_image_length_boundary():
    prefix = "https://i.example.com/"
    ok, too_long = prefix + "a" * (2000 - len(prefix)), prefix + "a" * (2001 - len(prefix))
    for src, expected in ((ok, ok), (too_long, None), (None, None)):
        p = _product(1)
        p["_embedded"] = {"wp:featuredmedia": [{"source_url": src}]}
        assert product_service._to_alert(p)["image_url"] == expected


# --- title matching ---

@pytest.mark.parametrize("title,expected", [
    ("Preorder Foo", True), ("[PREORDER] Foo", True), ("Foo pre-order now", True),
    ("Membership Standard", False), ("", False),
])
def test_title_matches(title, expected):
    assert product_service._title_matches(_product(1, title), ["preorder", "pre-order"]) == expected


def test_title_matches_non_string_title():
    p = _product(1)
    p["title"] = {"rendered": 123}
    assert product_service._title_matches(p, ["preorder"]) is False
