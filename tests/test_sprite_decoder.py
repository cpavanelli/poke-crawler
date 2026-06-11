"""Tests for the LigaPokemon sprite decoder (FRD §10, §4)."""

from __future__ import annotations

import json
import re
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

import parsers.sprite_decoder as sprite_decoder


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "ligapokemon"
FIXTURE_HTML_PATH = FIXTURE_DIR / "greninja_116_precocss.html"
FIXTURE_SPRITE_PATH = FIXTURE_DIR / "greninja_116_sprite.jpg"


def _load_fixture_html() -> str:
    return FIXTURE_HTML_PATH.read_text(encoding="utf-8")


def _load_style_css() -> str:
    html = _load_fixture_html()
    return "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", html, re.S | re.I))


def _load_preco_css_items() -> list[str]:
    html = _load_fixture_html()
    stock = json.loads(re.search(r"var\s+cards_stock\s*=\s*(\[.*?\]);", html, re.S).group(1))
    return [str(item["precoCss"]) for item in stock if "precoCss" in item]


def _load_sprite_bytes() -> bytes:
    return FIXTURE_SPRITE_PATH.read_bytes()


def test_parse_style_css_extracts_position_map_and_sprite_url() -> None:
    style = sprite_decoder.parse_style_css(_load_style_css())

    assert "/imgnum/" in style.sprite_url
    assert style.sprite_url.startswith("https://")
    assert len(style.position_map) >= 10
    assert all(len(value) == 2 for value in style.position_map.values())


def test_reference_templates_recognise_themselves() -> None:
    for digit, template in sprite_decoder._REFERENCE_TEMPLATES:
        assert sprite_decoder._recognise_digit(template) == digit


@pytest.mark.parametrize(
    ("index", "expected_price"),
    [
        (0, 843.0),
        (5, 871.39),
        (15, 999.99),
    ],
)
def test_decode_price_recovers_verified_values(index: int, expected_price: float) -> None:
    preco_css = _load_preco_css_items()[index]
    decoded = sprite_decoder.decode_price(preco_css, _load_style_css(), _load_sprite_bytes())

    assert decoded == expected_price


def test_decode_price_rejects_missing_or_duplicate_decimal_separator() -> None:
    style_css = _load_style_css()
    sprite_bytes = _load_sprite_bytes()
    preco_css = _load_preco_css_items()[0]

    with pytest.raises(sprite_decoder.SpriteDecodeError):
        sprite_decoder.decode_price(preco_css.replace(";V;", ";"), style_css, sprite_bytes)

    with pytest.raises(sprite_decoder.SpriteDecodeError):
        sprite_decoder.decode_price(preco_css.replace(";V;", ";V;V;"), style_css, sprite_bytes)


def test_decode_price_rejects_unrecognisable_crop() -> None:
    style_css = _load_style_css()
    preco_css = _load_preco_css_items()[0]
    image_buffer = BytesIO()
    Image.new("L", (600, 84), 255).save(image_buffer, format="JPEG")

    with pytest.raises(sprite_decoder.SpriteDecodeError):
        sprite_decoder.decode_price(preco_css, style_css, image_buffer.getvalue())


def test_decode_price_rejects_group_without_digit_class() -> None:
    style_css = """
        .digit{background-position:0px 0px;}
        .sprite{background-image:url(//example.com/imgnum/test.jpg)}
    """

    with pytest.raises(sprite_decoder.SpriteDecodeError):
        sprite_decoder.decode_price("missing;V;missing", style_css, _load_sprite_bytes())
