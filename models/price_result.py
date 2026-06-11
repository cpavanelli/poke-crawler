"""Parser output model: the lowest price found for a single condition."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class PriceResult:
    """The lowest listing price found for one card condition.

    Price is the listing price only; shipping is never included (FRD §5).

    Attributes:
        condition: Condition acronym (e.g. "NM").
        lowest_price: Lowest listing price for that condition.
    """

    condition: str
    lowest_price: float
