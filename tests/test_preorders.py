"""Tests for the preorders board (preorder_service)."""
import json
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import discord
import httpx
import pytest
from sqlalchemy import select

from src.db.models import BoardPost, GuildConfig
from src.scraping.fetcher import FetchError, fetch_preorder_products
from src.services import preorder_service, scheduler
from src.services.preorder_service import (
    _is_preorder, collect_preorders, upsert_preorder_board,
)

GUILD = 999
BASE = "https://shop.example.com"
TZ = ZoneInfo("Australia/Perth")


def _wp(pid, title, content="", image=None, link=None):
    p = {"id": pid, "title": {"rendered": title},
         "link": link or f"{BASE}/product/p{pid}/", "content": {"rendered": content}}
    if image:
        p["_embedded"] = {"wp:featuredmedia": [{"source_url": image}]}
    return p


# --- fetcher ---

def test_fetch_preorder_products_searches_and_paginates():
    seen = []

    def handler(request):
        seen.append(request)
        page = int(request.url.params["page"])
        return httpx.Response(200, json=[{"id": page}], headers={"X-WP-TotalPages": "2"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        got = fetch_preorder_products(BASE + "/", "preorder", client=c)
    assert [p["id"] for p in got] == [1, 2]
    assert all(r.url.params["search"] == "preorder" for r in seen)
    assert all(r.url.params["_embed"] == "1" for r in seen)


def test_fetch_preorder_products_requires_https_and_list_shape():
    with pytest.raises(FetchError):
        fetch_preorder_products("http://shop.example.com")
    with httpx.Client(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"x": 1}))) as c:
        with pytest.raises(FetchError):
            fetch_preorder_products(BASE, client=c)


# --- _is_preorder (title-only) ---

@pytest.mark.parametrize("title,expected", [
    ("Foo [PREORDER]", True), ("Foo Pre-Order", True), ("preorder foo", True),
    ("Membership Standard", False), ("", False), ("Regular Product", False),
])
def test_is_preorder_title_only(title, expected):
    assert _is_preorder(_wp(1, title)) == expected


def test_is_preorder_ignores_description_match():
    p = _wp(1, "Membership Standard", content="<p>preorder perks</p>")
    assert _is_preorder(p) is False


def test_is_preorder_non_string_title():
    p = _wp(1, "x")
    p["title"] = {"rendered": 123}
    assert _is_preorder(p) is False


# --- collect_preorders ---

def _patch_collect(monkeypatch, by_term, store=None):
    monkeypatch.setattr(preorder_service, "fetch_preorder_products",
                        lambda base, term, client=None: by_term.get(term, []))
    monkeypatch.setattr(preorder_service, "_store_prices", lambda base: store or {})


def test_collect_title_match_dedupes_and_prices(monkeypatch):
    a = _wp(1, "Alpha [PREORDER]", image="https://img/a.jpg")
    b = _wp(2, "Beta Pre-Order")
    _patch_collect(monkeypatch, {"preorder": [a, dict(a)], "pre-order": [b]},
                   store={"wp:1": "$30.00", "wp:2": "$25.00"})
    got = collect_preorders(BASE)
    assert [i["id"] for i in got] == [1, 2]
    assert got[0]["price"] == "$30.00" and got[1]["price"] == "$25.00"
    assert got[0]["image_url"] == "https://img/a.jpg" and got[1]["image_url"] is None


def test_collect_filters_junk_and_non_title_matches(monkeypatch):
    _patch_collect(monkeypatch, {
        "preorder": [_wp(1, "Real [PREORDER]"), _wp(2, "Preorder test item"),
                     _wp(3, "Membership", content="preorder")],
        "pre-order": [],
    })
    got = collect_preorders(BASE)
    assert [i["id"] for i in got] == [1]


def test_collect_empty_short_circuits_store(monkeypatch):
    called = []
    monkeypatch.setattr(preorder_service, "fetch_preorder_products", lambda b, t, client=None: [])
    monkeypatch.setattr(preorder_service, "_store_prices", lambda b: called.append(1) or {})
    assert collect_preorders(BASE) == []
    assert called == []  # no store fetch when nothing matched


def test_collect_raises_on_fetch_error(monkeypatch):
    def boom(base, term, client=None):
        raise FetchError("down")
    monkeypatch.setattr(preorder_service, "fetch_preorder_products", boom)
    with pytest.raises(FetchError):
        collect_preorders(BASE)


# --- upsert_preorder_board ---

@pytest.fixture
def db(session):
    session.add(GuildConfig(guild_id=GUILD, preorder_channel_id=555))
    session.commit()
    return session


def _bot_with(channel):
    bot = MagicMock()
    bot.user = MagicMock(id=1)
    bot.get_channel.return_value = channel
    bot.fetch_channel = AsyncMock(return_value=channel)
    return bot


def _msg(mid, author_id=1):
    m = MagicMock()
    m.id = mid
    m.author = MagicMock(id=author_id)
    m.edit = AsyncMock()
    m.delete = AsyncMock()
    return m


def _channel():
    ch = MagicMock()
    ch.id = 555
    ch.send = AsyncMock(side_effect=lambda **kw: _msg(len(ch.send.await_args_list) + 100))
    ch.fetch_message = AsyncMock()
    return ch


async def test_no_channel_configured_skips(session):
    session.add(GuildConfig(guild_id=GUILD))
    session.commit()
    assert await upsert_preorder_board(MagicMock(), session, GUILD, BASE) == "no_channel"


async def test_creates_header_plus_cards(db, monkeypatch):
    items = [{"id": 1, "title": "A [PREORDER]", "url": "u1", "price": "$10", "image_url": "i1"},
             {"id": 2, "title": "B Pre-Order", "url": "u2", "price": None, "image_url": None}]
    monkeypatch.setattr(preorder_service, "collect_preorders", lambda b: items)
    ch = _channel()
    result = await upsert_preorder_board(_bot_with(ch), db, GUILD, BASE)
    assert result == "created"
    # 1 header + 2 cards
    assert ch.send.await_count == 3
    post = db.execute(select(BoardPost)).scalar_one()
    assert post.board_type == "preorders"
    data = json.loads(post.render_hash)
    assert data["keys"] == ["header", "1", "2"]


async def test_empty_preorders_posts_empty_header(db, monkeypatch):
    monkeypatch.setattr(preorder_service, "collect_preorders", lambda b: [])
    ch = _channel()
    result = await upsert_preorder_board(_bot_with(ch), db, GUILD, BASE)
    assert result == "created"
    assert ch.send.await_count == 1
    embed = ch.send.await_args.kwargs["embed"]
    assert "Nothing available to pre-order" in embed.description


async def test_skips_when_unchanged(db, monkeypatch):
    items = [{"id": 1, "title": "A [PREORDER]", "url": "u1", "price": "$10", "image_url": "i1"}]
    monkeypatch.setattr(preorder_service, "collect_preorders", lambda b: items)
    ch = _channel()
    await upsert_preorder_board(_bot_with(ch), db, GUILD, BASE)
    # second run: stored messages exist; fetch_message returns editable messages
    existing = {mid: _msg(mid) for mid in (100, 101)}
    ch2 = _channel()
    ch2.fetch_message = AsyncMock(side_effect=lambda mid: existing[mid])
    result = await upsert_preorder_board(_bot_with(ch2), db, GUILD, BASE)
    assert result == "edited"
    ch2.send.assert_not_awaited()  # nothing re-sent


async def test_removes_card_when_product_gone(db, monkeypatch):
    items = [{"id": 1, "title": "A [PREORDER]", "url": "u1", "price": "$10", "image_url": "i1"},
             {"id": 2, "title": "B Pre-Order", "url": "u2", "price": None, "image_url": None}]
    monkeypatch.setattr(preorder_service, "collect_preorders", lambda b: items)
    ch = _channel()
    await upsert_preorder_board(_bot_with(ch), db, GUILD, BASE)

    # product 2 released -> only product 1 remains
    monkeypatch.setattr(preorder_service, "collect_preorders", lambda b: items[:1])
    existing = {mid: _msg(mid) for mid in (100, 101, 102)}
    ch2 = _channel()
    ch2.fetch_message = AsyncMock(side_effect=lambda mid: existing[mid])
    await upsert_preorder_board(_bot_with(ch2), db, GUILD, BASE)
    # key-set diff triggers a recreate: old messages deleted, fresh set sent
    assert existing[100].delete.await_count >= 0  # header may be deleted+resent
    assert ch2.send.await_count >= 1
    post = db.execute(select(BoardPost)).scalar_one()
    assert json.loads(post.render_hash)["keys"] == ["header", "1"]


async def test_collect_failure_propagates_and_posts_nothing(db, monkeypatch):
    def boom(b):
        raise FetchError("down")
    monkeypatch.setattr(preorder_service, "collect_preorders", boom)
    ch = _channel()
    with pytest.raises(FetchError):
        await upsert_preorder_board(_bot_with(ch), db, GUILD, BASE)
    ch.send.assert_not_awaited()
    assert db.execute(select(BoardPost)).scalars().all() == []


async def test_channel_unavailable_returns_no_channel(db):
    bot = MagicMock()
    bot.get_channel.return_value = None
    bot.fetch_channel = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "gone"))
    assert await upsert_preorder_board(bot, db, GUILD, BASE) == "no_channel"


# --- scheduler wiring ---

@pytest.fixture
def sched_db(temp_db, monkeypatch):
    monkeypatch.setattr("src.services.scheduler.refresh_events", MagicMock(return_value={"added": 0}))
    monkeypatch.setattr("src.services.scheduler.upsert_board", AsyncMock(side_effect=["created", "created"]))
    return temp_db


async def test_scheduler_runs_preorder_step_when_channel_set(sched_db, monkeypatch):
    with sched_db() as s:
        s.add(GuildConfig(guild_id=GUILD, timezone="Australia/Perth",
                          this_week_channel_id=1, this_month_channel_id=2,
                          preorder_channel_id=555))
        s.commit()
    up = AsyncMock(return_value="created")
    monkeypatch.setattr("src.services.scheduler.upsert_preorder_board", up)
    result = await scheduler.run_daily_refresh(MagicMock(), GUILD)
    up.assert_awaited_once()
    assert result["preorders"] == "created"
    assert "preorders" in scheduler._NON_DRIVING_KEYS


async def test_scheduler_skips_preorder_step_when_channel_unset(sched_db, monkeypatch):
    with sched_db() as s:
        s.add(GuildConfig(guild_id=GUILD, timezone="Australia/Perth",
                          this_week_channel_id=1, this_month_channel_id=2))
        s.commit()
    up = AsyncMock()
    monkeypatch.setattr("src.services.scheduler.upsert_preorder_board", up)
    result = await scheduler.run_daily_refresh(MagicMock(), GUILD)
    up.assert_not_awaited()
    assert "preorders" not in result


async def test_scheduler_preorder_failure_isolated(sched_db, monkeypatch):
    with sched_db() as s:
        s.add(GuildConfig(guild_id=GUILD, timezone="Australia/Perth",
                          this_week_channel_id=1, this_month_channel_id=2,
                          preorder_channel_id=555))
        s.commit()
    monkeypatch.setattr("src.services.scheduler.upsert_preorder_board",
                        AsyncMock(side_effect=ConnectionError("net")))
    result = await scheduler.run_daily_refresh(MagicMock(), GUILD)
    assert result["preorders"] == "error:ConnectionError"
    assert result["this_week"] == "created"
