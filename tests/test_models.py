"""Tests for the card identity helper and domain models."""

from __future__ import annotations

import hashlib

from models.card import Card, card_id_for


def test_card_id_is_sha256_of_url() -> None:
    url = "https://example.com/card?x=1"
    expected = hashlib.sha256(url.encode("utf-8")).hexdigest()
    assert card_id_for(url) == expected
    assert len(card_id_for(url)) == 64


def test_card_id_depends_only_on_url() -> None:
    url = "https://example.com/same"
    a = Card(name="Alpha", conditions=("NM",), url=url)
    b = Card(name="Totally Different Name", conditions=("SP", "HP"), url=url)
    assert a.card_id == b.card_id == card_id_for(url)


def test_distinct_urls_yield_distinct_ids() -> None:
    a = Card(name="x", conditions=("NM",), url="https://example.com/a")
    b = Card(name="x", conditions=("NM",), url="https://example.com/b")
    assert a.card_id != b.card_id
