import httpx
import pytest

from src.scraping.fetcher import (
    MAX_PAGES, MAX_PRODUCTS, MAX_RESPONSE_BYTES, FetchError, fetch_events,
)

BASE = "https://shop.example.com"


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_category_resolution_and_two_page_pagination():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/product_cat"):
            return httpx.Response(200, json=[{"id": 42}])
        page = int(request.url.params["page"])
        return httpx.Response(
            200, json=[{"id": page * 10 + i} for i in range(2)],
            headers={"X-WP-TotalPages": "2"})

    with _client(handler) as c:
        products = fetch_events(BASE + "/", client=c)

    assert [p["id"] for p in products] == [10, 11, 20, 21]
    assert seen[0].url.params["slug"] == "events"
    product_reqs = [r for r in seen if r.url.path.endswith("/product")]
    assert len(product_reqs) == 2
    for r in product_reqs:
        assert r.url.params["product_cat"] == "42"
        assert r.url.params["per_page"] == "100"
        assert r.url.params["_embed"] == "1"
    assert [r.url.params["page"] for r in product_reqs] == ["1", "2"]
    assert str(product_reqs[0].url).startswith(f"{BASE}/wp-json/wp/v2/product?")


def test_empty_category_list_raises():
    with _client(lambda r: httpx.Response(200, json=[])) as c:
        with pytest.raises(FetchError, match="category"):
            fetch_events(BASE, client=c)


def test_non_numeric_total_pages_treated_as_one():
    calls = {"products": 0}

    def handler(request):
        if request.url.path.endswith("/product_cat"):
            return httpx.Response(200, json=[{"id": 1}])
        calls["products"] += 1
        return httpx.Response(200, json=[{"id": 1}], headers={"X-WP-TotalPages": "lots"})

    with _client(handler) as c:
        assert fetch_events(BASE, client=c) == [{"id": 1}]
    assert calls["products"] == 1


def test_404_raises_without_retry():
    count = {"n": 0}

    def handler(request):
        count["n"] += 1
        return httpx.Response(404)

    with _client(handler) as c:
        with pytest.raises(httpx.HTTPStatusError):
            fetch_events(BASE, client=c)
    assert count["n"] == 1


def test_oversized_content_length_raises():
    def handler(request):
        return httpx.Response(
            200, json=[], headers={"Content-Length": str(MAX_RESPONSE_BYTES + 1)})

    with _client(handler) as c:
        with pytest.raises(FetchError, match="too large"):
            fetch_events(BASE, client=c)


def test_non_https_base_url_rejected():
    with pytest.raises(FetchError, match="https"):
        fetch_events("http://shop.example.com")


def test_total_pages_clamped():
    product_calls = {"n": 0}

    def handler(request):
        if request.url.path.endswith("/product_cat"):
            return httpx.Response(200, json=[{"id": 1}])
        product_calls["n"] += 1
        return httpx.Response(200, json=[{"id": product_calls["n"]}],
                              headers={"X-WP-TotalPages": "100000"})

    with _client(handler) as c:
        fetch_events(BASE, client=c)
    assert product_calls["n"] == MAX_PAGES


def test_total_products_hard_capped():
    def handler(request):
        if request.url.path.endswith("/product_cat"):
            return httpx.Response(200, json=[{"id": 1}])
        return httpx.Response(200, json=[{"id": i} for i in range(1500)],
                              headers={"X-WP-TotalPages": "5"})

    with _client(handler) as c:
        assert len(fetch_events(BASE, client=c)) == MAX_PRODUCTS
