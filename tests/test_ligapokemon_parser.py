"""Tests for the LigaPokemon parser (FRD §10-11)."""

from __future__ import annotations

from pathlib import Path

from models.card import Card
from parsers.ligapokemon import LigaPokemonParser


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "ligapokemon" / "mega_gengar_284.html"


def _load_fixture() -> str:
    return FIXTURE_PATH.read_text(encoding="utf-8")


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

    results = parser.parse(_load_fixture(), card)
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

    assert parser.parse(_load_fixture(), card) == []
