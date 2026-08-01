"""Card domain model and card identity helper."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

# Configured entry types (FRD §3). "price" is the price watcher (card or sealed
# product); "new_product" is the New-Product (Catalog) Watch (FRD §21).
TYPE_PRICE = "price"
TYPE_NEW_PRODUCT = "new_product"
VALID_TYPES = frozenset({TYPE_PRICE, TYPE_NEW_PRODUCT})


def card_id_for(url: str) -> str:
    """Return the internal card identifier for a listing URL.

    The identifier is the SHA-256 hex digest of the URL (FRD §9). Card names
    are display-only metadata and never participate in identity.

    Args:
        url: The marketplace card URL.

    Returns:
        The 64-character lowercase hex SHA-256 digest of the URL.
    """
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


@dataclass(slots=True, frozen=True)
class Card:
    """A configured entry to monitor: a card, a sealed product, or a catalog watch.

    Attributes:
        name: Display-only name.
        conditions: Conditions to track, as uppercase acronyms (e.g. "NM"),
            or an empty tuple for a sealed product or catalog watch (FRD §3).
        url: The marketplace URL; the source of the entry identity.
        is_sealed: True when this entry tracks one SEALED product price (FRD §5).
        entry_type: The configured ``type`` (FRD §3); ``TYPE_NEW_PRODUCT`` makes
            this a New-Product (Catalog) Watch (FRD §21). The default keeps the
            price watcher the implicit mode for internally-built entries; the
            JSON field itself is mandatory and validated in ``services.config``.
    """

    name: str
    conditions: tuple[str, ...]
    url: str
    is_sealed: bool = False
    entry_type: str = TYPE_PRICE

    @property
    def card_id(self) -> str:
        """The SHA-256 identity derived from :attr:`url` (FRD §9).

        Doubles as the ``watch_id`` of a catalog watch (FRD §21).
        """
        return card_id_for(self.url)

    @property
    def is_catalog(self) -> bool:
        """True when this entry is a New-Product (Catalog) Watch (FRD §21)."""
        return self.entry_type == TYPE_NEW_PRODUCT
