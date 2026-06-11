"""Marketplace parser contract (FRD §11)."""

from __future__ import annotations

from abc import ABC, abstractmethod

from models.listing import Listing


class MarketplaceParser(ABC):
    """Base class for marketplace parsers."""

    @abstractmethod
    def can_handle(self, url: str) -> bool:
        """Return whether this parser can handle the supplied URL."""

    @abstractmethod
    def parse_listings(self, html: str) -> list[Listing]:
        """Return every listing on the page (all conditions, unfiltered)."""
