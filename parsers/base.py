"""Marketplace parser contract (FRD §11)."""

from __future__ import annotations

from abc import ABC, abstractmethod

from models.card import Card
from models.price_result import PriceResult


class MarketplaceParser(ABC):
    """Base class for marketplace parsers."""

    @abstractmethod
    def can_handle(self, url: str) -> bool:
        """Return whether this parser can handle the supplied URL."""

    @abstractmethod
    def parse(self, html: str, card: Card) -> list[PriceResult]:
        """Parse one marketplace page and return the usable price results."""
