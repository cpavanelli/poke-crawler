"""LigaPokemon parser for the happy path price listings (FRD §10-11)."""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

from models.card import Card
from models.price_result import PriceResult
from parsers.base import MarketplaceParser


class LigaPokemonParser(MarketplaceParser):
    """Parse LigaPokemon card pages."""

    def can_handle(self, url: str) -> bool:
        """Return True for ligapokemon.com.br URLs, including www and queries."""
        hostname = urlsplit(url).hostname
        return bool(
            hostname
            and (hostname == "ligapokemon.com.br" or hostname.endswith(".ligapokemon.com.br"))
        )

    def parse(self, html: str, card: Card) -> list[PriceResult]:
        """Parse the page HTML and return the lowest listing per configured condition."""
        cards_stock = _extract_js_literal(html, "cards_stock")
        if not isinstance(cards_stock, list):
            raise ValueError("LigaPokemon cards_stock must be a JSON array")
        if not cards_stock:
            return []

        data_quality = _extract_js_literal(html, "dataQuality")
        condition_map = _build_condition_map(data_quality)

        lowest_by_condition: dict[str, float] = {}
        configured_conditions = set(card.conditions)

        for listing in cards_stock:
            if not isinstance(listing, dict):
                continue

            condition = _resolve_condition(listing, condition_map)
            if condition is None or condition not in configured_conditions:
                continue

            price = _parse_preco_final(listing)
            if price is None:
                # TODO(precoCss): sprite-decode path, FRD §10
                continue

            current = lowest_by_condition.get(condition)
            if current is None or price < current:
                lowest_by_condition[condition] = price

        ordered_conditions = dict.fromkeys(card.conditions)
        return [
            PriceResult(condition=condition, lowest_price=lowest_by_condition[condition])
            for condition in ordered_conditions
            if condition in lowest_by_condition
        ]


def _extract_js_literal(html: str, name: str) -> object:
    """Extract and decode a JSON-compatible JavaScript literal from the page."""
    match = re.search(rf"\bvar\s+{re.escape(name)}\s*=\s*", html)
    if match is None:
        raise ValueError(f"LigaPokemon variable not found: {name}")

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


def _parse_preco_final(listing: dict[str, object]) -> float | None:
    """Return the direct listing price, skipping LigaPokemon sprite listings."""
    raw_price = listing.get("precoFinal")
    if raw_price is None:
        return None

    try:
        return float(str(raw_price).strip())
    except (TypeError, ValueError):
        return None
