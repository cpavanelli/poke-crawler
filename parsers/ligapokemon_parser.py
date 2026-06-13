"""LigaPokemon parser for FRD §10-11, including precoCss sprite decode."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from urllib.parse import urlsplit

from models.listing import Listing
from parsers.base import MarketplaceParser
from parsers.sprite_decoder import (
    SpriteDecodeError,
    SpriteDecoder,
    parse_style_css,
)


SpriteFetcher = Callable[[str], bytes]
SpriteErrorHandler = Callable[[str], None]


class LigaPokemonParser(MarketplaceParser):
    """Parse LigaPokemon card pages."""

    def __init__(
        self,
        *,
        sprite_fetcher: SpriteFetcher | None = None,
        on_sprite_error: SpriteErrorHandler | None = None,
    ) -> None:
        self._sprite_fetcher = sprite_fetcher
        self._on_sprite_error = on_sprite_error

    def can_handle(self, url: str) -> bool:
        """Return True for ligapokemon.com.br URLs, including www and queries."""
        hostname = urlsplit(url).hostname
        return bool(
            hostname
            and (hostname == "ligapokemon.com.br" or hostname.endswith(".ligapokemon.com.br"))
        )

    def parse_listings(self, html: str) -> list[Listing]:
        """Parse the page HTML and return every priced listing."""
        cards_stock = _extract_first_js_literal(html, ("prod_stock", "cards_stock"))
        if not isinstance(cards_stock, list):
            raise ValueError("LigaPokemon cards_stock must be a JSON array")
        if not cards_stock:
            return []

        data_quality = _extract_js_literal(html, "dataQuality")
        condition_map = _build_condition_map(data_quality)
        style_css = _extract_inline_style(html)

        listings: list[Listing] = []
        sprite_decoder: SpriteDecoder | None = None
        sprite_setup_done = False
        sprite_error_reported = False

        def report_sprite_error(message: str) -> None:
            # At most one sprite warning per page: a page-level fault or
            # a site-wide decoder breakage otherwise fans the same error out
            # across every precoCss listing (FRD §10 is per-listing, but the
            # operator only needs one alert per product).
            nonlocal sprite_error_reported
            if sprite_error_reported:
                return
            sprite_error_reported = True
            self._emit_sprite_error(message)

        for listing in cards_stock:
            if not isinstance(listing, dict):
                continue

            condition = _resolve_condition(listing, condition_map)
            if condition is None:
                continue

            price = _parse_preco_final(listing)
            if price is None:
                raw_preco_css = listing.get("precoCss")
                if not isinstance(raw_preco_css, str) or self._sprite_fetcher is None:
                    continue

                if not sprite_setup_done:
                    sprite_setup_done = True
                    # Parse the style block, fetch the sprite, and open it once
                    # per page; a failure here (malformed style or undecodable
                    # sprite) is a page-level fault shared by every precoCss
                    # listing. A fetch HTTP error (403/429) is not a
                    # SpriteDecodeError, so it propagates and the scanner stops
                    # the cycle (FRD §12/§17).
                    try:
                        style = parse_style_css(style_css)
                        sprite_bytes = self._sprite_fetcher(style.sprite_url)
                        sprite_decoder = SpriteDecoder(style.position_map, sprite_bytes)
                    except SpriteDecodeError as exc:
                        report_sprite_error(str(exc))

                if sprite_decoder is None:
                    continue

                try:
                    price = sprite_decoder.decode(raw_preco_css)
                except SpriteDecodeError as exc:
                    report_sprite_error(str(exc))
                    continue

            listings.append(Listing(condition=condition, price=price))

        return listings

    def _emit_sprite_error(self, message: str) -> None:
        """Forward a sprite decode error to the optional callback."""
        if self._on_sprite_error is None:
            return

        self._on_sprite_error(message)


class _VariableNotFound(ValueError):
    """Raised when a named ``var`` literal is absent from the page.

    A subclass of :class:`ValueError` so callers that only care that extraction
    failed keep working, while :func:`_extract_first_js_literal` can distinguish
    "this candidate is absent, try the next" from a genuine parse fault without
    matching on the error message text.
    """


def _extract_js_literal(html: str, name: str) -> object:
    """Extract and decode a JSON-compatible JavaScript literal from the page."""
    match = re.search(rf"\bvar\s+{re.escape(name)}\s*=\s*", html)
    if match is None:
        raise _VariableNotFound(f"LigaPokemon variable not found: {name}")

    start = match.end()
    while start < len(html) and html[start].isspace():
        start += 1

    if start >= len(html):
        raise ValueError(f"LigaPokemon variable is empty: {name}")

    opener = html[start]
    if opener not in "[{":
        raise ValueError(f"LigaPokemon variable does not start with a JSON literal: {name}")

    stack = ["]" if opener == "[" else "}"]
    in_string = False
    quote = ""
    escaped = False

    for index in range(start + 1, len(html)):
        char = html[index]

        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                in_string = False
            continue

        if char in "\"'":
            in_string = True
            quote = char
            continue

        if char == "[":
            stack.append("]")
            continue

        if char == "{":
            stack.append("}")
            continue

        if char in "]}":
            if not stack or char != stack.pop():
                raise ValueError(f"LigaPokemon variable has unbalanced brackets: {name}")
            if not stack:
                return json.loads(html[start : index + 1])

    raise ValueError(f"LigaPokemon variable was not terminated: {name}")


def _extract_first_js_literal(html: str, names: tuple[str, ...]) -> object:
    """Extract the first present JavaScript literal among candidate names.

    Tries each name in order, skipping ones that are simply absent
    (:class:`_VariableNotFound`). Any other failure (malformed literal,
    unbalanced brackets) propagates immediately rather than masking a real fault
    behind the next candidate.
    """
    for name in names:
        try:
            return _extract_js_literal(html, name)
        except _VariableNotFound:
            continue

    raise _VariableNotFound(f"LigaPokemon variable not found: {' or '.join(names)}")


def _build_condition_map(data_quality: object) -> dict[int, str]:
    """Build the LigaPokemon condition-ID to acronym mapping from dataQuality."""
    if not isinstance(data_quality, list):
        raise ValueError("LigaPokemon dataQuality must be a JSON array")

    mapping: dict[int, str] = {}
    for entry in data_quality:
        if not isinstance(entry, dict):
            raise ValueError("LigaPokemon dataQuality entries must be JSON objects")
        try:
            qualid = int(entry["id"])
            acronym = str(entry["acron"]).strip().upper()
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("LigaPokemon dataQuality entry is malformed") from exc
        if not acronym:
            raise ValueError("LigaPokemon dataQuality entry has an empty acronym")
        mapping[qualid] = acronym
    return mapping


def _resolve_condition(listing: dict[str, object], condition_map: dict[int, str]) -> str | None:
    """Return the listing condition acronym, or None when it cannot be resolved."""
    raw_qualid = listing.get("qualid")
    if raw_qualid is None:
        return None

    try:
        qualid = int(raw_qualid)
    except (TypeError, ValueError):
        return None

    return condition_map.get(qualid)


def _extract_inline_style(html: str) -> str:
    """Return all inline style blocks joined into one CSS string."""
    styles = re.findall(r"<style[^>]*>(.*?)</style>", html, re.S | re.I)
    return "\n".join(styles)


def _parse_preco_final(listing: dict[str, object]) -> float | None:
    """Return the direct listing price, skipping LigaPokemon sprite listings."""
    raw_price = listing.get("precoFinal")
    if raw_price is None:
        return None

    try:
        return float(str(raw_price).strip())
    except (TypeError, ValueError):
        return None
