"""Copag Loja product-page parser for the availability watch (FRD §21).

Copag Loja runs on legacy VTEX. A product page is watched as a catalog holding
exactly one product, so the existing catalog machinery turns a sold-out ->
available transition into a RESTOCK alert, exactly as for Amazon. Pages are
fully server-rendered and carry two independent descriptions of the product:

1. The inline ``var skuJson_0 = {...}`` object in ``<head>``, the authoritative
   source: product id, name, per-SKU ``available`` flag, and price in cents
   (``bestPrice``).
2. The ``schema.org/Product`` microdata block, used as the fallback when the
   inline object is absent or unreadable.

Two VTEX details drive the rules below:

- A sold-out SKU still carries a price, but a placeholder one: VTEX renders
  ``R$ 9.999.876,00`` (``bestPrice`` 999987600). A price is therefore only ever
  read from an **available** SKU; a sold-out product has no price. The microdata
  block agrees: it omits ``itemprop="price"`` entirely when sold out.
- A product may hold several SKUs. It counts as available when any SKU is, and
  the displayed price is the lowest among the available ones.

Because the page holds one product, its stock state is the whole signal: an
unrecognised page (no ``skuJson`` and no microdata availability) raises instead
of degrading to sold out. The scanner then records a parse error and leaves the
watch state untouched.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from models.product import Product
from parsers.base import CatalogParser
from parsers.money import parse_brl

logger = logging.getLogger(__name__)

SUPPORTED_HOSTS = frozenset({"copagloja.com.br"})

_PRODUCT_PATH_RE = re.compile(r"/[^/]+/p/?$")
_SKU_JSON_RE = re.compile(r"var\s+skuJson_0\s*=\s*")
_IN_STOCK_SUFFIX = "instock"
_OUT_OF_STOCK_SUFFIX = "outofstock"


class CopagParser(CatalogParser):
    """Parse a Copag Loja product page into its single product (FRD §21)."""

    def can_handle(self, url: str) -> bool:
        """Return True for a copagloja.com.br ``/<slug>/p`` product URL."""
        parts = urlsplit(url)
        hostname = parts.hostname
        if not hostname:
            return False
        if not any(
            hostname == host or hostname.endswith(f".{host}") for host in SUPPORTED_HOSTS
        ):
            return False
        return _PRODUCT_PATH_RE.search(parts.path) is not None

    def parse_catalog(self, html: str) -> list[Product]:
        """Return exactly one product, or raise ValueError for an unusable page."""
        soup = BeautifulSoup(html, "html.parser")
        sku_json = _parse_sku_json(html)
        micro = _Microdata(soup)

        if sku_json is None and micro.availability() is None:
            # A store "product not found" page reaching the parser: never report
            # it as a sold-out product, which would seed or change the watch.
            raise ValueError("copag product page not recognised (not published?)")

        in_stock = _parse_stock(sku_json, micro)
        return [
            Product(
                product_id=_parse_id(sku_json, micro),
                name=_parse_name(sku_json, micro),
                url=_parse_url(soup, micro),
                price=_parse_price(sku_json, micro) if in_stock else None,
                in_stock=in_stock,
            )
        ]


class _Microdata:
    """The ``schema.org/Product`` block, read as the fallback source."""

    def __init__(self, soup: BeautifulSoup) -> None:
        self._block = soup.select_one('[itemtype$="schema.org/Product"]')

    def prop(self, name: str) -> str | None:
        """Return a ``meta[itemprop=<name>]`` content value from the block."""
        if self._block is None:
            return None
        element = self._block.select_one(f'meta[itemprop="{name}"][content]')
        if element is None:
            return None
        value = str(element.get("content") or "").strip()
        return value or None

    def availability(self) -> bool | None:
        """Return stock from ``offers.availability``, or None when absent."""
        if self._block is None:
            return None
        link = self._block.select_one('[itemprop="availability"]')
        if link is None:
            return None
        href = str(link.get("href") or link.get("content") or "").strip().lower()
        if href.endswith(_OUT_OF_STOCK_SUFFIX):
            return False
        if href.endswith(_IN_STOCK_SUFFIX):
            return True
        return None


def _parse_sku_json(html: str) -> dict[str, Any] | None:
    """Return the inline ``skuJson_0`` object, or None when it is unreadable."""
    match = _SKU_JSON_RE.search(html)
    if match is None:
        return None
    try:
        # More JS follows the object, so decode only as far as it extends.
        value, _ = json.JSONDecoder().raw_decode(html, match.end())
    except ValueError:
        logger.warning("Copag skuJson_0 is not readable JSON; ignored")
        return None
    return value if isinstance(value, dict) else None


def _available_skus(sku_json: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return the SKU objects whose ``available`` flag is true."""
    if sku_json is None:
        return []
    skus = sku_json.get("skus")
    if not isinstance(skus, list):
        return []
    return [sku for sku in skus if isinstance(sku, dict) and sku.get("available") is True]


def _parse_stock(sku_json: dict[str, Any] | None, micro: _Microdata) -> bool:
    """Return True when any SKU is available, else the page's own stock flag."""
    if sku_json is not None:
        if _available_skus(sku_json):
            return True
        if isinstance(sku_json.get("available"), bool):
            return bool(sku_json["available"])

    availability = micro.availability()
    if availability is not None:
        return availability

    raise ValueError("copag product page stock state not recognised")


def _parse_id(sku_json: dict[str, Any] | None, micro: _Microdata) -> str:
    """Return the VTEX product id, with the microdata ``productID`` as fallback."""
    if sku_json is not None:
        product_id = sku_json.get("productId")
        if isinstance(product_id, (int, str)) and str(product_id).strip():
            return str(product_id).strip()

    product_id = micro.prop("productID")
    if product_id:
        return product_id

    raise ValueError("copag product page has no product id")


def _parse_name(sku_json: dict[str, Any] | None, micro: _Microdata) -> str:
    """Return the whitespace-collapsed product name."""
    raw = None
    if sku_json is not None and isinstance(sku_json.get("name"), str):
        raw = sku_json["name"]
    if not raw:
        raw = micro.prop("name")

    name = " ".join(raw.split()) if raw else ""
    if not name:
        raise ValueError("copag product page has no name")
    return name


def _parse_url(soup: BeautifulSoup, micro: _Microdata) -> str:
    """Return the canonical product URL, so tracking parameters are dropped."""
    url = micro.prop("url")
    if url:
        return url

    canonical = soup.select_one('link[rel="canonical"][href]')
    if canonical is not None:
        href = str(canonical.get("href") or "").strip()
        if href:
            return href

    og_url = soup.select_one('meta[property="og:url"][content]')
    if og_url is not None:
        href = str(og_url.get("content") or "").strip()
        if href:
            return href

    raise ValueError("copag product page has no product URL")


def _parse_price(sku_json: dict[str, Any] | None, micro: _Microdata) -> float | None:
    """Return the lowest available-SKU price, else the microdata price, else None."""
    prices = [
        sku["bestPrice"] / 100
        for sku in _available_skus(sku_json)
        if isinstance(sku.get("bestPrice"), (int, float))
        and not isinstance(sku.get("bestPrice"), bool)
    ]
    if prices:
        return round(min(prices), 2)

    raw = micro.prop("price")
    if raw is None:
        # An available product with no readable price still alerts, without one.
        return None

    # Microdata prices use a dot decimal separator ("62.99"), which parse_brl
    # would read as a thousands group; only fall back to it for "R$ 62,99".
    try:
        return float(raw)
    except ValueError:
        pass

    price = parse_brl(raw)
    if price is None:
        logger.warning("Copag price %r is not readable; treated as unknown", raw)
    return price
