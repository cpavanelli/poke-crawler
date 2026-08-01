"""Tests for the Nuvemshop catalog parser (FRD §21)."""

from __future__ import annotations

from pathlib import Path

import pytest

from parsers.nuvemshop import NuvemshopParser

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "shisui"
PRE_VENDA_HTML = (FIXTURE_DIR / "pre_venda.html").read_text(encoding="utf-8")
PRODUTOS_HTML = (FIXTURE_DIR / "produtos_multi.html").read_text(encoding="utf-8")

PRE_VENDA_ID = "357460215"
PRE_VENDA_NAME = "(PRÉ-VENDA) Box Coleção Ilustração Parceiro Inicial Série 3"


def _parse(html: str):
    return NuvemshopParser().parse_catalog(html)


def _block(
    *,
    product_id: str = "1",
    name: str = "Produto",
    href: str = "https://shisuistore.com.br/produtos/produto/",
    price_attr: str = 'data-product-price="1000"',
    availability: str | None = "https://schema.org/InStock",
    variants: str | None = None,
) -> str:
    ld_json = (
        ""
        if availability is None
        else (
            '<script type="application/ld+json">'
            f'{{"@type":"Product","offers":{{"availability":"{availability}"}}}}'
            "</script>"
        )
    )
    variants_attr = "" if variants is None else f"data-variants='{variants}'"
    link = "" if not href else f'<a class="item-link" href="{href}"></a>'
    return f"""
        <div class="js-item-product" data-product-id="{product_id}">
          <div {variants_attr}>
            {link}
            <div class="js-item-name">{name}</div>
            <span class="js-price-display" {price_attr}>R$10,00</span>
            {ld_json}
          </div>
        </div>
    """


def _page(*blocks: str) -> str:
    return (
        '<html><head><link rel="canonical" href="https://www.shisuistore.com.br/x/"/>'
        f"</head><body>{''.join(blocks)}</body></html>"
    )


# --- can_handle -----------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.shisuistore.com.br/pre-venda/", True),
        ("https://shisuistore.com.br/produtos/", True),
        ("https://www.ligapokemon.com.br/?view=cards/card", False),
        ("https://notshisuistore.com.br/pre-venda/", False),
        ("not-a-url", False),
    ],
)
def test_can_handle(url: str, expected: bool) -> None:
    assert NuvemshopParser().can_handle(url) is expected


# --- Real fixtures --------------------------------------------------------


def test_pre_venda_fixture_yields_one_sold_out_product() -> None:
    products = _parse(PRE_VENDA_HTML)

    # Exactly one: the page also carries a quick-shop modal template that
    # matches div.js-item-product but has an empty data-product-id.
    assert len(products) == 1
    product = products[0]
    assert product.product_id == PRE_VENDA_ID
    assert product.name == PRE_VENDA_NAME
    assert product.url == (
        "https://shisuistore.com.br/produtos/"
        "pre-venda-box-colecao-ilustracao-parceiro-inicial-serie-3/"
    )
    assert product.price == 99.99
    assert product.in_stock is False


def test_in_stock_products_parse_despite_the_hidden_esgotado_label() -> None:
    # The "Esgotado" label is rendered for every product and merely hidden with
    # display:none when in stock, so it must never drive the stock decision.
    assert "Esgotado" in PRODUTOS_HTML

    products = _parse(PRODUTOS_HTML)
    by_id = {product.product_id: product for product in products}

    assert [product.product_id for product in products] == [
        "338984154",
        "352754928",
        "350678871",
        PRE_VENDA_ID,
    ]
    assert by_id["338984154"].in_stock is True
    assert by_id["338984154"].price == 4999.99
    assert by_id["350678871"].in_stock is True
    assert by_id[PRE_VENDA_ID].in_stock is False


# --- Extraction rules -----------------------------------------------------


def test_block_without_product_id_is_skipped() -> None:
    html = _page(_block(product_id="7"), '<div class="js-item-product" data-product-id=""></div>')
    assert [product.product_id for product in _parse(html)] == ["7"]


def test_block_without_name_or_link_is_skipped() -> None:
    html = _page(_block(product_id="1", name=""), _block(product_id="2", href=""))
    assert _parse(html) == []


def test_unreadable_price_keeps_the_product_with_none_price() -> None:
    html = _page(_block(price_attr=""), _block(product_id="2", price_attr='data-product-price="x"'))
    products = _parse(html)
    assert [product.price for product in products] == [None, None]
    assert [product.in_stock for product in products] == [True, True]


def test_out_of_stock_availability_is_read_from_ld_json() -> None:
    html = _page(_block(availability="https://schema.org/OutOfStock"))
    assert _parse(html)[0].in_stock is False


def test_variants_are_used_when_ld_json_is_absent() -> None:
    in_stock = _block(
        product_id="1", availability=None, variants='[{"available":false},{"available":true}]'
    )
    sold_out = _block(product_id="2", availability=None, variants='[{"available":false}]')
    assert [product.in_stock for product in _parse(_page(in_stock, sold_out))] == [True, False]


def test_product_without_any_stock_signal_is_treated_as_sold_out() -> None:
    products = _parse(_page(_block(availability=None)))
    assert [(product.product_id, product.in_stock) for product in products] == [("1", False)]


def test_relative_product_link_is_absolutised_against_the_canonical_url() -> None:
    html = _page(_block(href="/produtos/relativo/"))
    assert _parse(html)[0].url == "https://www.shisuistore.com.br/produtos/relativo/"


def test_page_without_products_returns_empty_list() -> None:
    assert _parse("<html><body><p>nada</p></body></html>") == []
