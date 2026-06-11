"""Marketplace listing model: one condition-specific listing price."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class Listing:
    """One marketplace listing: a condition and its listing price (FRD §5).

    Distinct from PriceResult, which is the reduced lowest price per condition.
    Price is the listing price only; shipping is never included (FRD §5).

    Attributes:
        condition: Condition acronym (e.g. "NM").
        price: Listing price for that condition.
    """

    condition: str
    price: float
