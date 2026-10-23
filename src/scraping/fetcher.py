import logging

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

log = logging.getLogger("hightide.fetcher")

UA = {"User-Agent": "hightide-events-bot/0.1 (+discord community bot)"}


class FetchError(Exception):
    pass


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
def _get(client: httpx.Client, url: str, **params) -> httpx.Response:
    resp = client.get(url, params=params, headers=UA, timeout=15.0)
    resp.raise_for_status()
    return resp


def resolve_events_category_id(client: httpx.Client, base_url: str) -> int:
    resp = _get(client, f"{base_url}/wp-json/wp/v2/product_cat", slug="events")
    data = resp.json()
    if not data:
        raise FetchError("events category not found")
    return int(data[0]["id"])


def fetch_events(base_url: str) -> list[dict]:
    """Fetch all Events-category products across all pages."""
    with httpx.Client() as client:
        cat_id = resolve_events_category_id(client, base_url)
        products: list[dict] = []
        page = 1
        while True:
            resp = _get(
                client,
                f"{base_url}/wp-json/wp/v2/product",
                product_cat=cat_id, per_page=100, page=page, _embed="1",
            )
            products.extend(resp.json())
            total_pages = int(resp.headers.get("X-WP-TotalPages", "1"))
            if page >= total_pages:
                break
            page += 1
        log.info("fetched %d products from events category", len(products))
        return products
