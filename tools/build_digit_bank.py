"""Build parsers/digit_bank.png from labelled LigaPokemon captures (issue #12).

The JPEG sprite renders each digit as one of a few pixel-stable bitmaps. A single
template misses the others, so this tool collects *every* distinct bitmap per
digit across several captures and writes them as an atlas (row per digit, column
per bitmap) that the decoder matches by nearest neighbour.

Labelling needs no per-listing prices: every obfuscated listing on a page shares
one digit->glyph map, and each listing keeps a stable ``lj_id`` across page loads.
So given the *set* of true obfuscated prices (read once from the rendered page),
the tool bootstraps confident digits from the current bank, unions them per
``lj_id`` across captures, and matches each listing to a unique price in the set.

Usage:
    python tools/build_digit_bank.py \
        --captures path/to/captures_dir \
        --prices path/to/prices.txt \
        --output parsers/digit_bank.png

`captures_dir` holds `<stem>.html` + `<stem>_sprite.jpg` pairs (see
`tools/capture_page.py`); `prices.txt` lists the obfuscated prices, one per line
(e.g. `843,00`). Captures must be of the same card/page.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

from parsers.sprite_decoder import (
    _REFERENCE_TEMPLATES,
    _TEMPLATE_CELL_HEIGHT,
    _TEMPLATE_CELL_WIDTH,
    _mean_absolute_difference,
    parse_style_css,
)

_CONFIDENT_DIST = 2.5  # bootstrap label only on near-exact template matches
_DEDUP_DIST = 1.0  # treat crops within this distance as the same bitmap


def _parse_cards_stock(html: str) -> list[dict]:
    match = re.search(r"\bvar\s+cards_stock\s*=\s*", html)
    if match is None:
        raise SystemExit("cards_stock not found in capture")
    start = match.end()
    depth = 0
    in_string = False
    escaped = False
    quote = ""
    index = start
    while index < len(html):
        char = html[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                in_string = False
        elif char in "\"'":
            in_string = True
            quote = char
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                break
        index += 1
    return json.loads(html[start : index + 1])


def _load_capture(html_path: Path) -> tuple[list[dict], dict, Image.Image]:
    html = html_path.read_text(encoding="utf-8")
    sprite_path = html_path.with_name(html_path.stem + "_sprite.jpg")
    sprite = Image.open(sprite_path).convert("L")
    sprite.load()
    stock = _parse_cards_stock(html)
    style = parse_style_css("\n".join(re.findall(r"<style[^>]*>(.*?)</style>", html, re.S | re.I)))
    listings = [
        item
        for item in stock
        if isinstance(item, dict) and "precoCss" in item and "precoFinal" not in item
    ]
    return listings, style.position_map, sprite


def _digit_classes(item: dict, position_map: dict) -> tuple[list[str], list[int]]:
    classes: list[str] = []
    separators: list[int] = []
    for index, group in enumerate(str(item["precoCss"]).split(";")):
        if group == "V":
            separators.append(index)
            continue
        candidates = [name for name in group.split() if name in position_map]
        if len(candidates) != 1:
            raise SystemExit(f"group must resolve to one digit class: {group!r}")
        classes.append(candidates[0])
    return classes, separators


def _crop(sprite: Image.Image, position_map: dict, class_name: str) -> Image.Image:
    x, y = position_map[class_name]
    return sprite.crop((-x, -y, -x + _TEMPLATE_CELL_WIDTH, -y + _TEMPLATE_CELL_HEIGHT))


def _bootstrap_digit(crop: Image.Image) -> str | None:
    best = min((_mean_absolute_difference(crop, t), d) for d, t in _REFERENCE_TEMPLATES)
    return best[1] if best[0] <= _CONFIDENT_DIST else None


def build(captures_dir: Path, prices: list[str], output: Path) -> None:
    captures = [_load_capture(p) for p in sorted(captures_dir.glob("*.html"))]
    if not captures:
        raise SystemExit(f"no .html captures found in {captures_dir}")

    # 1) bootstrap confident digits and union per lj_id across captures
    known_digits = [p.replace(",", "") for p in prices]
    width = len(known_digits[0])
    if any(len(d) != width for d in known_digits):
        raise SystemExit("all prices must share the same digit count")

    seen_positions: dict[str, dict[int, set[str]]] = defaultdict(lambda: defaultdict(set))
    lj_ids: set[str] = set()
    for listings, position_map, sprite in captures:
        for item in listings:
            lj = str(item["lj_id"])
            lj_ids.add(lj)
            classes, separators = _digit_classes(item, position_map)
            if len(classes) != width:
                continue
            for position, class_name in enumerate(classes):
                digit = _bootstrap_digit(_crop(sprite, position_map, class_name))
                if digit is not None:
                    seen_positions[lj][position].add(digit)

    # 2) resolve each lj_id to a unique price from the (multiset) known set
    available = Counter(known_digits)
    assigned: dict[str, str] = {}
    unresolved = set(lj_ids)
    changed = True
    while changed:
        changed = False
        for lj in list(unresolved):
            pattern = [
                next(iter(seen_positions[lj][p])) if len(seen_positions[lj].get(p, ())) == 1 else None
                for p in range(width)
            ]
            candidates = [
                d
                for d in set(available.elements())
                if all(pattern[p] is None or pattern[p] == d[p] for p in range(width))
            ]
            if len(candidates) == 1:
                assigned[lj] = candidates[0]
                available[candidates[0]] -= 1
                unresolved.discard(lj)
                changed = True
    if unresolved:
        raise SystemExit(f"could not resolve {len(unresolved)} listings: {sorted(unresolved)}")

    # 3) collect distinct bitmaps per digit across all captures
    bank: dict[str, list[Image.Image]] = defaultdict(list)
    for listings, position_map, sprite in captures:
        for item in listings:
            price = assigned[str(item["lj_id"])]
            classes, _ = _digit_classes(item, position_map)
            for position, class_name in enumerate(classes):
                crop = _crop(sprite, position_map, class_name).copy()
                digit = price[position]
                if all(_mean_absolute_difference(crop, kept) > _DEDUP_DIST for kept in bank[digit]):
                    bank[digit].append(crop)

    counts = {d: len(bank[d]) for d in "0123456789"}
    missing = [d for d in "0123456789" if not bank[d]]
    if missing:
        raise SystemExit(f"no bitmaps collected for digits: {missing}")
    print("distinct bitmaps per digit:", counts)

    # 4) write the atlas: row per digit, column per bitmap, blanks left white
    columns = max(counts.values())
    atlas = Image.new("L", (_TEMPLATE_CELL_WIDTH * columns, _TEMPLATE_CELL_HEIGHT * 10), 255)
    for row, digit in enumerate("0123456789"):
        for column, crop in enumerate(bank[digit]):
            atlas.paste(crop, (column * _TEMPLATE_CELL_WIDTH, row * _TEMPLATE_CELL_HEIGHT))
    output.parent.mkdir(parents=True, exist_ok=True)
    atlas.save(output)
    print(f"saved {output} {atlas.size}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the LigaPokemon digit bank atlas.")
    parser.add_argument("--captures", type=Path, required=True, help="Directory of HTML+sprite pairs.")
    parser.add_argument("--prices", type=Path, required=True, help="Obfuscated prices, one per line.")
    parser.add_argument("--output", type=Path, required=True, help="Output atlas PNG path.")
    args = parser.parse_args(argv)
    prices = [line.strip() for line in args.prices.read_text(encoding="utf-8").splitlines() if line.strip()]
    build(args.captures, prices, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
