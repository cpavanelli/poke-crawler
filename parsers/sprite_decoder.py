"""Pure LigaPokemon sprite decoder (FRD §10, §4)."""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

_TEMPLATE_CELL_WIDTH = 8
_TEMPLATE_CELL_HEIGHT = 21
_MAX_MEAN_ABSOLUTE_DIFF = 4.0

_STYLE_BLOCK_RE = re.compile(r"\.([A-Za-z0-9_-]+)\{([^}]*)\}")
_POSITION_RE = re.compile(r"background-position:\s*(-?\d+)px\s*(-?\d+)px")
_SPRITE_URL_RE = re.compile(r"background-image:\s*url\(([^)]*imgnum[^)]*)\)")


class SpriteDecodeError(Exception):
    """Raised when a LigaPokemon sprite price cannot be decoded."""


@dataclass(slots=True, frozen=True)
class SpriteStyle:
    """Parsed sprite style information extracted from the inline CSS."""

    position_map: dict[str, tuple[int, int]]
    sprite_url: str


def parse_style_css(style_css: str) -> SpriteStyle:
    """Parse the inline style block that defines the sprite positions."""
    position_map: dict[str, tuple[int, int]] = {}
    sprite_url: str | None = None

    for class_name, body in _STYLE_BLOCK_RE.findall(style_css):
        position_match = _POSITION_RE.search(body)
        if position_match is not None:
            position_map[class_name] = (
                int(position_match.group(1)),
                int(position_match.group(2)),
            )

        if sprite_url is None:
            image_match = _SPRITE_URL_RE.search(body)
            if image_match is not None:
                sprite_url = image_match.group(1).strip().strip("\"'")

    if not position_map:
        raise SpriteDecodeError("Sprite style block does not define digit positions")

    if sprite_url is None:
        raise SpriteDecodeError("Sprite style block does not define a sprite URL")

    if sprite_url.startswith("//"):
        sprite_url = "https:" + sprite_url

    return SpriteStyle(position_map=position_map, sprite_url=sprite_url)


def decode_price(preco_css: str, style_css: str, sprite_bytes: bytes) -> float:
    """Decode one obfuscated LigaPokemon precoCss price (FRD §10).

    Standalone helper that parses the style block and opens the sprite for a
    single price. To decode several precoCss listings from the same page, build
    a :class:`SpriteDecoder` once so the sprite is opened only once.
    """
    style = parse_style_css(style_css)
    return SpriteDecoder(style.position_map, sprite_bytes).decode(preco_css)


class SpriteDecoder:
    """Decode precoCss prices against one page's sprite (FRD §10, §4).

    The sprite is decoded from bytes once (held in memory via ``io.BytesIO``,
    never written to disk — FRD §4) and reused for every precoCss listing on the
    page, so a page with many obfuscated listings decodes the JPEG only once.
    """

    __slots__ = ("_position_map", "_sprite")

    def __init__(self, position_map: dict[str, tuple[int, int]], sprite_bytes: bytes) -> None:
        self._position_map = position_map
        self._sprite = _open_sprite(sprite_bytes)

    def decode(self, preco_css: str) -> float:
        """Decode one precoCss price string. Raises SpriteDecodeError on failure."""
        return _decode(preco_css, self._position_map, self._sprite)


def _decode(
    preco_css: str,
    position_map: dict[str, tuple[int, int]],
    sprite: Image.Image,
) -> float:
    decoded_parts: list[str] = []
    separator_count = 0

    for group in preco_css.split(";"):
        if group == "V":
            separator_count += 1
            decoded_parts.append(",")
            continue

        digit_class = _select_digit_class(group, position_map)
        x, y = position_map[digit_class]
        crop = sprite.crop((-x, -y, -x + _TEMPLATE_CELL_WIDTH, -y + _TEMPLATE_CELL_HEIGHT))
        decoded_parts.append(_recognise_digit(crop))

    if separator_count != 1:
        raise SpriteDecodeError(
            f"Sprite price must contain exactly one decimal separator: {preco_css!r}"
        )

    price_text = "".join(decoded_parts)
    if price_text.count(",") != 1 or not price_text.replace(",", "").isdigit():
        raise SpriteDecodeError(f"Sprite price did not decode cleanly: {preco_css!r}")

    try:
        return float(price_text.replace(",", "."))
    except ValueError as exc:
        raise SpriteDecodeError(f"Sprite price did not parse as float: {price_text!r}") from exc


def _open_sprite(sprite_bytes: bytes) -> Image.Image:
    try:
        with Image.open(io.BytesIO(sprite_bytes)) as sprite:
            return sprite.convert("L")
    except (OSError, ValueError) as exc:
        raise SpriteDecodeError("Sprite bytes could not be decoded as an image") from exc


def _select_digit_class(group: str, position_map: dict[str, tuple[int, int]]) -> str:
    candidates = [class_name for class_name in group.split() if class_name in position_map]
    if len(candidates) != 1:
        raise SpriteDecodeError(f"Sprite price group must resolve to one digit class: {group!r}")
    return candidates[0]


def _recognise_digit(crop: Image.Image) -> str:
    best_digit: str | None = None
    best_score: float | None = None

    for digit, template in _REFERENCE_TEMPLATES:
        score = _mean_absolute_difference(crop, template)
        if best_score is None or score < best_score:
            best_digit = digit
            best_score = score

    if best_digit is None or best_score is None or best_score > _MAX_MEAN_ABSOLUTE_DIFF:
        raise SpriteDecodeError("Sprite digit crop did not match a known template")

    return best_digit


def _mean_absolute_difference(left: Image.Image, right: Image.Image) -> float:
    diff = ImageChops.difference(left, right)
    return ImageStat.Stat(diff).mean[0]


def _load_reference_templates() -> tuple[tuple[str, Image.Image], ...]:
    template_path = Path(__file__).with_name("digit_templates.png")
    with Image.open(template_path) as template_strip:
        strip = template_strip.convert("L")

    templates: list[tuple[str, Image.Image]] = []
    for index, digit in enumerate("0123456789"):
        left = index * _TEMPLATE_CELL_WIDTH
        templates.append(
            (
                digit,
                strip.crop(
                    (left, 0, left + _TEMPLATE_CELL_WIDTH, _TEMPLATE_CELL_HEIGHT)
                ).copy(),
            )
        )
    return tuple(templates)


_REFERENCE_TEMPLATES = _load_reference_templates()

