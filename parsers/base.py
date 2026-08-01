"""Marketplace parser contract (FRD §11)."""

from __future__ import annotations

from abc import ABC, abstractmethod

from models.listing import Listing
from models.product import Product


class MarketplaceParser(ABC):
    """Base class for price-watch marketplace parsers."""

    @abstractmethod
    def can_handle(self, url: str) -> bool:
        """Return whether this parser can handle the supplied URL."""

    @abstractmethod
    def parse_listings(self, html: str) -> list[Listing]:
        """Return every listing on the page (all conditions, unfiltered)."""


class CatalogParser(ABC):
    """Base class for catalog (New-Product Watch) parsers (FRD §11, §21).

    A distinct capability from :class:`MarketplaceParser`: it turns a collection
    page into the set of products present on it. The parser knows nothing about
    stored state, new-vs-restock logic, or notifications.
    """

    @abstractmethod
    def can_handle(self, url: str) -> bool:
        """Return whether this parser can handle the supplied collection URL."""

    @abstractmethod
    def parse_catalog(self, html: str) -> list[Product]:
        """Return every product on page 1 of the collection page (FRD §21)."""
