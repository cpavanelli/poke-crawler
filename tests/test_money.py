"""Tests for shared currency parsing (parsers.money)."""

from __future__ import annotations

import pytest

from parsers.money import parse_brl


def test_parse_brl_handles_thousands_separator_and_nbsp() -> None:
    assert parse_brl("R$\xa01.234,56") == 1234.56


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("R$147,49", 147.49),
        ("R$ 99,99", 99.99),
        ("indisponível", None),
        ("", None),
    ],
)
def test_parse_brl(raw: str, expected: float | None) -> None:
    assert parse_brl(raw) == expected
