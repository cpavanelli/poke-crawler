"""Tests for the LigaPokemon parser (FRD §10-11)."""

from __future__ import annotations

from collections import Counter
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from PIL import Image

from models.listing import Listing
from parsers.ligapokemon_parser import LigaPokemonParser
from services.pricing import lowest_prices


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "ligapokemon"
GENGAR_FIXTURE_PATH = FIXTURE_DIR / "mega_gengar_284.html"
GRENINJA_FIXTURE_PATH = FIXTURE_DIR / "greninja_116_precocss.html"
GRENINJA_SPRITE_PATH = FIXTURE_DIR / "greninja_116_sprite.jpg"
ETB_FIXTURE_PATH = FIXTURE_DIR / "etb_ascended_heroes_prod.html"


def _load_fixture(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _load_blank_sprite_bytes() -> bytes:
    buffer = BytesIO()
    Image.new("L", (600, 84), 255).save(buffer, format="JPEG")
    return buffer.getvalue()


def test_can_handle_ligapokemon_urls() -> None:
    parser = LigaPokemonParser()

    assert parser.can_handle(
        "https://www.ligapokemon.com.br/?view=cards/card&card=Mega+Gengar+ex%20(284/217)&show=1"
    )
    assert parser.can_handle(
        "https://ligapokemon.com.br/?view=cards/card&card=Mega+Gengar+ex%20(284/217)&show=1"
    )
    assert parser.can_handle(
        "https://www.ligapokemon.com.br/?view=cards/card&card=Mega+Gengar+ex%20(284/217)&show=1&foo=bar"
    )
    assert not parser.can_handle("https://www.mypcards.com/?view=cards/card")
    assert not parser.can_handle("https://example.com/?view=cards/card")


def test_parse_listings_fixture_returns_every_priced_listing_unfiltered() -> None:
    parser = LigaPokemonParser()

    listings = parser.parse_listings(_load_fixture(GENGAR_FIXTURE_PATH))

    assert len(listings) == 25
    assert Counter(listing.condition for listing in listings) == Counter(
        {"M": 1, "NM": 17, "SP": 7}
    )
    assert min(listing.price for listing in listings if listing.condition == "M") == 2687.04
    assert min(listing.price for listing in listings if listing.condition == "NM") == 2670.0
    assert min(listing.price for listing in listings if listing.condition == "SP") == 2350.0


def test_parse_listings_includes_conditions_not_requested_by_old_parser() -> None:
    parser = LigaPokemonParser()

    listings = parser.parse_listings(_load_fixture(GENGAR_FIXTURE_PATH))

    assert any(listing.condition == "M" for listing in listings)


def test_parse_listings_skips_preco_css_only_listing_without_fetcher() -> None:
    parser = LigaPokemonParser()
    html = """
        <html>
            <script>
                var cards_stock = [{"qualid":"2","precoCss":"foo"}];
                var cards_stores = {};
                var dataQuality = [{"id":2,"acron":"NM","label":"Praticamente Nova (NM)"}];
            </script>
        </html>
    """

    assert parser.parse_listings(html) == []


def test_parse_listings_uses_sprite_decode_when_fetcher_is_configured() -> None:
    sprite_bytes = GRENINJA_SPRITE_PATH.read_bytes()
    fetch_calls: list[str] = []

    def sprite_fetcher(url: str) -> bytes:
        fetch_calls.append(url)
        return sprite_bytes

    parser = LigaPokemonParser(sprite_fetcher=sprite_fetcher)

    listings = parser.parse_listings(_load_fixture(GRENINJA_FIXTURE_PATH))

    assert Listing(condition="NM", price=843.0) in listings
    assert fetch_calls == [
        "https://repositorio.sbrauble.com/arquivos/up/comp/imgnum/files/img/260422lT92f3zskqjd04i6zfa6z2q78n23hf.jpg"
    ]


def test_parse_listings_supports_prod_stock_with_sealed_acronyms() -> None:
    sprite_bytes = GRENINJA_SPRITE_PATH.read_bytes()
    parser = LigaPokemonParser(sprite_fetcher=lambda _url: sprite_bytes)

    listings = parser.parse_listings(_load_fixture(ETB_FIXTURE_PATH))

    assert listings == [
        Listing(condition="L", price=843.0),
        Listing(condition="L", price=900.0),
        Listing(condition="D", price=805.0),
    ]


def test_parse_listings_opens_sprite_once_for_many_preco_css_listings(monkeypatch) -> None:
    import parsers.sprite_decoder as sprite_decoder

    open_calls = 0
    real_open_sprite = sprite_decoder._open_sprite

    def counting_open_sprite(sprite_bytes: bytes):
        nonlocal open_calls
        open_calls += 1
        return real_open_sprite(sprite_bytes)

    monkeypatch.setattr(sprite_decoder, "_open_sprite", counting_open_sprite)

    sprite_bytes = GRENINJA_SPRITE_PATH.read_bytes()
    parser = LigaPokemonParser(sprite_fetcher=lambda _url: sprite_bytes)

    listings = parser.parse_listings(_load_fixture(GRENINJA_FIXTURE_PATH))

    assert Listing(condition="NM", price=843.0) in listings
    assert open_calls == 1


def test_parse_listings_without_sprite_fetcher_skips_preco_css() -> None:
    parser = LigaPokemonParser()

    listings = parser.parse_listings(_load_fixture(GRENINJA_FIXTURE_PATH))

    assert min(listing.price for listing in listings if listing.condition == "NM") == 934.15
    assert Listing(condition="NM", price=843.0) not in listings


def test_parse_listings_composes_with_lowest_prices_for_old_behavior() -> None:
    parser = LigaPokemonParser()

    results = lowest_prices(
        parser.parse_listings(_load_fixture(GENGAR_FIXTURE_PATH)), ("NM", "SP")
    )

    assert [(result.condition, result.lowest_price) for result in results] == [
        ("NM", 2670.0),
        ("SP", 2350.0),
    ]


def test_parse_listings_with_sprite_composes_with_lowest_prices() -> None:
    sprite_bytes = GRENINJA_SPRITE_PATH.read_bytes()
    parser = LigaPokemonParser(sprite_fetcher=lambda _url: sprite_bytes)

    results = lowest_prices(
        parser.parse_listings(_load_fixture(GRENINJA_FIXTURE_PATH)), ("NM",)
    )

    assert [(result.condition, result.lowest_price) for result in results] == [("NM", 843.0)]
    assert results[0].lowest_price < 934.15


def test_parse_listings_isolates_and_warns_once_for_sprite_decode_failures() -> None:
    blank_sprite_bytes = _load_blank_sprite_bytes()
    sprite_fetch_calls = 0
    error_messages: list[str] = []

    def sprite_fetcher(url: str) -> bytes:
        nonlocal sprite_fetch_calls
        sprite_fetch_calls += 1
        return blank_sprite_bytes

    parser = LigaPokemonParser(
        sprite_fetcher=sprite_fetcher,
        on_sprite_error=error_messages.append,
    )
    html = """
        <html>
            <script>
                var cards_stock = [
                    {"qualid":"2","precoFinal":"934.15"},
                    {"qualid":"2","precoCss":"digit foo;digit bar;V;digit bar"},
                    {"qualid":"2","precoCss":"digit bar;V;digit foo;digit bar"}
                ];
                var dataQuality = [{"id":2,"acron":"NM","label":"Praticamente Nova (NM)"}];
            </script>
            <style>
                .digit{background-position:0px 0px;}
                .foo{width:7px;float:left;height:15px;}
                .bar{background-image:url(//example.com/imgnum/test.jpg)}
            </style>
        </html>
    """

    listings = parser.parse_listings(html)

    assert listings == [Listing(condition="NM", price=934.15)]
    assert sprite_fetch_calls == 1
    assert error_messages == ["Sprite digit crop did not match a known template"]


@pytest.mark.parametrize("status_code", [403, 429])
def test_parse_listings_propagates_sprite_fetch_errors(status_code: int) -> None:
    request = httpx.Request("GET", "https://example.com/sprite.jpg")
    response = httpx.Response(status_code, request=request)

    def sprite_fetcher(url: str) -> bytes:
        raise httpx.HTTPStatusError("Sprite fetch failed", request=request, response=response)

    parser = LigaPokemonParser(sprite_fetcher=sprite_fetcher)
    html = """
        <html>
            <script>
                var cards_stock = [{"qualid":"2","precoCss":"digit foo;V;digit foo"}];
                var dataQuality = [{"id":2,"acron":"NM","label":"Praticamente Nova (NM)"}];
            </script>
            <style>
                .digit{background-position:0px 0px;}
                .foo{background-image:url(//example.com/imgnum/test.jpg)}
            </style>
        </html>
    """

    with pytest.raises(httpx.HTTPStatusError):
        parser.parse_listings(html)
