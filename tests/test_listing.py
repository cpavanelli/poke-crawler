"""Tests for marketplace listing model."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from models.listing import Listing


def test_listing_fields_and_immutability() -> None:
    listing = Listing(condition="NM", price=12.34)

    assert listing.condition == "NM"
    assert listing.price == 12.34

    with pytest.raises(FrozenInstanceError):
        listing.price = 56.78
