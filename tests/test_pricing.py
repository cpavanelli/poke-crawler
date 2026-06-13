"""Tests for marketplace-agnostic price reduction."""

from __future__ import annotations

from models.listing import Listing
from services.pricing import SEALED_LABEL, lowest_prices, lowest_sealed_price


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


def test_lowest_sealed_price_counts_only_factory_sealed_condition() -> None:
    listings = [
        Listing(condition="N", price=50.0),
        Listing(condition="NEA", price=40.0),
        Listing(condition="NSA", price=30.0),
        Listing(condition="A", price=20.0),
        Listing(condition="U", price=10.0),
        Listing(condition="D", price=5.0),
        Listing(condition="L", price=80.0),
    ]

    result = lowest_sealed_price(listings)

    assert result is not None
    assert result.condition == SEALED_LABEL
    assert result.lowest_price == 80.0


def test_lowest_sealed_price_picks_minimum_l_listing() -> None:
    listings = [
        Listing(condition="L", price=90.0),
        Listing(condition="D", price=1.0),
        Listing(condition="L", price=75.0),
    ]

    result = lowest_sealed_price(listings)

    assert result is not None
    assert result.condition == SEALED_LABEL
    assert result.lowest_price == 75.0


def test_lowest_sealed_price_returns_none_without_l_listing() -> None:
    listings = [
        Listing(condition="D", price=5.0),
        Listing(condition="U", price=10.0),
    ]

    assert lowest_sealed_price(listings) is None
