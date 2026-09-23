"""Tests for the Copag Loja (VTEX) availability parser (FRD §21)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from parsers.amazon import AmazonParser
from parsers.copag import CopagParser
from parsers.fourse import FourseParser
from parsers.nuvemshop import NuvemshopParser
from services.scanner import DEFAULT_CATALOG_PARSERS

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "copag"
SOLD_OUT_HTML = (FIXTURE_DIR / "sold_out.html").read_text(encoding="utf-8")
IN_STOCK_HTML = (FIXTURE_DIR / "in_stock.html").read_text(encoding="utf-8")
NOT_FOUND_HTML = (FIXTURE_DIR / "not_found.html").read_text(encoding="utf-8")

TARGET_URL = "https://www.copagloja.com.br/treinador-avancado-pokemon-30-anos/p"

# The placeholder price VTEX renders for a sold-out SKU.
SOLD_OUT_PLACEHOLDER_CENTS = 999987600

MICRODATA = """
  <div itemscope="itemscope" itemtype="http://schema.org/Product">
    <meta itemprop="name" content="Produto Microdata"/>
    <meta itemprop="url" content="{url}"/>
    <meta itemprop="productID" content="4242"/>
    <div itemtype="http://schema.org/Offer" itemscope="itemscope" itemprop="offers">
      {availability}{price}
    </div>
  </div>
"""


def _parse(html: str):
    return CopagParser().parse_catalog(html)


def _sku(*, available: bool, best_price: int, sku: int = 1) -> dict:
    return {
        "sku": sku,
        "skuname": "SKU Teste",
        "available": available,
        "availablequantity": 99999 if available else 0,
        "bestPriceFormated": "R$ 62,99",
        "bestPrice": best_price,
        "seller": "copag",
    }


def _page(
    *,
    sku_json: dict | None = None,
    availability: str | None = "OutOfStock",
    price: str | None = None,
    url: str = TARGET_URL,
    canonical: bool = False,
) -> str:
    """Minimal VTEX product page with the two sources the parser reads."""
    script = (
        ""
        if sku_json is None
        else (
            "<script>var skuJson_0 = "
            + json.dumps(sku_json)
            + ";CATALOG_SDK.setProductWithVariationsCache(skuJson_0.productId, skuJson_0);"
            " var skuJson = skuJson_0;</script>"
        )
    )
    micro = (
        ""
        if availability is None and price is None
        else MICRODATA.format(
            url=url,
            availability=(
                ""
                if availability is None
                else f'<link itemprop="availability" href="http://schema.org/{availability}"/>'
            ),
            price="" if price is None else f'<meta itemprop="price" content="{price}"/>',
        )
    )
    canonical_html = (
        f'<link rel="canonical" href="{url}"/>' if canonical else ""
    )
    return f"<html><head>{canonical_html}{script}</head><body>{micro}</body></html>"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (TARGET_URL, True),
        ("https://copagloja.com.br/baralho-texas-hold-em-azul/p", True),
        ("https://www.copagloja.com.br/treinador-avancado-pokemon-30-anos/p/", True),
        (f"{TARGET_URL}?utm_source=newsletter", True),
        ("https://www.copagloja.com.br/pokemon", False),
        ("https://www.copagloja.com.br/", False),
        ("https://www.copagloja.com.br/busca/p/extra", False),
        ("https://notcopagloja.com.br/produto/p", False),
        ("https://fourse.com.br/item/produto/", False),
        ("https://www.amazon.com.br/dp/B0H78BB9TY", False),
        ("not-a-url", False),
    ],
)
def test_can_handle(url: str, expected: bool) -> None:
    assert CopagParser().can_handle(url) is expected


def test_parser_is_registered_for_catalog_watches() -> None:
    assert CopagParser in DEFAULT_CATALOG_PARSERS


def test_other_catalog_parsers_do_not_claim_copag_urls() -> None:
    assert FourseParser().can_handle(TARGET_URL) is False
    assert NuvemshopParser().can_handle(TARGET_URL) is False
    assert AmazonParser().can_handle(TARGET_URL) is False


def test_sold_out_target_fixture() -> None:
    products = _parse(SOLD_OUT_HTML)

    assert len(products) == 1
    product = products[0]
    assert product.product_id == "2687"
    assert product.name == "Pokémon Celebração de 30 Anos - Treinador Avançado"
    assert product.url == TARGET_URL
    assert product.in_stock is False
    assert product.price is None


def test_sold_out_fixture_still_carries_the_vtex_placeholder_price() -> None:
    # The trap: a sold-out SKU is priced R$ 9.999.876,00, so a price must never
    # be read from an unavailable SKU.
    assert f'"bestPrice":{SOLD_OUT_PLACEHOLDER_CENTS}' in SOLD_OUT_HTML.replace(" ", "")
    assert _parse(SOLD_OUT_HTML)[0].price is None


def test_in_stock_fixture() -> None:
    product = _parse(IN_STOCK_HTML)[0]

    assert product.product_id == "1519"
    assert product.name == "Baralho de Poker Texas Hold’em Azul"
    assert product.url == "https://www.copagloja.com.br/baralho-texas-hold-em-azul/p"
    assert product.in_stock is True
    assert product.price == 62.99


def test_any_available_sku_makes_the_product_available_at_its_lowest_price() -> None:
    sku_json = {
        "productId": 2687,
        "name": "Produto Multi-SKU",
        "available": False,
        "skus": [
            _sku(available=False, best_price=SOLD_OUT_PLACEHOLDER_CENTS, sku=1),
            _sku(available=True, best_price=8990, sku=2),
            _sku(available=True, best_price=7550, sku=3),
        ],
    }

    product = _parse(_page(sku_json=sku_json))[0]

    assert product.in_stock is True
    assert product.price == 75.50


def test_sku_json_wins_over_stale_microdata_availability() -> None:
    sku_json = {
        "productId": 2687,
        "name": "Produto",
        "available": True,
        "skus": [_sku(available=True, best_price=6299)],
    }

    product = _parse(_page(sku_json=sku_json, availability="OutOfStock"))[0]

    assert product.in_stock is True
    assert product.price == 62.99
    assert product.product_id == "2687"
    assert product.name == "Produto"


@pytest.mark.parametrize(
    ("availability", "expected"),
    [("InStock", True), ("OutOfStock", False)],
)
def test_microdata_is_the_fallback_when_sku_json_is_absent(
    availability: str, expected: bool
) -> None:
    product = _parse(_page(availability=availability, price="62.99"))[0]

    assert product.in_stock is expected
    assert product.product_id == "4242"
    assert product.name == "Produto Microdata"
    assert product.url == TARGET_URL
    assert product.price == (62.99 if expected else None)


def test_unreadable_sku_json_falls_back_to_microdata(
    caplog: pytest.LogCaptureFixture,
) -> None:
    html = _page(availability="InStock", price="62.99").replace(
        "<head>", "<head><script>var skuJson_0 = {not json};</script>"
    )

    with caplog.at_level(logging.WARNING, logger="parsers.copag"):
        product = _parse(html)[0]

    assert product.in_stock is True
    assert product.price == 62.99
    assert "skuJson_0" in caplog.text


def test_brl_formatted_microdata_price_is_accepted() -> None:
    product = _parse(_page(availability="InStock", price="R$ 1.234,56"))[0]

    assert product.price == 1234.56


def test_unreadable_microdata_price_keeps_the_product(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="parsers.copag"):
        product = _parse(_page(availability="InStock", price="sob consulta"))[0]

    assert product.in_stock is True
    assert product.price is None


def test_available_product_without_any_price_is_kept() -> None:
    product = _parse(_page(availability="InStock"))[0]

    assert product.in_stock is True
    assert product.price is None


def test_url_falls_back_to_the_canonical_link() -> None:
    html = _page(availability="InStock", canonical=True).replace(
        f'<meta itemprop="url" content="{TARGET_URL}"/>', ""
    )

    assert _parse(html)[0].url == TARGET_URL


def test_unrecognised_page_raises_instead_of_reporting_sold_out() -> None:
    # A page holds one product, so an unreadable page must never seed or change
    # the watch: the scanner records a parse error instead.
    with pytest.raises(ValueError):
        _parse("<html><body><h1>404</h1></body></html>")


def test_store_not_found_page_raises_instead_of_reporting_sold_out() -> None:
    # The body Copag serves for an unpublished product (/Sistema/404). It is a
    # real HTTP 404, so the fetcher normally stops before the parser; if the
    # store ever serves it with a 200, it must still never seed the watch.
    assert "skuJson_0" not in NOT_FOUND_HTML

    with pytest.raises(ValueError, match="not recognised"):
        _parse(NOT_FOUND_HTML)


def test_page_without_a_name_raises() -> None:
    html = _page(availability="InStock").replace(
        '<meta itemprop="name" content="Produto Microdata"/>', ""
    )

    with pytest.raises(ValueError):
        _parse(html)


def test_page_without_a_product_id_raises() -> None:
    html = _page(availability="InStock").replace(
        '<meta itemprop="productID" content="4242"/>', ""
    )

    with pytest.raises(ValueError):
        _parse(html)
