"""Catalog product model: one product on a watched collection page (FRD §21)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class Product:
    """One product listed on a collection page (FRD §21).

    Attributes:
        product_id: The store's stable product identifier; the only identity
            (FRD §21). Name, url, and price are display-only metadata.
        name: Display-only product name.
        url: Display-only product page URL.
        price: Listing price for display in the alert, or None when the page
            does not expose one. Never feeds a baseline, and shipping is never
            included (FRD §5).
        in_stock: Whether the product is currently purchasable.
    """

    product_id: str
    name: str
    url: str
    price: float | None
    in_stock: bool
