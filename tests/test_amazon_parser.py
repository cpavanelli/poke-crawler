"""Tests for the Amazon Brasil availability parser (FRD §21, issue #18)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from parsers.amazon import AmazonParser
from parsers.fourse import FourseParser
from parsers.nuvemshop import NuvemshopParser

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "amazon"
OOS_APP_UA_HTML = (FIXTURE_DIR / "oos_app_ua.html").read_text(encoding="utf-8")
OOS_BROWSER_UA_HTML = (FIXTURE_DIR / "oos_browser_ua.html").read_text(encoding="utf-8")
IN_STOCK_HTML = (FIXTURE_DIR / "in_stock.html").read_text(encoding="utf-8")
OFFERS_ONLY_HTML = (FIXTURE_DIR / "offers_only.html").read_text(encoding="utf-8")

TARGET_URL = "https://www.amazon.com.br/dp/B0H78BB9TY"
WISHLIST_URL = (
    "https://www.amazon.com.br/dp/B0H78BB9TY/?coliid=I3Q7XNBNIOVQFK"
    "&colid=1EFQV8H980ZM9&psc=0&ref_=list_c_wl_lv_ov_lig_dp_it"
)

OUT_OF_STOCK_BOX = '<div id="outOfStock">Não disponível.</div>'
ADD_TO_CART = '<input id="add-to-cart-button" type="submit"/>'


def _parse(html: str):
    return AmazonParser().parse_catalog(html)


def _page(
    *,
    asin: str | None = "B0TEST0001",
    canonical_asin: str | None = None,
    title: str | None = "Caixa Pokémon",
    buybox: str | None = OUT_OF_STOCK_BOX,
    outside: str = "",
    captcha: bool = False,
) -> str:
    """Minimal product page with a decoy carousel price outside the buy box."""
    canonical = (
        ""
        if canonical_asin is None
        else f'<link rel="canonical" href="https://www.amazon.com.br/Caixa/dp/{canonical_asin}"/>'
    )
    form = (
        '<form action="/errors/validateCaptcha"><input name="field-keywords"/></form>'
        if captcha
        else ""
    )
    title_html = "" if title is None else f'<span id="productTitle">  {title}  </span>'
    asin_html = "" if asin is None else f'<input type="hidden" id="ASIN" value="{asin}"/>'
    buybox_html = "" if buybox is None else f'<div id="desktop_buybox">{buybox}</div>'
    return f"""
        <html><head>{canonical}</head><body>{form}
          <div id="centerCol">{title_html}</div>
          <div id="rightCol">{asin_html}{buybox_html}</div>
          {outside}
          <div id="CardInstanceDecoy">
            <span class="a-price"><span class="a-offscreen">R$449,90</span></span>
          </div>
        </body></html>
    """


def _core_price(*texts: str) -> str:
    spans = "".join(f'<span class="a-offscreen">{text}</span>' for text in texts)
    return f'<div id="corePrice_feature_div">{spans}</div>'


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (TARGET_URL, True),
        (WISHLIST_URL, True),
        ("https://amazon.com.br/gp/product/B0H78BB9TY", True),
        (
            "https://www.amazon.com.br/Pok%C3%A9mon-TCG-Elite-Trainer-Ingl%C3%AAs/dp/B0H78BB9TY",
            True,
        ),
        ("https://www.amazon.com/dp/B0H78BB9TY", False),
        ("https://notamazon.com.br/dp/B0H78BB9TY", False),
        ("https://www.amazon.com.br/s?k=pokemon", False),
        ("https://www.amazon.com.br/hz/wishlist/ls/1EFQV8H980ZM9", False),
        ("https://fourse.com.br/item/produto/", False),
        ("https://www.shisuistore.com.br/pre-venda/", False),
        ("not-a-url", False),
    ],
)
def test_can_handle(url: str, expected: bool) -> None:
    assert AmazonParser().can_handle(url) is expected


def test_other_catalog_parsers_do_not_claim_amazon_urls() -> None:
    assert FourseParser().can_handle(TARGET_URL) is False
    assert NuvemshopParser().can_handle(TARGET_URL) is False


@pytest.mark.parametrize("html", [OOS_APP_UA_HTML, OOS_BROWSER_UA_HTML])
def test_sold_out_target_fixtures_parse_identically_for_both_user_agents(html: str) -> None:
    products = _parse(html)

    assert len(products) == 1
    product = products[0]
    assert product.product_id == "B0H78BB9TY"
    assert product.name == "Pokémon TCG: 30th Elite Trainer Box - Inglês"
    assert product.url == TARGET_URL
    assert product.price is None
    assert product.in_stock is False


def test_browser_ua_fixture_has_no_availability_text() -> None:
    # Trap 1: the sold-out markup changes with the User-Agent. If a recapture
    # loses these markers, this fixture no longer guards that trap.
    browser = BeautifulSoup(OOS_BROWSER_UA_HTML, "html.parser")
    app = BeautifulSoup(OOS_APP_UA_HTML, "html.parser")

    assert browser.select_one("#availability") is None
    assert browser.select_one("#FALLBACK_OFFER_DISPLAY_desktop #outOfStock") is not None
    assert app.select_one("#availability") is not None
    assert app.select_one("#outOfStockBuyBox_feature_div #outOfStock") is not None


def test_carousel_prices_are_never_the_product_price() -> None:
    # Trap 2: the sold-out page still carries unrelated carousel prices.
    soup = BeautifulSoup(OOS_APP_UA_HTML, "html.parser")
    assert len(soup.select(".a-offscreen")) > 0

    assert _parse(OOS_APP_UA_HTML)[0].price is None


def test_in_stock_fixture_uses_the_buy_box_price() -> None:
    product = _parse(IN_STOCK_HTML)[0]

    assert product.product_id == "B0H7XF3LJL"
    assert product.url == "https://www.amazon.com.br/dp/B0H7XF3LJL"
    assert product.in_stock is True
    assert product.price == 147.49
    assert product.price != 138.90  # the lower "a partir de" other-sellers price


def test_offers_only_fixture_is_available_without_a_price() -> None:
    product = _parse(OFFERS_ONLY_HTML)[0]

    assert product.product_id == "B0C8Y8MJ36"
    assert product.in_stock is True
    assert product.price is None


def test_see_all_buying_choices_wins_over_out_of_stock() -> None:
    buybox = OUT_OF_STOCK_BOX + '<a id="buybox-see-all-buying-choices">Ver todas</a>'

    assert _parse(_page(buybox=buybox))[0].in_stock is True


def test_other_sellers_link_makes_item_available_with_its_price() -> None:
    link = (
        '<div id="olpLinkWidget_feature_div"><a id="aod-ingress-link">'
        '<span class="a-offscreen">R$ 1.234,56</span></a></div>'
    )

    product = _parse(_page(outside=link))[0]

    assert product.in_stock is True
    assert product.price == 1234.56


def test_add_to_cart_outside_the_buy_box_is_ignored() -> None:
    product = _parse(_page(outside=ADD_TO_CART))[0]

    assert product.in_stock is False
    assert product.price is None


def test_add_to_cart_in_buy_box_is_available_with_core_price() -> None:
    product = _parse(_page(buybox=_core_price("R$199,90") + ADD_TO_CART))[0]

    assert product.in_stock is True
    assert product.price == 199.90
    assert product.url == "https://www.amazon.com.br/dp/B0TEST0001"


def test_empty_core_price_node_is_skipped() -> None:
    product = _parse(_page(buybox=_core_price("", "R$ 59,90") + ADD_TO_CART))[0]

    assert product.price == 59.90


def test_unparseable_price_keeps_product_with_none_price(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="parsers.amazon"):
        products = _parse(_page(buybox=_core_price("indisponível") + ADD_TO_CART))

    assert len(products) == 1
    assert products[0].price is None
    assert "is not readable BRL; treated as unknown" in caplog.text


def test_title_whitespace_is_collapsed() -> None:
    product = _parse(_page(title="Caixa\n   Pokémon  <b>ETB</b>"))[0]

    assert product.name == "Caixa Pokémon ETB"


def test_asin_falls_back_to_canonical_link() -> None:
    product = _parse(_page(asin=None, canonical_asin="B0CANON001"))[0]

    assert product.product_id == "B0CANON001"
    assert product.url == "https://www.amazon.com.br/dp/B0CANON001"


def test_invalid_asin_input_falls_back_to_canonical_link() -> None:
    product = _parse(_page(asin="not-an-asin", canonical_asin="B0CANON001"))[0]

    assert product.product_id == "B0CANON001"


def test_missing_asin_raises() -> None:
    with pytest.raises(ValueError, match="has no ASIN"):
        _parse(_page(asin=None))


@pytest.mark.parametrize("title", [None, "   "])
def test_missing_or_empty_title_raises(title: str | None) -> None:
    with pytest.raises(ValueError, match="has no title"):
        _parse(_page(title=title))


def test_captcha_form_raises_robot_check() -> None:
    with pytest.raises(ValueError, match="robot check"):
        _parse(_page(captcha=True))


def test_page_without_title_and_asin_raises_robot_check() -> None:
    with pytest.raises(ValueError, match="robot check"):
        _parse("<html><body><p>Digite os caracteres</p></body></html>")


def test_buy_box_without_any_signal_raises() -> None:
    with pytest.raises(ValueError, match="buy box state not recognised"):
        _parse(_page(buybox='<div id="somethingNew">?</div>'))


def test_missing_buy_box_raises() -> None:
    with pytest.raises(ValueError, match="buy box state not recognised"):
        _parse(_page(buybox=None))
