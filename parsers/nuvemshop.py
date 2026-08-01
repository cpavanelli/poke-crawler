"""Nuvemshop / Tiendanube catalog parser for the New-Product Watch (FRD §21).

Collection pages are fully server-rendered: product ids, names, links, prices,
and stock state are all present in the raw HTML, so no headless browser is
needed (FRD §21). Two page quirks drive the extraction rules below:

1. ``div.js-item-product`` also matches the quick-shop modal template, which
   carries an **empty** ``data-product-id``. Only blocks with a non-empty id are
   real products.
2. The "Esgotado" (sold out) label is rendered for **every** product and merely
   hidden with ``style="display:none;"`` when the product is in stock. Stock is
   therefore read from the per-product ``ld+json`` ``offers.availability``, with
   the quick-shop ``data-variants`` JSON as a fallback — never from the label.
   When neither structured signal is readable the product is treated as sold
   out, which is the state that never raises an alert on its own.
"""

from __future__ import annotations

import json
import logging
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from models.product import Product
from parsers.base import CatalogParser

logger = logging.getLogger(__name__)

# Nuvemshop is multi-tenant on custom domains, so the platform cannot be
# detected from the hostname. Add a host here to watch another store.
SUPPORTED_HOSTS = frozenset({"shisuistore.com.br"})

_IN_STOCK_SUFFIX = "instock"
_OUT_OF_STOCK_SUFFIX = "outofstock"


class NuvemshopParser(CatalogParser):
    """Parse Nuvemshop collection pages into the products they list (FRD §21)."""

    def can_handle(self, url: str) -> bool:
        """Return True for a supported Nuvemshop store host, including www."""
        hostname = urlsplit(url).hostname
        if not hostname:
            return False
        return any(
            hostname == host or hostname.endswith(f".{host}") for host in SUPPORTED_HOSTS
        )

    def parse_catalog(self, html: str) -> list[Product]:
        """Return every product on the page, in page order (FRD §21)."""
        soup = BeautifulSoup(html, "html.parser")
        base_url = _page_base_url(soup)

        products: list[Product] = []
        for block in soup.select("div.js-item-product"):
            product = _parse_block(block, base_url)
            if product is not None:
                products.append(product)
        return products


def _page_base_url(soup: BeautifulSoup) -> str:
    """Return the page's own URL, used to absolutise relative product links."""
    canonical = soup.select_one('link[rel="canonical"][href]')
    if canonical is not None:
        return str(canonical.get("href") or "")
    og_url = soup.select_one('meta[property="og:url"][content]')
    if og_url is not None:
        return str(og_url.get("content") or "")
    return ""


def _parse_block(block: Tag, base_url: str) -> Product | None:
    """Build one Product from a product block, or None when it is not one."""
    product_id = str(block.get("data-product-id") or "").strip()
    if not product_id:
        # The quick-shop modal template, not a product.
        return None

    name_el = block.select_one(".js-item-name")
    name = name_el.get_text(strip=True) if name_el is not None else ""
    if not name:
        logger.warning("Nuvemshop product %s has no name; skipped", product_id)
        return None

    url = _parse_url(block, base_url)
    if url is None:
        logger.warning("Nuvemshop product %s has no product link; skipped", product_id)
        return None

    in_stock = _parse_stock(block)
    if in_stock is None:
        # No structured signal: treat as sold out. Sold-out is the silent state
        # (in-stock -> sold-out never alerts), so an unreadable product is kept
        # and tracked rather than dropped.
        logger.warning(
            "Nuvemshop product %s has no stock signal; treated as sold out", product_id
        )
        in_stock = False

    return Product(
        product_id=product_id,
        name=name,
        url=url,
        price=_parse_price(block),
        in_stock=in_stock,
    )


def _parse_url(block: Tag, base_url: str) -> str | None:
    """Return the product page URL, absolutised against the page URL if needed."""
    link = block.select_one("a.item-link[href]") or block.select_one('a[href*="/produtos/"]')
    if link is None:
        return None
    href = str(link.get("href") or "").strip()
    if not href:
        return None
    if href.startswith("//"):
        return f"https:{href}"
    if href.startswith("http") or not base_url:
        return href
    return urljoin(base_url, href)


def _parse_price(block: Tag) -> float | None:
    """Return the listing price from the integer-cents attribute (FRD §5).

    Price is display-only, so an unreadable price never drops the product.
    """
    price_el = block.select_one(".js-price-display[data-product-price]")
    if price_el is None:
        return None
    raw = str(price_el.get("data-product-price") or "").strip()
    try:
        return int(raw) / 100
    except ValueError:
        logger.warning("Nuvemshop price %r is not integer cents; treated as unknown", raw)
        return None


def _parse_stock(block: Tag) -> bool | None:
    """Return the stock state from a structured signal, or None when unreadable."""
    availability = _availability_from_ld_json(block)
    if availability is not None:
        return availability
    return _availability_from_variants(block)


def _availability_from_ld_json(block: Tag) -> bool | None:
    """Read schema.org availability from the block's own ld+json (primary)."""
    script = block.select_one('script[type="application/ld+json"]')
    if script is None:
        return None
    try:
        data = json.loads(script.get_text())
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    offers = data.get("offers")
    if isinstance(offers, list):
        offers = offers[0] if offers else None
    if not isinstance(offers, dict):
        return None

    availability = offers.get("availability")
    if not isinstance(availability, str):
        return None

    normalized = availability.rsplit("/", 1)[-1].strip().lower()
    if normalized == _IN_STOCK_SUFFIX:
        return True
    if normalized == _OUT_OF_STOCK_SUFFIX:
        return False
    return None


def _availability_from_variants(block: Tag) -> bool | None:
    """Read stock from the quick-shop data-variants JSON (fallback)."""
    container = block.select_one("[data-variants]")
    if container is None:
        return None
    try:
        variants = json.loads(str(container.get("data-variants") or ""))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(variants, list) or not variants:
        return None

    available = [
        variant.get("available") for variant in variants if isinstance(variant, dict)
    ]
    if not any(isinstance(value, bool) for value in available):
        return None
    return any(value is True for value in available)
