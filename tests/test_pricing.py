"""Tests for marketplace-agnostic price reduction."""

from __future__ import annotations

from models.listing import Listing
from services.pricing import lowest_prices


def test_lowest_prices_picks_minimum_per_condition() -> None:
    listings = [
        Listing(condition="NM", price=10.0),
        Listing(condition="NM", price=8.0),
        Listing(condition="SP", price=7.0),
        Listing(condition="SP", price=9.0),
    ]

    results = lowest_prices(listings, ("NM", "SP"))

    assert [(result.condition, result.lowest_price) for result in results] == [
        ("NM", 8.0),
        ("SP", 7.0),
    ]


def test_lowest_prices_filters_unrequested_conditions() -> None:
    listings = [
        Listing(condition="M", price=5.0),
        Listing(condition="NM", price=10.0),
    ]

    results = lowest_prices(listings, ("NM",))

    assert [(result.condition, result.lowest_price) for result in results] == [("NM", 10.0)]


def test_lowest_prices_output_order_follows_requested_conditions_and_omits_missing() -> None:
    listings = [
        Listing(condition="NM", price=10.0),
        Listing(condition="M", price=5.0),
    ]

    results = lowest_prices(listings, ("SP", "M", "NM"))

    assert [(result.condition, result.lowest_price) for result in results] == [
        ("M", 5.0),
        ("NM", 10.0),
    ]


def test_lowest_prices_empty_input_returns_empty_list() -> None:
    assert lowest_prices([], ("NM", "SP")) == []
