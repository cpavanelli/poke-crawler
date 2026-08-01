"""Marketplace-agnostic catalog change reduction (FRD §11, §21)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from models.product import Product

CHANGE_NEW = "new"
CHANGE_RESTOCK = "restock"


@dataclass(slots=True, frozen=True)
class Change:
    """One actionable catalog change for a watch (FRD §21).

    Attributes:
        kind: CHANGE_NEW for a product never seen before, CHANGE_RESTOCK for a
            known product that went sold-out -> in-stock.
        product: The product as parsed on this scan.
    """

    kind: str
    product: Product


def detect_changes(
    current: Iterable[Product], known: Mapping[str, bool]
) -> list[Change]:
    """Return the new and restocked products for one watch (FRD §21).

    Args:
        current: Products parsed from the collection page, in page order.
        known: Maps a stored ``product_id`` to the ``in_stock`` state recorded
            for it. Deliberately a plain mapping so this reduction stays
            independent of storage and of any marketplace.

    Returns:
        One Change per new or restocked product, in the order of ``current``.
        A product_id absent from ``known`` is new even when it is sold out; a
        known sold-out product now in stock is a restock. In-stock -> sold-out,
        price moves, and products that disappeared are never reported.
    """
    changes: list[Change] = []

    for product in current:
        if product.product_id not in known:
            changes.append(Change(kind=CHANGE_NEW, product=product))
        elif product.in_stock and not known[product.product_id]:
            changes.append(Change(kind=CHANGE_RESTOCK, product=product))

    return changes
