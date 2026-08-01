"""Tests for the catalog change reduction (FRD §21)."""

from __future__ import annotations

from models.product import Product
from services.catalog import CHANGE_NEW, CHANGE_RESTOCK, detect_changes


def _product(product_id: str, *, in_stock: bool = True) -> Product:
    return Product(
        product_id=product_id,
        name=f"Produto {product_id}",
        url=f"https://shisuistore.com.br/produtos/{product_id}/",
        price=99.99,
        in_stock=in_stock,
    )


def test_unseen_product_is_new() -> None:
    changes = detect_changes([_product("1")], {})
    assert [(change.kind, change.product.product_id) for change in changes] == [
        (CHANGE_NEW, "1")
    ]


def test_unseen_sold_out_product_is_still_new() -> None:
    changes = detect_changes([_product("1", in_stock=False)], {"2": True})
    assert [change.kind for change in changes] == [CHANGE_NEW]


def test_known_sold_out_product_back_in_stock_is_a_restock() -> None:
    changes = detect_changes([_product("1", in_stock=True)], {"1": False})
    assert [(change.kind, change.product.product_id) for change in changes] == [
        (CHANGE_RESTOCK, "1")
    ]


def test_known_product_going_out_of_stock_is_not_reported() -> None:
    assert detect_changes([_product("1", in_stock=False)], {"1": True}) == []


def test_unchanged_products_are_not_reported() -> None:
    current = [_product("1", in_stock=True), _product("2", in_stock=False)]
    assert detect_changes(current, {"1": True, "2": False}) == []


def test_known_product_missing_from_the_page_is_not_reported() -> None:
    assert detect_changes([], {"1": True}) == []


def test_first_scan_reports_everything_as_new() -> None:
    current = [_product("1"), _product("2", in_stock=False)]
    assert [change.kind for change in detect_changes(current, {})] == [
        CHANGE_NEW,
        CHANGE_NEW,
    ]


def test_mixed_batch_keeps_page_order() -> None:
    current = [
        _product("1", in_stock=True),   # unchanged
        _product("2", in_stock=True),   # restock
        _product("3", in_stock=False),  # new, sold out
    ]
    known = {"1": True, "2": False}

    changes = detect_changes(current, known)

    assert [(change.kind, change.product.product_id) for change in changes] == [
        (CHANGE_RESTOCK, "2"),
        (CHANGE_NEW, "3"),
    ]
