"""Marketplace-agnostic price reduction helpers."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from models.listing import Listing
from models.price_result import PriceResult


def lowest_prices(
    listings: Iterable[Listing], conditions: Sequence[str]
) -> list[PriceResult]:
    """Lowest listing price per requested condition (FRD §5, §11).

    Keeps only listings whose condition is in `conditions`, takes the minimum
    price per condition, and returns one PriceResult per condition that had at
    least one listing, ordered to follow `conditions`.
    """
    requested_conditions = set(conditions)
    lowest_by_condition: dict[str, float] = {}

    for listing in listings:
        if listing.condition not in requested_conditions:
            continue

        current = lowest_by_condition.get(listing.condition)
        if current is None or listing.price < current:
            lowest_by_condition[listing.condition] = listing.price

    ordered_conditions = dict.fromkeys(conditions)
    return [
        PriceResult(condition=condition, lowest_price=lowest_by_condition[condition])
        for condition in ordered_conditions
        if condition in lowest_by_condition
    ]
