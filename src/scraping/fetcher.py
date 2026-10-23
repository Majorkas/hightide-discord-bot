import logging
from urllib.parse import urlparse

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

log = logging.getLogger("hightide.fetcher")

UA = {"User-Agent": "hightide-events-bot/0.1 (+discord community bot)"}

MAX_PAGES = 20
MAX_PRODUCTS = 2000
MAX_RESPONSE_BYTES = 10 * 1024 * 1024


class FetchError(Exception):
    pass


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code >= 500 or code == 429
    return False


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=1, max=8),
    retry=retry_if_exception(_is_retryable),
    reraise=True,
)
def _get(client: httpx.Client, url: str, **params) -> httpx.Response:
    resp = client.get(url, params=params, headers=UA, timeout=15.0)
    resp.raise_for_status()
    try:
        length = int(resp.headers.get("Content-Length", "0"))
    except ValueError:
        length = 0
    if length > MAX_RESPONSE_BYTES:
        raise FetchError(f"response too large: {length} bytes")
    return resp


def resolve_events_category_id(base_url: str,
                               client: httpx.Client | None = None) -> int:
    if client is None:
        with httpx.Client() as own:
            return resolve_events_category_id(base_url, own)
    resp = _get(client, f"{base_url}/wp-json/wp/v2/product_cat", slug="events")
    data = resp.json()
    if not data:
        raise FetchError("events category not found")
    return int(data[0]["id"])


def _fetch_all(client: httpx.Client, base_url: str) -> list[dict]:
    cat_id = resolve_events_category_id(base_url, client)
    products: list[dict] = []
    page = 1
    while True:
        resp = _get(
            client,
            f"{base_url}/wp-json/wp/v2/product",
            product_cat=cat_id, per_page=100, page=page, _embed="1",
        )
        batch = resp.json()
        if not isinstance(batch, list):
            raise FetchError("unexpected product response shape")
        products.extend(batch)
        try:
            total_pages = int(resp.headers.get("X-WP-TotalPages", "1"))
        except ValueError:
            total_pages = 1
        total_pages = min(total_pages, MAX_PAGES)
        if page >= total_pages or len(products) >= MAX_PRODUCTS:
            break
        page += 1
    if len(products) > MAX_PRODUCTS:
        log.warning("truncating %d products to cap of %d", len(products), MAX_PRODUCTS)
        products = products[:MAX_PRODUCTS]
    log.info("fetched %d products from events category", len(products))
    return products


def fetch_events(base_url: str, client: httpx.Client | None = None) -> list[dict]:
    """Fetch all Events-category products across all pages."""
    base_url = base_url.rstrip("/")
    if urlparse(base_url).scheme != "https":
        raise FetchError("base_url must use https")
    if client is not None:
        return _fetch_all(client, base_url)
    with httpx.Client() as own:
        return _fetch_all(own, base_url)
