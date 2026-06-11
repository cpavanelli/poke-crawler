"""Tests for the LigaPokemon parser (FRD §10-11)."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import httpx
import pytest
from PIL import Image

from models.card import Card
from parsers.ligapokemon_parser import LigaPokemonParser, SpriteDecodeContext


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "ligapokemon"
GENGAR_FIXTURE_PATH = FIXTURE_DIR / "mega_gengar_284.html"
GRENINJA_FIXTURE_PATH = FIXTURE_DIR / "greninja_116_precocss.html"
GRENINJA_SPRITE_PATH = FIXTURE_DIR / "greninja_116_sprite.jpg"


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


def test_parse_fixture_returns_expected_lowest_prices() -> None:
    parser = LigaPokemonParser()
    card = Card(
        name="Mega Gengar",
        conditions=("NM", "SP"),
        url="https://www.ligapokemon.com.br/?view=cards/card&card=Mega+Gengar+ex%20(284/217)&show=1&ed=ASC&num=284",
    )

    results = parser.parse(_load_fixture(GENGAR_FIXTURE_PATH), card)
    assert [(result.condition, result.lowest_price) for result in results] == [
        ("NM", 2670.0),
        ("SP", 2350.0),
    ]


def test_parse_skips_preco_css_only_listing() -> None:
    parser = LigaPokemonParser()
    card = Card(name="Test Card", conditions=("NM",), url="https://www.ligapokemon.com.br/?x=1")
    html = """
        <html>
            <script>
                var cards_stock = [{"qualid":"2","precoCss":"foo"}];
                var cards_stores = {};
                var dataQuality = [{"id":2,"acron":"NM","label":"Praticamente Nova (NM)"}];
            </script>
        </html>
    """

    assert parser.parse(html, card) == []


def test_parse_omits_conditions_without_listings() -> None:
    parser = LigaPokemonParser()
    card = Card(
        name="Mega Gengar",
        conditions=("HP",),
        url="https://www.ligapokemon.com.br/?view=cards/card&card=Mega+Gengar+ex%20(284/217)&show=1&ed=ASC&num=284",
    )

    assert parser.parse(_load_fixture(GENGAR_FIXTURE_PATH), card) == []


def test_parse_fixture_uses_sprite_decode_when_fetcher_is_configured() -> None:
    sprite_bytes = GRENINJA_SPRITE_PATH.read_bytes()
    fetch_calls: list[str] = []

    def sprite_fetcher(url: str) -> bytes:
        fetch_calls.append(url)
        return sprite_bytes

    parser = LigaPokemonParser(sprite_fetcher=sprite_fetcher)
    card = Card(
        name="Mega Greninja",
        conditions=("NM",),
        url="https://www.ligapokemon.com.br/?view=cards/card&card=Mega+Greninja+ex%20(116/086)&show=1&ed=CRI&num=116",
    )

    results = parser.parse(_load_fixture(GRENINJA_FIXTURE_PATH), card)

    assert [(result.condition, result.lowest_price) for result in results] == [("NM", 843.0)]
    assert results[0].lowest_price < 934.15
    assert fetch_calls == [
        "https://repositorio.sbrauble.com/arquivos/up/comp/imgnum/files/img/260422lT92f3zskqjd04i6zfa6z2q78n23hf.jpg"
    ]


def test_parse_opens_sprite_once_for_many_preco_css_listings(monkeypatch) -> None:
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
    card = Card(
        name="Mega Greninja",
        conditions=("NM",),
        url="https://www.ligapokemon.com.br/?view=cards/card&card=Mega+Greninja+ex%20(116/086)&show=1&ed=CRI&num=116",
    )

    results = parser.parse(_load_fixture(GRENINJA_FIXTURE_PATH), card)

    # The fixture has 16 precoCss listings, but the sprite is decoded only once.
    assert [(result.condition, result.lowest_price) for result in results] == [("NM", 843.0)]
    assert open_calls == 1


def test_parse_fixture_without_sprite_fetcher_skips_preco_css() -> None:
    parser = LigaPokemonParser()
    card = Card(
        name="Mega Greninja",
        conditions=("NM",),
        url="https://www.ligapokemon.com.br/?view=cards/card&card=Mega+Greninja+ex%20(116/086)&show=1&ed=CRI&num=116",
    )

    results = parser.parse(_load_fixture(GRENINJA_FIXTURE_PATH), card)

    assert [(result.condition, result.lowest_price) for result in results] == [("NM", 934.15)]


def test_parse_isolates_and_warns_once_for_sprite_decode_failures() -> None:
    blank_sprite_bytes = _load_blank_sprite_bytes()
    sprite_fetch_calls = 0
    error_contexts: list[SpriteDecodeContext] = []

    def sprite_fetcher(url: str) -> bytes:
        nonlocal sprite_fetch_calls
        sprite_fetch_calls += 1
        return blank_sprite_bytes

    def on_sprite_error(context: SpriteDecodeContext) -> None:
        error_contexts.append(context)

    parser = LigaPokemonParser(sprite_fetcher=sprite_fetcher, on_sprite_error=on_sprite_error)
    card = Card(
        name="Broken Sprite Card",
        conditions=("NM",),
        url="https://www.ligapokemon.com.br/?view=cards/card&card=Broken+Sprite+Card&show=1",
    )
    # Two precoCss listings both fail to decode against the blank sprite, but the
    # operator should be warned only once per product.
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

    results = parser.parse(html, card)

    assert [(result.condition, result.lowest_price) for result in results] == [("NM", 934.15)]
    assert sprite_fetch_calls == 1
    assert len(error_contexts) == 1
    assert error_contexts[0] == SpriteDecodeContext(
        card=card,
        url=card.url,
        error_message="Sprite digit crop did not match a known template",
    )


def test_parse_propagates_sprite_fetch_errors() -> None:
    request = httpx.Request("GET", "https://example.com/sprite.jpg")
    response = httpx.Response(429, request=request)

    def sprite_fetcher(url: str) -> bytes:
        raise httpx.HTTPStatusError("Too Many Requests", request=request, response=response)

    parser = LigaPokemonParser(sprite_fetcher=sprite_fetcher)
    card = Card(
        name="Rate Limited Card",
        conditions=("NM",),
        url="https://www.ligapokemon.com.br/?view=cards/card&card=Rate+Limited+Card&show=1",
    )
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
        parser.parse(html, card)
