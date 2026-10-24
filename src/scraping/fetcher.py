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


def resolve_category_id(base_url: str, cat_slug: str,
                        client: httpx.Client | None = None) -> int:
    if client is None:
        with httpx.Client() as own:
            return resolve_category_id(base_url, cat_slug, own)
    resp = _get(client, f"{base_url}/wp-json/wp/v2/product_cat", slug=cat_slug)
    data = resp.json()
    if not data:
        raise FetchError(f"{cat_slug} category not found")
    return int(data[0]["id"])


def resolve_events_category_id(base_url: str,
                               client: httpx.Client | None = None) -> int:
    return resolve_category_id(base_url, "events", client)


def _fetch_all(client: httpx.Client, base_url: str, cat_slug: str) -> list[dict]:
    cat_id = resolve_category_id(base_url, cat_slug, client)
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
    log.info("fetched %d products from %s category", len(products), cat_slug)
    return products


def fetch_category_products(base_url: str, cat_slug: str,
                            client: httpx.Client | None = None) -> list[dict]:
    """Fetch all products in a category (by slug) across all pages."""
    base_url = base_url.rstrip("/")
    if urlparse(base_url).scheme != "https":
        raise FetchError("base_url must use https")
    if client is not None:
        return _fetch_all(client, base_url, cat_slug)
    with httpx.Client() as own:
        return _fetch_all(own, base_url, cat_slug)


def fetch_latest_products(base_url: str,
                          client: httpx.Client | None = None) -> list[dict]:
    """Fetch the newest 100 products (the shop's New In ordering); single page."""
    base_url = base_url.rstrip("/")
    if urlparse(base_url).scheme != "https":
        raise FetchError("base_url must use https")

    def _latest(c: httpx.Client) -> list[dict]:
        resp = _get(c, f"{base_url}/wp-json/wp/v2/product",
                    orderby="date", order="desc", per_page=100, page=1, _embed="1")
        batch = resp.json()
        if not isinstance(batch, list):
            raise FetchError("unexpected latest-products response shape")
        log.info("fetched %d latest products", len(batch))
        return batch[:MAX_PRODUCTS]

    if client is not None:
        return _latest(client)
    with httpx.Client() as own:
        return _latest(own)


def fetch_category_ids(base_url: str,
                       client: httpx.Client | None = None) -> dict[str, int]:
    """Map of every product category slug -> id."""
    base_url = base_url.rstrip("/")
    if urlparse(base_url).scheme != "https":
        raise FetchError("base_url must use https")

    def _cats(c: httpx.Client) -> dict[str, int]:
        out: dict[str, int] = {}
        page = 1
        while True:
            resp = _get(c, f"{base_url}/wp-json/wp/v2/product_cat", per_page=100, page=page)
            batch = resp.json()
            if not isinstance(batch, list):
                raise FetchError("unexpected category response shape")
            for cat in batch:
                if not isinstance(cat, dict):
                    continue
                slug, cid = cat.get("slug"), cat.get("id")
                if isinstance(slug, str) and isinstance(cid, int) and not isinstance(cid, bool):
                    out.setdefault(slug, cid)
            try:
                total_pages = int(resp.headers.get("X-WP-TotalPages", "1"))
            except ValueError:
                total_pages = 1
            if page >= min(total_pages, MAX_PAGES):
                break
            page += 1
        return out

    if client is not None:
        return _cats(client)
    with httpx.Client() as own:
        return _cats(own)


def fetch_store_products_latest(base_url: str,
                                client: httpx.Client | None = None) -> list[dict]:
    """Fetch the newest 100 WooCommerce Store API products (with prices); single page."""
    base_url = base_url.rstrip("/")
    if urlparse(base_url).scheme != "https":
        raise FetchError("base_url must use https")

    def _latest(c: httpx.Client) -> list[dict]:
        resp = _get(c, f"{base_url}/wp-json/wc/store/v1/products",
                    orderby="date", order="desc", per_page=100, page=1)
        batch = resp.json()
        if not isinstance(batch, list):
            raise FetchError("unexpected store latest-products response shape")
        return batch[:MAX_PRODUCTS]

    if client is not None:
        return _latest(client)
    with httpx.Client() as own:
        return _latest(own)


def fetch_events(base_url: str, client: httpx.Client | None = None) -> list[dict]:
    """Fetch all Events-category products across all pages."""
    return fetch_category_products(base_url, "events", client)
