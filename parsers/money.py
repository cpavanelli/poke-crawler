"""Shared currency parsing for marketplace parsers.

Brazilian stores render prices as text such as ``R$ 1.234,56``: ``.`` groups
thousands and ``,`` separates decimals. Parsed prices are display-only for the
catalog watch, and shipping is never included (FRD §5).
"""

from __future__ import annotations


def parse_brl(raw: str) -> float | None:
    """Parse Brazilian currency text such as ``R$ 1.234,56``."""
    normalized = "".join(raw.replace("R$", "").replace("\xa0", " ").split())
    normalized = normalized.replace(".", "").replace(",", ".")
    try:
        return float(normalized)
    except ValueError:
        return None
