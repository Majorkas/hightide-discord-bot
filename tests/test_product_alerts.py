from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import discord
import httpx
import pytest
from sqlalchemy import select

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
    state = {"catalog": {"pre-orders": []}, "requests": []}
    real = fetch_category_products

    def fake(base_url, cat_slug, client=None):
        with _shop(state["catalog"], state["requests"]) as c:
            return real(base_url, cat_slug, client=c)

    monkeypatch.setattr(product_service, "fetch_category_products", fake)
    return state


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
    # first run: unresolvable slug must not leave a half-seeded table
    with pytest.raises(FetchError):
        check_new_products(db, GUILD, BASE, TZ)
    assert db.execute(select(ProductAlert)).scalars().all() == []
    # once seeded, a dead slug is skipped rather than blocking the rest
    cfg.new_stock_cat_slugs = "pre-orders"
    db.commit()
    check_new_products(db, GUILD, BASE, TZ)
    cfg.new_stock_cat_slugs = "pre-orders,typo"
    db.commit()
    patch_fetch["catalog"] = {"pre-orders": [_product(1), _product(2)]}
    assert [p["external_id"] for p in check_new_products(db, GUILD, BASE, TZ)] == ["wp:2"]


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
    assert i.response.send_message.call_args.kwargs["ephemeral"] is True


async def test_config_new_stock_categories_saves_normalised(admin_db):
    cog = AdminCog(MagicMock())
    i = _interaction()
    await cog.set_new_stock_categories.callback(cog, i, "Pre-Orders, sealed")
    with admin_db() as s:
        assert s.get(GuildConfig, GUILD).new_stock_cat_slugs == "pre-orders,sealed"
    assert i.response.send_message.call_args.kwargs["ephemeral"] is True


async def test_config_new_stock_categories_rejects_invalid_without_saving(admin_db):
    cog = AdminCog(MagicMock())
    i = _interaction()
    await cog.set_new_stock_categories.callback(cog, i, "bad slug!")
    with admin_db() as s:
        cfg = s.get(GuildConfig, GUILD)
        assert cfg is None or cfg.new_stock_cat_slugs == "pre-orders"
    assert "Invalid" in i.response.send_message.call_args.args[0]


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
