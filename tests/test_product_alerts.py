from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import discord
import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.db import repositories
from src.db.models import Base, GuildConfig, ProductAlert
from src.db.session import get_engine, get_session_factory
from src.discord_ui import commands_admin
from src.discord_ui.commands_admin import AdminCog
from src.scraping import fetcher
from src.scraping.fetcher import FetchError, fetch_category_products
from src.services import product_service, scheduler
from src.services.product_service import (
    check_new_products, parse_category_slugs, post_product_alerts,
)

GUILD = 999
OTHER_GUILD = 1000
CHANNEL_ID = 555
BASE = "https://shop.example.com"
TZ = ZoneInfo("Australia/Perth")


def _product(pid, title="Foo Set", link=None, content="", image=None):
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
    """Mock WP API: slug -> products. Unknown slug -> empty category list."""
    ids = {slug: 100 + i for i, slug in enumerate(catalog)}

    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        if request.url.path.endswith("/product_cat"):
            slug = request.url.params["slug"]
            return httpx.Response(200, json=[{"id": ids[slug]}] if slug in ids else [])
        cat = int(request.url.params["product_cat"])
        slug = next(s for s, i in ids.items() if i == cat)
        return httpx.Response(200, json=catalog[slug])

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def patch_fetch(monkeypatch):
    """Route the service's category fetch through a mutable in-memory catalog."""
    state = {"catalog": {"pre-orders": []}, "requests": [], "store": [], "store_calls": []}
    real = fetch_category_products
    real_resolve = fetcher.resolve_category_id

    def fake(base_url, cat_slug, client=None):
        with _shop(state["catalog"], state["requests"]) as c:
            return real(base_url, cat_slug, client=c)

    def fake_resolve(base_url, cat_slug, client=None):
        with _shop(state["catalog"]) as c:
            return real_resolve(base_url, cat_slug, client=c)

    def fake_store(base_url, cat_id, client=None):
        state["store_calls"].append(cat_id)
        if isinstance(state["store"], Exception):
            raise state["store"]
        return state["store"]

    monkeypatch.setattr(product_service, "fetch_category_products", fake)
    monkeypatch.setattr(product_service, "resolve_category_id", fake_resolve)
    monkeypatch.setattr(product_service, "fetch_store_products", fake_store)
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


# --- fetcher generalisation ---

def test_fetch_category_products_uses_given_slug_and_paginates():
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        if request.url.path.endswith("/product_cat"):
            return httpx.Response(200, json=[{"id": 7}])
        page = int(request.url.params["page"])
        return httpx.Response(200, json=[{"id": page}], headers={"X-WP-TotalPages": "2"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        got = fetch_category_products(BASE + "/", "pre-orders", client=c)

    assert [p["id"] for p in got] == [1, 2]
    assert seen[0].url.params["slug"] == "pre-orders"
    reqs = [r for r in seen if r.url.path.endswith("/product")]
    assert all(r.url.params["product_cat"] == "7" for r in reqs)
    assert all(r.url.params["_embed"] == "1" for r in reqs)


def test_fetch_category_products_unknown_category_raises():
    with httpx.Client(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json=[]))) as c:
        with pytest.raises(FetchError, match="pre-orders"):
            fetch_category_products(BASE, "pre-orders", client=c)


def test_fetch_category_products_requires_https():
    with pytest.raises(FetchError):
        fetch_category_products("http://shop.example.com", "pre-orders")


def test_fetch_events_delegates_with_events_slug(monkeypatch):
    calls = []
    monkeypatch.setattr(fetcher, "fetch_category_products",
                        lambda base, slug, client=None: calls.append((base, slug)) or [])
    fetcher.fetch_events(BASE)
    assert calls == [(BASE, "events")]


# --- slug parsing ---

def test_parse_slugs_basic_and_normalised():
    assert parse_category_slugs("pre-orders, New-Releases ,pre-orders") == [
        "pre-orders", "new-releases"]


@pytest.mark.parametrize("raw", [
    "", " , ", "bad slug", "a/b", "sealed!", "-lead", "x" * 51, ",".join(f"s{i}" for i in range(11)),
    "ok,<script>",
])
def test_parse_slugs_rejects_invalid(raw):
    with pytest.raises(ValueError):
        parse_category_slugs(raw)


def test_parse_slugs_limits_are_inclusive():
    assert parse_category_slugs("x" * 50) == ["x" * 50]
    assert len(parse_category_slugs(",".join(f"s{i}" for i in range(10)))) == 10


# --- check_new_products ---

def test_first_run_seeds_silently(db, patch_fetch):
    patch_fetch["catalog"]["pre-orders"] = [_product(1), _product(2)]
    assert check_new_products(db, GUILD, BASE, TZ) == []
    assert _alerted(db) == {"wp:1", "wp:2"}


def test_second_run_one_new_product_one_alert(db, patch_fetch):
    patch_fetch["catalog"]["pre-orders"] = [_product(1)]
    check_new_products(db, GUILD, BASE, TZ)
    patch_fetch["catalog"]["pre-orders"] = [_product(1), _product(2, "New Set")]
    got = check_new_products(db, GUILD, BASE, TZ)
    assert [p["external_id"] for p in got] == ["wp:2"]
    assert got[0]["title"] == "New Set"
    # Detection alone does not record; only a successful send does.
    assert _alerted(db) == {"wp:1"}
    assert [p["external_id"] for p in check_new_products(db, GUILD, BASE, TZ)] == ["wp:2"]


def test_first_run_with_empty_category_still_seeds_so_later_product_alerts(db, patch_fetch):
    patch_fetch["catalog"]["pre-orders"] = []
    assert check_new_products(db, GUILD, BASE, TZ) == []
    patch_fetch["catalog"]["pre-orders"] = [_product(5)]
    assert [p["external_id"] for p in check_new_products(db, GUILD, BASE, TZ)] == ["wp:5"]


def test_channel_unset_skips_without_fetching_or_seeding(session, patch_fetch):
    session.add(GuildConfig(guild_id=GUILD))
    session.commit()
    patch_fetch["catalog"]["pre-orders"] = [_product(1)]
    assert check_new_products(session, GUILD, BASE, TZ) == []
    assert patch_fetch["requests"] == []
    assert session.execute(select(ProductAlert)).scalars().all() == []


def test_fetches_each_configured_slug_and_dedupes(db, patch_fetch):
    cfg = db.get(GuildConfig, GUILD)
    cfg.new_stock_cat_slugs = "pre-orders,sealed"
    db.commit()
    patch_fetch["catalog"] = {"pre-orders": [_product(1)], "sealed": [_product(1), _product(2)]}
    check_new_products(db, GUILD, BASE, TZ)  # seed
    patch_fetch["catalog"] = {"pre-orders": [_product(1), _product(3)],
                              "sealed": [_product(3), _product(4)]}
    got = check_new_products(db, GUILD, BASE, TZ)
    assert [p["external_id"] for p in got] == ["wp:3", "wp:4"]


def test_default_slug_is_pre_orders(db, patch_fetch):
    patch_fetch["catalog"]["pre-orders"] = []
    check_new_products(db, GUILD, BASE, TZ)
    slugs = {r.url.params["slug"] for r in patch_fetch["requests"]
             if r.url.path.endswith("/product_cat")}
    assert slugs == {"pre-orders"}


def test_bad_slug_after_seed_is_skipped_others_still_alert(db, patch_fetch):
    cfg = db.get(GuildConfig, GUILD)
    cfg.new_stock_cat_slugs = "pre-orders,typo"
    db.commit()
    patch_fetch["catalog"] = {"pre-orders": [_product(1)]}
    # an unresolvable slug neither blocks nor gets a marker; the good slug still seeds
    assert check_new_products(db, GUILD, BASE, TZ) == []
    assert _markers(db) == {"seeded:pre-orders"}
    patch_fetch["catalog"] = {"pre-orders": [_product(1), _product(2)]}
    assert [p["external_id"] for p in check_new_products(db, GUILD, BASE, TZ)] == ["wp:2"]
    assert _markers(db) == {"seeded:pre-orders"}


def test_all_slugs_failing_on_first_run_raises_and_records_nothing(db, patch_fetch):
    patch_fetch["catalog"] = {}
    with pytest.raises(FetchError):
        check_new_products(db, GUILD, BASE, TZ)
    assert db.execute(select(ProductAlert)).scalars().all() == []


# --- per-slug seeding ---

def _markers(session, guild=GUILD):
    return {r.external_id for r in session.execute(
        select(ProductAlert).where(ProductAlert.guild_id == guild)).scalars()
        if r.external_id.startswith("seeded")}


def _set_slugs(session, slugs):
    session.get(GuildConfig, GUILD).new_stock_cat_slugs = slugs
    session.commit()


def test_seed_creates_per_slug_marker(db, patch_fetch):
    patch_fetch["catalog"]["pre-orders"] = []
    check_new_products(db, GUILD, BASE, TZ)
    assert _markers(db) == {"seeded:pre-orders"}


def test_adding_slug_after_seed_does_not_flood(db, patch_fetch):
    patch_fetch["catalog"] = {"pre-orders": [_product(1)]}
    check_new_products(db, GUILD, BASE, TZ)
    _set_slugs(db, "pre-orders,sealed")
    patch_fetch["catalog"] = {"pre-orders": [_product(1)],
                              "sealed": [_product(10), _product(11)]}
    assert check_new_products(db, GUILD, BASE, TZ) == []
    assert _alerted(db) == {"wp:1", "wp:10", "wp:11"}
    assert _markers(db) == {"seeded:pre-orders", "seeded:sealed"}
    # the new slug now alerts on genuinely new products
    patch_fetch["catalog"]["sealed"].append(_product(12))
    assert [p["external_id"] for p in check_new_products(db, GUILD, BASE, TZ)] == ["wp:12"]


def test_new_slug_seed_does_not_swallow_pending_alert_from_seeded_slug(db, patch_fetch):
    patch_fetch["catalog"] = {"pre-orders": [_product(1)]}
    check_new_products(db, GUILD, BASE, TZ)
    _set_slugs(db, "pre-orders,sealed")
    patch_fetch["catalog"] = {"pre-orders": [_product(1), _product(2)],
                              "sealed": [_product(2), _product(3)]}
    assert [p["external_id"] for p in check_new_products(db, GUILD, BASE, TZ)] == ["wp:2"]
    assert _alerted(db) == {"wp:1", "wp:3"}


def test_failed_unseeded_slug_retries_seeding_later(db, patch_fetch):
    patch_fetch["catalog"] = {"pre-orders": []}
    check_new_products(db, GUILD, BASE, TZ)
    _set_slugs(db, "pre-orders,sealed")
    assert check_new_products(db, GUILD, BASE, TZ) == []  # sealed unresolvable
    assert _markers(db) == {"seeded:pre-orders"}
    patch_fetch["catalog"] = {"pre-orders": [], "sealed": [_product(7)]}
    assert check_new_products(db, GUILD, BASE, TZ) == []
    assert _markers(db) == {"seeded:pre-orders", "seeded:sealed"}
    assert _alerted(db) == {"wp:7"}


def test_legacy_global_marker_covers_existing_slugs(db, patch_fetch):
    _set_slugs(db, "pre-orders,sealed")
    db.add(ProductAlert(guild_id=GUILD, external_id="seeded"))
    db.add(ProductAlert(guild_id=GUILD, external_id="wp:1"))
    db.commit()
    patch_fetch["catalog"] = {"pre-orders": [_product(1), _product(2)], "sealed": []}
    got = check_new_products(db, GUILD, BASE, TZ)
    assert [p["external_id"] for p in got] == ["wp:2"]
    assert {"seeded:pre-orders", "seeded:sealed"} <= _markers(db)


def test_legacy_marker_then_new_slug_seeds_individually(db, patch_fetch):
    db.add(ProductAlert(guild_id=GUILD, external_id="seeded"))
    db.commit()
    patch_fetch["catalog"] = {"pre-orders": []}
    check_new_products(db, GUILD, BASE, TZ)  # migrates
    _set_slugs(db, "pre-orders,extra")
    patch_fetch["catalog"] = {"pre-orders": [], "extra": [_product(9)]}
    assert check_new_products(db, GUILD, BASE, TZ) == []
    assert "seeded:extra" in _markers(db)
    assert _alerted(db) == {"wp:9"}


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


def test_alert_price_comes_from_store_api_matched_by_id(db, patch_fetch):
    patch_fetch["catalog"]["pre-orders"] = []
    check_new_products(db, GUILD, BASE, TZ)
    patch_fetch["catalog"]["pre-orders"] = [_product(1, "A"), _product(2, "B")]
    patch_fetch["store"] = [_store(2, "2500"), _store(1, "1500"), _store(99, "1")]
    got = {p["external_id"]: p for p in check_new_products(db, GUILD, BASE, TZ)}
    assert got["wp:1"]["price"] == "$15.00"
    assert got["wp:2"]["price"] == "$25.00"
    assert patch_fetch["store_calls"] == [100]


def test_store_api_skipped_when_nothing_to_alert(db, patch_fetch):
    patch_fetch["catalog"]["pre-orders"] = [_product(1)]
    check_new_products(db, GUILD, BASE, TZ)
    check_new_products(db, GUILD, BASE, TZ)
    assert patch_fetch["store_calls"] == []


def test_store_api_product_missing_or_zero_price_shows_none(db, patch_fetch):
    patch_fetch["catalog"]["pre-orders"] = []
    check_new_products(db, GUILD, BASE, TZ)
    patch_fetch["catalog"]["pre-orders"] = [_product(1), _product(2), _product(3)]
    patch_fetch["store"] = [_store(1, price=None), _store(2, "0")]
    got = check_new_products(db, GUILD, BASE, TZ)
    assert [p["price"] for p in got] == [None, None, None]


def test_store_api_failure_falls_back_with_warning_and_still_alerts(db, patch_fetch, caplog):
    patch_fetch["catalog"]["pre-orders"] = []
    check_new_products(db, GUILD, BASE, TZ)
    patch_fetch["catalog"]["pre-orders"] = [_product(1), _product(2, content="<p>→ Ticket: $25.00</p>")]
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
    patch_fetch["catalog"]["pre-orders"] = []
    check_new_products(db, GUILD, BASE, TZ)
    patch_fetch["catalog"]["pre-orders"] = [_product(1, content="<p>→ Ticket: $25.00</p>")]
    patch_fetch["store"] = garbage
    assert [p["price"] for p in check_new_products(db, GUILD, BASE, TZ)] == ["$25.00"]


def test_store_api_decode_error_degrades(db, patch_fetch):
    patch_fetch["catalog"]["pre-orders"] = []
    check_new_products(db, GUILD, BASE, TZ)
    patch_fetch["catalog"]["pre-orders"] = [_product(1)]
    patch_fetch["store"] = ValueError("bad json")
    got = check_new_products(db, GUILD, BASE, TZ)
    assert [p["price"] for p in got] == [None]


def test_fetch_store_products_params_and_pagination():
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        page = int(request.url.params["page"])
        return httpx.Response(200, json=[{"id": page}], headers={"X-WP-TotalPages": "2"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        got = fetcher.fetch_store_products(BASE + "/", 7, client=c)
    assert [p["id"] for p in got] == [1, 2]
    assert all(r.url.path == "/wp-json/wc/store/v1/products" for r in seen)
    assert all(r.url.params["category"] == "7" for r in seen)
    assert all(r.headers["User-Agent"] == fetcher.UA["User-Agent"] for r in seen)


def test_fetch_store_products_requires_https_and_list_shape():
    with pytest.raises(FetchError):
        fetcher.fetch_store_products("http://shop.example.com", 7)
    with httpx.Client(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"code": "x"}))) as c:
        with pytest.raises(FetchError):
            fetcher.fetch_store_products(BASE, 7, client=c)


def test_per_guild_isolation(db, patch_fetch):
    db.add(GuildConfig(guild_id=OTHER_GUILD, new_stock_channel_id=1))
    db.commit()
    patch_fetch["catalog"]["pre-orders"] = [_product(1)]
    check_new_products(db, GUILD, BASE, TZ)
    patch_fetch["catalog"]["pre-orders"] = [_product(1), _product(2)]
    # other guild's first run seeds independently of GUILD's history
    assert check_new_products(db, OTHER_GUILD, BASE, TZ) == []
    assert _alerted(db, OTHER_GUILD) == {"wp:1", "wp:2"}
    assert _alerted(db, GUILD) == {"wp:1"}
    assert [p["external_id"] for p in check_new_products(db, GUILD, BASE, TZ)] == ["wp:2"]


def test_product_fields_and_malformed_products(db, patch_fetch):
    patch_fetch["catalog"]["pre-orders"] = []
    check_new_products(db, GUILD, BASE, TZ)
    patch_fetch["catalog"]["pre-orders"] = [
        _product(1, "A &amp; B", content="<p>x</p><p>→ Ticket: $25.00</p>",
                 image="https://img.example.com/a.jpg"),
        {"id": 2},  # malformed: skipped, not fatal
        _product(3),
    ]
    got = check_new_products(db, GUILD, BASE, TZ)
    assert [p["external_id"] for p in got] == ["wp:1", "wp:3"]
    assert got[0]["title"] == "A & B"
    assert got[0]["image_url"] == "https://img.example.com/a.jpg"
    assert got[1]["image_url"] is None


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
    assert "@everyone" not in desc.replace("@\u200beveryone", "")
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
    patch_fetch["catalog"]["pre-orders"] = []
    check_new_products(db, GUILD, BASE, TZ)  # seed
    patch_fetch["catalog"]["pre-orders"] = [_product(1, "New")]

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


async def test_scheduler_product_failure_isolated_and_non_driving(sched_db, monkeypatch):
    _seed_cfg(sched_db, new_stock_channel_id=CHANNEL_ID)
    monkeypatch.setattr("src.services.scheduler.check_new_products",
                        MagicMock(side_effect=ConnectionError("net")))
    result = await scheduler.run_daily_refresh(MagicMock(), GUILD)
    assert result["products"] == "error:ConnectionError"
    assert result["this_week"] == "created"
    assert "products" in scheduler._NON_DRIVING_KEYS


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


async def test_config_new_stock_categories_saves_normalised(admin_db):
    cog = AdminCog(MagicMock())
    i = _interaction()
    await cog.set_new_stock_categories.callback(cog, i, "Pre-Orders, sealed")
    with admin_db() as s:
        assert s.get(GuildConfig, GUILD).new_stock_cat_slugs == "pre-orders,sealed"
    assert i.response.send_message.call_args.args[0] == "watching categories: pre-orders, sealed"
    assert i.response.send_message.call_args.kwargs["ephemeral"] is True


async def test_config_new_stock_categories_rejects_invalid_without_saving(admin_db):
    cog = AdminCog(MagicMock())
    i = _interaction()
    await cog.set_new_stock_categories.callback(cog, i, "bad slug!")
    with admin_db() as s:
        assert s.get(GuildConfig, GUILD) is None  # nothing created on rejection
    assert i.response.send_message.call_args.args[0].startswith("Invalid categories.")
    assert i.response.send_message.call_args.kwargs["ephemeral"] is True


async def test_config_new_stock_categories_invalid_keeps_existing_value(admin_db):
    with admin_db() as s:
        s.add(GuildConfig(guild_id=GUILD, new_stock_cat_slugs="sealed"))
        s.commit()
    cog = AdminCog(MagicMock())
    i = _interaction()
    await cog.set_new_stock_categories.callback(cog, i, "bad slug!")
    with admin_db() as s:
        assert s.get(GuildConfig, GUILD).new_stock_cat_slugs == "sealed"
    assert i.response.send_message.call_args.args[0].startswith("Invalid categories.")


def test_new_guild_config_defaults(session):
    cfg = GuildConfig(guild_id=1)
    session.add(cfg)
    session.commit()
    assert cfg.new_stock_channel_id is None
    assert cfg.new_stock_cat_slugs == "pre-orders"


def test_product_alerts_unique_per_guild_and_ddl(session):
    from sqlalchemy.exc import IntegrityError
    session.add(ProductAlert(guild_id=1, external_id="wp:1"))
    session.commit()
    session.add(ProductAlert(guild_id=1, external_id="wp:1"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
    session.add(ProductAlert(guild_id=2, external_id="wp:1"))
    session.commit()


def test_ensure_v2_columns_adds_new_stock_columns(tmp_path):
    from sqlalchemy import inspect, text
    from src.db.session import ensure_guild_config_v2_columns
    engine = get_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE guild_config (guild_id BIGINT PRIMARY KEY)"))
            conn.execute(text("INSERT INTO guild_config (guild_id) VALUES (1)"))
        ensure_guild_config_v2_columns(engine)
        ensure_guild_config_v2_columns(engine)
        cols = {c["name"] for c in inspect(engine).get_columns("guild_config")}
        assert {"new_stock_channel_id", "new_stock_cat_slugs"} <= cols
        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT new_stock_channel_id, new_stock_cat_slugs FROM guild_config")).one()
        assert tuple(row) == (None, "pre-orders")
    finally:
        engine.dispose()


# --- council batch: scheduler gate + real wiring ---

async def test_scheduler_skips_product_step_when_fetch_false(sched_db, monkeypatch):
    _seed_cfg(sched_db, new_stock_channel_id=CHANNEL_ID)
    check = MagicMock()
    monkeypatch.setattr("src.services.scheduler.check_new_products", check)
    result = await scheduler.run_daily_refresh(MagicMock(), GUILD, fetch=False)
    check.assert_not_called()
    assert "products" not in result


async def test_scheduler_real_check_to_post_wiring(sched_db, patch_fetch, monkeypatch):
    _seed_cfg(sched_db, new_stock_channel_id=CHANNEL_ID)
    monkeypatch.setattr("src.services.scheduler.upsert_board", AsyncMock(return_value="created"))
    check = MagicMock(wraps=product_service.check_new_products)
    post = AsyncMock(wraps=product_service.post_product_alerts)
    monkeypatch.setattr("src.services.scheduler.check_new_products", check)
    monkeypatch.setattr("src.services.scheduler.post_product_alerts", post)
    ch = _channel()
    bot = _bot(ch)

    patch_fetch["catalog"]["pre-orders"] = [_product(1)]
    assert (await scheduler.run_daily_refresh(bot, GUILD))["products"] == 0  # silent seed
    ch.send.assert_not_awaited()

    patch_fetch["catalog"]["pre-orders"] = [_product(1), _product(2, "New Set")]
    patch_fetch["store"] = [_store(2, "2500")]
    result = await scheduler.run_daily_refresh(bot, GUILD)

    assert check.call_args.args[2] == "https://hightidetabletop.com.au"
    assert check.call_args.args[3] == ZoneInfo("Australia/Perth")
    assert {r.url.host for r in patch_fetch["requests"]} == {"hightidetabletop.com.au"}
    handed = post.await_args.args[3]
    assert [(p["external_id"], p["price"]) for p in handed] == [("wp:2", "$25.00")]
    assert result["products"] == 1
    assert ch.send.await_args.kwargs["embed"].description == (
        "[New Set](https://shop.example.com/product/p2/) · $25.00")


# --- council batch: precedence + store fallback matrix ---

def _armed(db, patch_fetch, catalog=None):
    """Seed the default slug, then return so the next check sees `catalog` as new."""
    patch_fetch["catalog"]["pre-orders"] = []
    check_new_products(db, GUILD, BASE, TZ)
    if catalog is not None:
        patch_fetch["catalog"]["pre-orders"] = catalog


def test_store_price_wins_over_content_price(db, patch_fetch):
    _armed(db, patch_fetch, [_product(1, content="<p>→ Ticket: $25.00</p>")])
    patch_fetch["store"] = [_store(1, "1500")]
    assert [p["price"] for p in check_new_products(db, GUILD, BASE, TZ)] == ["$15.00"]


def test_store_httpx_error_falls_back(db, patch_fetch):
    _armed(db, patch_fetch, [_product(1, content="<p>→ Ticket: $25.00</p>")])
    patch_fetch["store"] = httpx.ConnectError("down")
    assert [p["price"] for p in check_new_products(db, GUILD, BASE, TZ)] == ["$25.00"]


def test_store_category_resolution_failure_falls_back(db, patch_fetch, monkeypatch):
    _armed(db, patch_fetch, [_product(1, content="<p>→ Ticket: $25.00</p>")])
    patch_fetch["store"] = [_store(1, "1500")]

    def boom(*a, **k):
        raise FetchError("category not found")

    monkeypatch.setattr(product_service, "resolve_category_id", boom)
    got = check_new_products(db, GUILD, BASE, TZ)
    assert [p["price"] for p in got] == ["$25.00"]
    assert patch_fetch["store_calls"] == []


def test_store_partial_failure_across_slugs(db, patch_fetch, monkeypatch):
    _set_slugs(db, "pre-orders,sealed")
    patch_fetch["catalog"] = {"pre-orders": [], "sealed": []}
    check_new_products(db, GUILD, BASE, TZ)
    patch_fetch["catalog"] = {"pre-orders": [_product(3, content="<p>→ Ticket: $9.00</p>")],
                              "sealed": [_product(4)]}

    def fake_store(base_url, cat_id, client=None):
        if cat_id == 100:  # pre-orders
            raise FetchError("boom")
        return [_store(4, "2500")]

    monkeypatch.setattr(product_service, "fetch_store_products", fake_store)
    got = {p["external_id"]: p["price"] for p in check_new_products(db, GUILD, BASE, TZ)}
    assert got == {"wp:3": "$9.00", "wp:4": "$25.00"}


def test_seeded_slug_transient_fetch_failure_is_silent(db, patch_fetch, monkeypatch):
    _armed(db, patch_fetch, [_product(1)])
    before = {r.external_id for r in db.execute(select(ProductAlert)).scalars()}

    def down(base_url, slug, client=None):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(product_service, "fetch_category_products", down)
    assert check_new_products(db, GUILD, BASE, TZ) == []
    assert {r.external_id for r in db.execute(select(ProductAlert)).scalars()} == before


@pytest.mark.parametrize("exc", [ValueError("bad json"), KeyError("id"), TypeError("shape")])
def test_malformed_slug_response_aborts_only_that_slug(db, patch_fetch, monkeypatch, exc):
    _set_slugs(db, "pre-orders,sealed")
    patch_fetch["catalog"] = {"pre-orders": [], "sealed": []}
    check_new_products(db, GUILD, BASE, TZ)
    real = product_service.fetch_category_products

    def flaky(base_url, slug, client=None):
        if slug == "sealed":
            raise exc
        return real(base_url, slug, client)

    patch_fetch["catalog"]["pre-orders"] = [_product(8)]
    monkeypatch.setattr(product_service, "fetch_category_products", flaky)
    assert [p["external_id"] for p in check_new_products(db, GUILD, BASE, TZ)] == ["wp:8"]


def test_malformed_unseeded_slug_response_does_not_raise_when_another_slug_ok(
        db, patch_fetch, monkeypatch):
    _set_slugs(db, "pre-orders,sealed")
    patch_fetch["catalog"] = {"pre-orders": [_product(1)], "sealed": []}
    real = product_service.fetch_category_products

    def flaky(base_url, slug, client=None):
        if slug == "sealed":
            raise KeyError("id")
        return real(base_url, slug, client)

    monkeypatch.setattr(product_service, "fetch_category_products", flaky)
    assert check_new_products(db, GUILD, BASE, TZ) == []
    assert _markers(db) == {"seeded:pre-orders"}


# --- council batch: price formatter edges ---

@pytest.mark.parametrize("prices,expected", [
    ({"price": "\u0661\u0662\u0663"}, None),  # Arabic-Indic digits
    ({"price": "\uff11\uff12\uff13"}, None),  # fullwidth digits
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


# --- council batch: store fetcher caps ---

def test_fetch_store_products_caps_pages():
    seen = []

    def handler(request):
        seen.append(request)
        page = int(request.url.params["page"])
        return httpx.Response(200, json=[{"id": page}], headers={"X-WP-TotalPages": "500"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        got = fetcher.fetch_store_products(BASE, 7, client=c)
    assert len(seen) == fetcher.MAX_PAGES
    assert len(got) == fetcher.MAX_PAGES


def test_fetch_store_products_truncates_to_max_products(monkeypatch):
    monkeypatch.setattr(fetcher, "MAX_PRODUCTS", 150)
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=[{"id": i} for i in range(100)],
                              headers={"X-WP-TotalPages": "5"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        got = fetcher.fetch_store_products(BASE, 7, client=c)
    assert len(got) == 150
    assert len(seen) == 2


# --- council batch: record_product_alerts ---

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


# --- council batch: _to_alert caps + validation ---

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


def test_malformed_product_on_first_run_alerts_once_fixed(db, patch_fetch):
    bad = _product(2)
    bad["link"] = ["nope"]
    patch_fetch["catalog"]["pre-orders"] = [_product(1), bad]
    assert check_new_products(db, GUILD, BASE, TZ) == []
    assert _alerted(db) == {"wp:1"}  # malformed product is not swallowed by the seed
    patch_fetch["catalog"]["pre-orders"] = [_product(1), _product(2, "Fixed")]
    assert [p["external_id"] for p in check_new_products(db, GUILD, BASE, TZ)] == ["wp:2"]


def test_poison_products_mid_list_do_not_block_later_ones(db, patch_fetch):
    _armed(db, patch_fetch)
    list_link, bool_id, str_id = _product(2), _product(True), _product("4")
    list_link["link"] = ["x"]
    patch_fetch["catalog"]["pre-orders"] = [_product(1), list_link, bool_id, str_id, _product(5)]
    assert [p["external_id"] for p in check_new_products(db, GUILD, BASE, TZ)] == [
        "wp:1", "wp:5"]


# --- council batch: alert cap + poison-pill sends ---

async def test_alert_cap_posts_ten_plus_digest_and_leaves_overflow_unrecorded(db, patch_fetch):
    _armed(db, patch_fetch, [_product(i) for i in range(1, 13)])
    ch = _channel()
    todo = check_new_products(db, GUILD, BASE, TZ)
    assert len(todo) == 12
    assert await post_product_alerts(_bot(ch), db, GUILD, todo) == 10
    assert ch.send.await_count == 11
    digest = ch.send.await_args_list[-1]
    assert digest.args == ("\u2795 2 more new products \u2014 check the shop.",)
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


async def test_digest_failure_is_swallowed(db):
    ch = _channel()
    ch.send.side_effect = [None] * 10 + [discord.HTTPException(MagicMock(status=500), "x")]
    n = await post_product_alerts(_bot(ch), db, GUILD, [_prod(i) for i in range(11)])
    assert n == 10
    assert len(_alerted(db)) == 10


async def test_non_http_send_error_is_recorded_and_does_not_block_later(db):
    ch = _channel()
    ch.send.side_effect = [None, ValueError("poison"), None]
    n = await post_product_alerts(_bot(ch), db, GUILD, [_prod(1), _prod(2), _prod(3)])
    assert n == 2
    assert _alerted(db) == {"wp:1", "wp:2", "wp:3"}


async def test_unrenderable_product_is_recorded_and_does_not_block_later(db):
    ch = _channel()
    broken = _prod(2)
    broken["title"] = None  # embed build raises TypeError
    n = await post_product_alerts(_bot(ch), db, GUILD, [_prod(1), broken, _prod(3)])
    assert n == 2
    assert ch.send.await_count == 2
    assert _alerted(db) == {"wp:1", "wp:2", "wp:3"}
