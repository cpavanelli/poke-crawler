"""Tests for the Fourse catalog parser (FRD §21)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from parsers.fourse import FourseParser
from parsers.nuvemshop import NuvemshopParser

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "fourse"
BLOCK_HTML = (FIXTURE_DIR / "block_30_years.html").read_text(encoding="utf-8")
CATEGORY_HTML = (FIXTURE_DIR / "category_multi.html").read_text(encoding="utf-8")


def _parse(html: str):
    return FourseParser().parse_catalog(html)


def _block(
    *,
    product_id: str = "1",
    name: str = "Produto",
    href: str = "https://fourse.com.br/item/produto/",
    stock_class: str | None = "instock",
    price: str = "R$ 99,99",
    old_price: str | None = None,
    post_id: bool = True,
    fallback_id: str | None = None,
) -> str:
    classes = ["product", "type-product"]
    if post_id:
        classes.append(f"post-{product_id}")
    if stock_class:
        classes.append(stock_class)
    fallback = "" if fallback_id is None else f'data-product_id="{fallback_id}"'
    title = (
        ""
        if not name
        else (
            '<h2 class="woocommerce-loop-product__title">'
            f'<a href="{href}">{name}</a>'
            "</h2>"
        )
    )
    link = (
        ""
        if not href
        else f'<a class="woocommerce-loop-product__link" href="{href}"></a>'
    )
    amount = (
        f"<del><span class=\"woocommerce-Price-amount\"><bdi>{old_price}</bdi></span></del>"
        f"<ins><span class=\"woocommerce-Price-amount\"><bdi>{price}</bdi></span></ins>"
        if old_price is not None
        else f'<span class="woocommerce-Price-amount"><bdi>{price}</bdi></span>'
    )
    return f"""
        <li class="{' '.join(classes)}">
          {link}
          {title}
          <span class="price">{amount}<span class="em-price-unit">/ UN</span></span>
          <div class="fswp_installments_price"><p class="price">
            <span class="woocommerce-Price-amount"><bdi>R$ 25,00</bdi></span>
          </p></div>
          <div class="fswp_in_cash_price"><p class="price">
            <span class="woocommerce-Price-amount"><bdi>R$ 94,99</bdi></span>
          </p></div>
          <button {fallback}></button>
        </li>
    """


def _page(*blocks: str) -> str:
    return (
        '<html><body><div id="ecomus-shop-content"><ul class="products">'
        f"{''.join(blocks)}"
        "</ul></div></body></html>"
    )


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://fourse.com.br/block/celebrating-30-years-of-pokemon/", True),
        ("https://www.fourse.com.br/product-category/pokemon/", True),
        ("https://shop.fourse.com.br/colecao/", True),
        ("https://shisuistore.com.br/pre-venda/", False),
        ("https://ligapokemon.com.br/", False),
        ("https://notfourse.com.br/", False),
        ("not-a-url", False),
    ],
)
def test_can_handle(url: str, expected: bool) -> None:
    assert FourseParser().can_handle(url) is expected


def test_nuvemshop_parser_does_not_claim_fourse_urls() -> None:
    url = "https://fourse.com.br/block/celebrating-30-years-of-pokemon/"
    assert NuvemshopParser().can_handle(url) is False


def test_block_fixture_yields_only_the_grid_product() -> None:
    products = _parse(BLOCK_HTML)

    # The document holds 6 li.product blocks and 6 distinct /item/ product links;
    # only the single block below #ecomus-shop-content belongs to this page.
    assert len(products) == 1
    product = products[0]
    assert product.product_id == "17960"
    assert product.name == (
        "(PT-BR) Pokémon – Box Coleção Ilustração – Parceiro Inicial – Série 2"
    )
    assert product.url == (
        "https://fourse.com.br/item/"
        "pt-br-pokemon-box-colecao-ilustracao-parceiro-inicial-serie-2/"
    )
    assert product.price == 99.99
    assert product.in_stock is True


def test_phantom_search_modal_listings_are_never_parsed() -> None:
    assert "search-products-suggest-list" in BLOCK_HTML
    assert BLOCK_HTML.count('<li class="product ') == 6

    assert len(_parse(BLOCK_HTML)) == 1


def test_category_fixture_returns_the_grid_in_page_order() -> None:
    products = _parse(CATEGORY_HTML)

    assert [product.product_id for product in products] == [
        "22857",
        "19111",
        "19101",
        "19014",
        "17932",
        "17623",
        "15899",
        "15891",
        "13299",
        "13296",
        "10751",
        "13289",
        "13276",
        "13266",
        "13309",
        "13311",
    ]
    assert len(products) < CATEGORY_HTML.count('<li class="product ')


def test_sale_price_uses_the_ins_price() -> None:
    products = {product.product_id: product for product in _parse(CATEGORY_HTML)}

    assert products["13296"].price == 349.99
    assert products["13296"].price != 399.99


def test_installment_pix_and_unit_suffix_are_not_parsed_as_the_price() -> None:
    product = _parse(_page(_block(price="R$ 299,99")))[0]

    assert product.price == 299.99
    assert product.price not in {25.0, 94.99}


def test_outofstock_is_not_misread_as_instock() -> None:
    product = _parse(_page(_block(stock_class="outofstock")))[0]

    assert product.in_stock is False


def test_product_without_stock_token_is_treated_as_sold_out(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="parsers.fourse"):
        product = _parse(_page(_block(stock_class=None)))[0]

    assert product.in_stock is False
    assert "has no stock signal; treated as sold out" in caplog.text


def test_unparseable_price_keeps_product_with_none_price(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="parsers.fourse"):
        product = _parse(_page(_block(price="indisponível")))[0]

    assert product.price is None
    assert "is not readable BRL; treated as unknown" in caplog.text


def test_missing_name_or_link_is_skipped_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    html = _page(_block(product_id="1", name=""), _block(product_id="2", href=""))

    with caplog.at_level(logging.WARNING, logger="parsers.fourse"):
        assert _parse(html) == []

    assert "product 1 has no name; skipped" in caplog.text
    assert "product 2 has no product link; skipped" in caplog.text


def test_missing_post_id_falls_back_to_data_product_id() -> None:
    html = _page(_block(post_id=False, fallback_id="42"))

    assert _parse(html)[0].product_id == "42"


def test_product_without_post_or_data_product_id_is_skipped_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A bare data-id is not a product id: the live page carries data-id="215"
    # on the Mailchimp newsletter form.
    block = _block(post_id=False, name="Caixa sem id").replace(
        "</li>", '<form data-id="215"></form></li>'
    )

    with caplog.at_level(logging.WARNING, logger="parsers.fourse"):
        assert _parse(_page(block)) == []

    # Selection is scoped to the grid, so this drops a real product and must
    # never be silent.
    assert "product 'Caixa sem id' has no id; skipped" in caplog.text


def test_protocol_relative_and_relative_links_are_absolutised() -> None:
    protocol_relative = _block(product_id="1", href="//fourse.com.br/item/um/")
    relative = _block(product_id="2", href="/item/dois/")

    assert [product.url for product in _parse(_page(protocol_relative, relative))] == [
        "https://fourse.com.br/item/um/",
        "https://fourse.com.br/item/dois/",
    ]


def test_page_without_ecomus_shop_content_returns_empty_list() -> None:
    assert _parse('<html><body><ul class="products">' + _block() + "</ul></body></html>") == []
