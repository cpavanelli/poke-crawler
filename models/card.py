"""Card domain model and card identity helper."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


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
    """A configured card or sealed product to monitor.

    Attributes:
        name: Display-only name.
        conditions: Conditions to track, as uppercase acronyms (e.g. "NM"),
            or an empty tuple for a sealed product (FRD §3).
        url: The marketplace listing URL; the source of the card identity.
        is_sealed: True when this entry tracks one SEALED product price (FRD §5).
    """

    name: str
    conditions: tuple[str, ...]
    url: str
    is_sealed: bool = False

    @property
    def card_id(self) -> str:
        """The SHA-256 identity derived from :attr:`url` (FRD §9)."""
        return card_id_for(self.url)
