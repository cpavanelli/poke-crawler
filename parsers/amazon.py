"""Amazon Brasil product-page parser for the availability watch (FRD §21).

An Amazon product page is watched as a catalog holding exactly one product, so
the existing catalog machinery turns a sold-out -> available transition into a
RESTOCK alert. Pages are server-rendered, but three details make deliberately
narrow rules necessary:

1. The sold-out markup depends on the User-Agent. Under a browser UA the
   ``#availability`` text is absent and the ``#outOfStock`` wrapper id differs.
   Stock is therefore read from buy-box controls and ``#outOfStock``, never from
   availability text or wrapper ids.
2. Every page carries many unrelated carousel prices, some malformed. The price
   is read only from the buy box's core price, falling back to the price on the
   other-sellers link.
3. Watch URLs often carry wish-list tracking parameters, so the product URL is
   rebuilt from the ASIN.

"Available" means any offer at all, including third-party sellers. A product
page holds one product, so its stock state is the whole signal: an unrecognised
page (robot check, missing ASIN or title, unknown buy box) raises instead of
degrading to sold out. The scanner then records a parse error and leaves the
watch state untouched. Robot checks are never bypassed.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from models.product import Product
from parsers.base import CatalogParser
from parsers.money import parse_brl

logger = logging.getLogger(__name__)

SUPPORTED_HOSTS = frozenset({"amazon.com.br"})
CANONICAL_PRODUCT_URL = "https://www.amazon.com.br/dp/{asin}"

_PRODUCT_PATH_RE = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:[/?]|$)")
_ASIN_RE = re.compile(r"[A-Z0-9]{10}")
_BUYBOX = "#desktop_buybox"
# Offer signals win over #outOfStock: any offer makes the item available.
_BUYBOX_OFFER_SELECTORS = ("#add-to-cart-button", "#buybox-see-all-buying-choices")
_OTHER_SELLERS_LINK = "#aod-ingress-link"
_OUT_OF_STOCK = "#outOfStock"
_CORE_PRICE = "#corePrice_feature_div .a-offscreen"


class AmazonParser(CatalogParser):
    """Parse an Amazon Brasil product page into its single product (FRD §21)."""

    def can_handle(self, url: str) -> bool:
        """Return True for an amazon.com.br ``/dp/`` or ``/gp/product/`` URL."""
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
        _check_not_robot_page(soup)

        asin = _parse_asin(soup)
        return [
            Product(
                product_id=asin,
                name=_parse_name(soup),
                url=CANONICAL_PRODUCT_URL.format(asin=asin),
                price=_parse_price(soup),
                in_stock=_parse_stock(soup),
            )
        ]


def _check_not_robot_page(soup: BeautifulSoup) -> None:
    """Raise when Amazon served a robot check instead of the product page."""
    captcha_form = soup.select_one('form[action*="validateCaptcha"]')
    no_product = (
        soup.select_one("#productTitle") is None and soup.select_one("input#ASIN") is None
    )
    if captcha_form is not None or no_product:
        raise ValueError("amazon robot check page")


def _parse_asin(soup: BeautifulSoup) -> str:
    """Return the page ASIN, with the canonical ``/dp/<ASIN>`` link as fallback."""
    asin_input = soup.select_one("input#ASIN")
    if asin_input is not None:
        asin = str(asin_input.get("value") or "").strip()
        if _ASIN_RE.fullmatch(asin):
            return asin

    canonical = soup.select_one('link[rel="canonical"][href]')
    if canonical is not None:
        match = _PRODUCT_PATH_RE.search(str(canonical.get("href") or ""))
        if match is not None:
            return match.group(1)

    raise ValueError("amazon product page has no ASIN")


def _parse_name(soup: BeautifulSoup) -> str:
    """Return the whitespace-collapsed product title."""
    title_el = soup.select_one("#productTitle")
    name = " ".join(title_el.get_text().split()) if title_el is not None else ""
    if not name:
        raise ValueError("amazon product page has no title")
    return name


def _parse_stock(soup: BeautifulSoup) -> bool:
    """Return True for any offer, False for a sold-out buy box, else raise."""
    buybox = soup.select_one(_BUYBOX)
    if buybox is None:
        raise ValueError("amazon buy box state not recognised")

    if any(buybox.select_one(selector) is not None for selector in _BUYBOX_OFFER_SELECTORS):
        return True
    if soup.select_one(_OTHER_SELLERS_LINK) is not None:
        return True
    if buybox.select_one(_OUT_OF_STOCK) is not None:
        return False

    raise ValueError("amazon buy box state not recognised")


def _parse_price(soup: BeautifulSoup) -> float | None:
    """Return the buy-box price, else the other-sellers price, else None."""
    buybox = soup.select_one(_BUYBOX)
    raw = _first_text(buybox.select(_CORE_PRICE)) if buybox is not None else None
    if raw is None:
        raw = _first_text(soup.select(f"{_OTHER_SELLERS_LINK} .a-offscreen"))
    if raw is None:
        # Sold-out and offers-only pages show no price; that is not an error.
        return None

    price = parse_brl(raw)
    if price is None:
        logger.warning("Amazon price %r is not readable BRL; treated as unknown", raw)
    return price


def _first_text(elements: Iterable[Tag]) -> str | None:
    """Return the first non-empty stripped text among elements."""
    for element in elements:
        text = element.get_text(strip=True)
        if text:
            return text
    return None
