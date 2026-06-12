"""Digit-bank decoder gate (issue #12).

The LigaPokemon JPEG sprite renders each digit as one of a small set of
pixel-stable bitmaps; the bank holds every observed bitmap so nearest-match
recognition is robust across page loads. These tests are the safety gate:
**every decoded digit must be correct (zero misreads)** on held-out live
captures, and coverage of the obfuscated listings must be complete.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from parsers.ligapokemon_parser import LigaPokemonParser
from parsers.sprite_decoder import (
    _REFERENCE_TEMPLATES,
    _TEMPLATE_CELL_HEIGHT,
    _TEMPLATE_CELL_WIDTH,
    _slice_bank,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "ligapokemon"

# Ground truth read from the live page (all listings below R$1.000). One of these
# (934.15) is a plaintext precoFinal price; the other 18 are obfuscated precoCss.
OBFUSCATED_PRICES = sorted(
    [
        829.98, 843.00, 846.50, 849.89, 849.90, 850.00, 871.39, 879.75, 899.00,
        899.75, 899.90, 949.99, 950.29, 982.79, 999.00, 999.90, 999.90, 999.99,
    ]
)
PLAINTEXT_BELOW_1000 = [934.15]
ALL_BELOW_1000 = sorted(OBFUSCATED_PRICES + PLAINTEXT_BELOW_1000)

# Held-out live renders captured independently of the committed bank.
HELDOUT_STEMS = ["greninja_116_heldout_00", "greninja_116_heldout_01"]

# #4 anchor fixture (predates the bank): its 16 obfuscated listings.
GRENINJA_KNOWN = sorted(
    [
        843.00, 846.50, 849.89, 849.90, 850.00, 871.39, 879.75, 899.00, 899.75,
        949.99, 950.29, 982.79, 999.00, 999.90, 999.90, 999.99,
    ]
)


def _prices_below_1000(stem: str) -> list[float]:
    html = (FIXTURE_DIR / f"{stem}.html").read_text(encoding="utf-8")
    sprite = (FIXTURE_DIR / f"{stem}_sprite.jpg").read_bytes()
    parser = LigaPokemonParser(sprite_fetcher=lambda _url: sprite)
    listings = parser.parse_listings(html)
    return sorted(round(listing.price, 2) for listing in listings if listing.price < 1000)


@pytest.mark.parametrize("stem", HELDOUT_STEMS)
def test_heldout_capture_decodes_all_obfuscated_prices_without_misread(stem: str) -> None:
    # Zero-misread + full-coverage gate: every sub-R$1.000 price on a held-out
    # live render must decode to exactly the known set. A wrong digit would
    # produce a price not in the set; a skipped digit would drop a listing.
    assert _prices_below_1000(stem) == ALL_BELOW_1000


def test_anchor_fixture_fully_decodes_with_bank() -> None:
    # The #4 fixture was not used to build the bank and predates it, so this is a
    # temporal-stability + held-out check: all 16 obfuscated listings decode.
    sprite = (FIXTURE_DIR / "greninja_116_sprite.jpg").read_bytes()
    parser = LigaPokemonParser(sprite_fetcher=lambda _url: sprite)
    html = (FIXTURE_DIR / "greninja_116_precocss.html").read_text(encoding="utf-8")
    listings = parser.parse_listings(html)
    # The fixture also carries one plaintext precoFinal below R$1.000 (934.15).
    expected = sorted(GRENINJA_KNOWN + [934.15])
    assert sorted(round(l.price, 2) for l in listings if l.price < 1000) == expected


def test_bank_has_every_digit_with_multiple_bitmaps() -> None:
    by_digit: dict[str, int] = {}
    for digit, template in _REFERENCE_TEMPLATES:
        by_digit[digit] = by_digit.get(digit, 0) + 1
        assert template.size == (_TEMPLATE_CELL_WIDTH, _TEMPLATE_CELL_HEIGHT)
    assert sorted(by_digit) == list("0123456789")
    # The sprite renders each digit as more than one bitmap; the bank must hold
    # them all (a single template per digit is what issue #12 fixes).
    assert all(count >= 2 for count in by_digit.values())


def test_slice_bank_skips_blank_trailing_slots() -> None:
    # A 3-wide atlas where each digit has only one real bitmap in column 0 and
    # blank (white) columns 1-2 must yield exactly 10 templates.
    width = 3 * _TEMPLATE_CELL_WIDTH
    height = 10 * _TEMPLATE_CELL_HEIGHT
    atlas = Image.new("L", (width, height), 255)
    for row in range(10):
        cell = Image.new("L", (_TEMPLATE_CELL_WIDTH, _TEMPLATE_CELL_HEIGHT), row * 20)
        atlas.paste(cell, (0, row * _TEMPLATE_CELL_HEIGHT))
    templates = _slice_bank(atlas)
    assert len(templates) == 10
    assert [digit for digit, _ in templates] == list("0123456789")
