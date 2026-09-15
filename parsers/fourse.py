"""Fourse catalog parser for the New-Product Watch (FRD §21).

Fourse is a WooCommerce store using the Ecomus theme. Its pages are fully
server-rendered, but three theme details make deliberately narrow selectors
necessary:

1. The header search modal repeats five full ``li.product`` recommendation
   blocks and also renders a second, structurally unrelated suggestion list.
   Products are therefore selected only below ``#ecomus-shop-content``.
2. Sale prices contain both ``<del>`` and ``<ins>`` amounts, so the current
   ``<ins>`` amount must win.
3. Each product also contains installment and Pix ``p.price`` elements, while
   the real ``span.price`` has a ``/ UN`` suffix. The amount is read from the
   real span's inner WooCommerce ``bdi`` element.

WooCommerce's exact ``instock`` / ``outofstock`` class token is the only stock
signal. A missing signal is treated as sold out, the state that never alerts on
its own.
"""

from __future__ import annotations

import logging
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from models.product import Product
from parsers.base import CatalogParser
from parsers.money import parse_brl

logger = logging.getLogger(__name__)

SUPPORTED_HOSTS = frozenset({"fourse.com.br"})

_CANONICAL_URL = "https://fourse.com.br/"
_GRID_SELECTOR = "#ecomus-shop-content ul.products > li.product"
_IN_STOCK_CLASS = "instock"
_OUT_OF_STOCK_CLASS = "outofstock"
_POST_ID_PREFIX = "post-"


class FourseParser(CatalogParser):
    """Parse Fourse collection pages into the products they list (FRD §21)."""

    def can_handle(self, url: str) -> bool:
        """Return True for the Fourse host and any of its subdomains."""
        hostname = urlsplit(url).hostname
        if not hostname:
            return False
        return any(
            hostname == host or hostname.endswith(f".{host}") for host in SUPPORTED_HOSTS
        )

    def parse_catalog(self, html: str) -> list[Product]:
        """Return every grid product on page 1, in page order (FRD §21)."""
        soup = BeautifulSoup(html, "html.parser")

        products: list[Product] = []
        for block in soup.select(_GRID_SELECTOR):
            product = _parse_block(block)
            if product is not None:
                products.append(product)
        return products


def _parse_block(block: Tag) -> Product | None:
    """Build one Product from a grid block, or None when required data is absent."""
    name = _parse_name(block)

    product_id = _parse_id(block)
    if product_id is None:
        # Selection is already scoped to the grid, so every block reaching here
        # is a real product. Unlike the Nuvemshop modal template, an unreadable
        # id drops a product, so it must never be skipped silently.
        logger.warning("Fourse product %r has no id; skipped", name or "<unnamed>")
        return None

    if name is None:
        logger.warning("Fourse product %s has no name; skipped", product_id)
        return None

    url = _parse_url(block)
    if url is None:
        logger.warning("Fourse product %s has no product link; skipped", product_id)
        return None

    in_stock = _parse_stock(block)
    if in_stock is None:
        logger.warning(
            "Fourse product %s has no stock signal; treated as sold out", product_id
        )
        in_stock = False

    return Product(
        product_id=product_id,
        name=name,
        url=url,
        price=_parse_price(block),
        in_stock=in_stock,
    )


def _parse_id(block: Tag) -> str | None:
    """Return the WooCommerce post id, with ``data-product_id`` as fallback."""
    classes = block.get("class") or []
    if isinstance(classes, str):
        classes = classes.split()
    for token in classes:
        token = str(token)
        if token.startswith(_POST_ID_PREFIX):
            product_id = token.removeprefix(_POST_ID_PREFIX)
            if product_id.isdigit():
                return product_id

    product_el = block.select_one("[data-product_id]")
    if product_el is None:
        return None
    product_id = str(product_el.get("data-product_id") or "").strip()
    return product_id or None


def _parse_name(block: Tag) -> str | None:
    """Return the WooCommerce loop title, with HTML entities already decoded."""
    name_el = block.select_one("h2.woocommerce-loop-product__title a")
    if name_el is None:
        return None
    name = name_el.get_text(strip=True)
    return name or None


def _parse_url(block: Tag) -> str | None:
    """Return the product URL, defensively resolving a relative href."""
    link = block.select_one("a.woocommerce-loop-product__link[href]")
    if link is None:
        return None
    href = str(link.get("href") or "").strip()
    if not href:
        return None
    if href.startswith("//"):
        return f"https:{href}"
    if href.startswith("http"):
        return href
    return urljoin(_CANONICAL_URL, href)


def _parse_price(block: Tag) -> float | None:
    """Return the real WooCommerce price, preferring the current sale amount."""
    price_el = block.select_one("span.price")
    amount_el = None
    if price_el is not None:
        amount_el = price_el.select_one("ins .woocommerce-Price-amount bdi")
        if amount_el is None:
            amount_el = price_el.select_one(".woocommerce-Price-amount bdi")

    raw = amount_el.get_text() if amount_el is not None else ""
    price = parse_brl(raw)
    if price is None:
        logger.warning("Fourse price %r is not readable BRL; treated as unknown", raw)
    return price


def _parse_stock(block: Tag) -> bool | None:
    """Return stock from exact WooCommerce class tokens, or None if ambiguous."""
    classes = block.get("class") or []
    if isinstance(classes, str):
        classes = classes.split()
    tokens = {str(token) for token in classes}
    if _OUT_OF_STOCK_CLASS in tokens:
        return False
    if _IN_STOCK_CLASS in tokens:
        return True
    return None
