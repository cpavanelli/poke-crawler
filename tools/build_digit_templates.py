"""Build parsers/digit_templates.png from a captured LigaPokemon page."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from PIL import Image

_CELL_WIDTH = 8
_CELL_HEIGHT = 21


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the LigaPokemon digit template strip.")
    parser.add_argument("--html", type=Path, required=True, help="Saved LigaPokemon HTML capture.")
    parser.add_argument("--sprite", type=Path, required=True, help="Sprite JPEG from the same capture.")
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Output path for the 80x21 digit template strip.",
    )
    parser.add_argument(
        "--prices",
        nargs="+",
        required=True,
        help="Decoded precoCss prices in the same order as the precoCss listings.",
    )
    args = parser.parse_args()

    html = args.html.read_text(encoding="utf-8")
    stock = json.loads(re.search(r"var\s+cards_stock\s*=\s*(\[.*?\]);", html, re.S).group(1))
    css_items = [item for item in stock if "precoCss" in item]
    if len(css_items) != len(args.prices):
        raise SystemExit(
            f"Expected {len(css_items)} prices, received {len(args.prices)}. "
            "Pass one decoded price per precoCss listing, in page order."
        )

    style_css = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", html, re.S | re.I))
    position_map = _build_position_map(style_css)
    with Image.open(args.sprite) as sprite_image:
        sprite = sprite_image.convert("L")

    digit_to_class = _build_digit_map(css_items, args.prices, position_map)
    strip = Image.new("L", (_CELL_WIDTH * 10, _CELL_HEIGHT), 255)

    for index, digit in enumerate("0123456789"):
        class_name = digit_to_class[digit]
        x, y = position_map[class_name]
        crop = sprite.crop((-x, -y, -x + _CELL_WIDTH, -y + _CELL_HEIGHT))
        strip.paste(crop, (index * _CELL_WIDTH, 0))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    strip.save(args.output)
    return 0


def _build_position_map(style_css: str) -> dict[str, tuple[int, int]]:
    position_map: dict[str, tuple[int, int]] = {}
    for class_name, body in re.findall(r"\.([A-Za-z0-9_-]+)\{([^}]*)\}", style_css):
        match = re.search(r"background-position:\s*(-?\d+)px\s*(-?\d+)px", body)
        if match is not None:
            position_map[class_name] = (int(match.group(1)), int(match.group(2)))
    if not position_map:
        raise SystemExit("No background-position rules were found in the inline style blocks.")
    return position_map


def _build_digit_map(
    css_items: list[dict[str, object]],
    prices: list[str],
    position_map: dict[str, tuple[int, int]],
) -> dict[str, str]:
    digit_to_class: dict[str, str] = {}
    class_to_digit: dict[str, str] = {}

    for item, price in zip(css_items, prices):
        groups = str(item["precoCss"]).split(";")
        if len(groups) != len(price):
            raise SystemExit(f"Price length mismatch for {price!r}.")

        for char, group in zip(price, groups):
            if char == ",":
                if group != "V":
                    raise SystemExit(f"Expected a V separator for {price!r}, got {group!r}.")
                continue

            candidates = [class_name for class_name in group.split() if class_name in position_map]
            if len(candidates) != 1:
                raise SystemExit(f"Expected one digit class in {group!r}.")

            class_name = candidates[0]
            previous_digit = class_to_digit.get(class_name)
            if previous_digit is not None and previous_digit != char:
                raise SystemExit(f"Class {class_name!r} mapped to both {previous_digit!r} and {char!r}.")

            class_to_digit[class_name] = char
            digit_to_class.setdefault(char, class_name)

    missing_digits = [digit for digit in "0123456789" if digit not in digit_to_class]
    if missing_digits:
        raise SystemExit(f"Missing digit templates for: {', '.join(missing_digits)}")

    return digit_to_class


if __name__ == "__main__":
    raise SystemExit(main())
