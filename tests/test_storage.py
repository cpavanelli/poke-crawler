"""Tests for the SQLite storage layer (FRD §8)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from models.product import Product
from services.storage import (
    BaselineRow,
    connect,
    get_baseline,
    get_known_stock,
    init_db,
    insert_scan_error,
    insert_scan_result,
    is_watch_seeded,
    mark_watch_scanned,
    upsert_baseline,
    upsert_collection_products,
)


def _open_db(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(tmp_path / "watcher.sqlite3")
    init_db(conn)
    return conn


def test_connect_applies_pragmas(tmp_path: Path) -> None:
    conn = connect(tmp_path / "pragmas.sqlite3")
    try:
        assert conn.row_factory is sqlite3.Row
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
    finally:
        conn.close()


def test_init_db_round_trips_each_table(tmp_path: Path) -> None:
    conn = _open_db(tmp_path)
    try:
        now = "2026-06-11T01:02:03-03:00"

        upsert_baseline(
            conn,
            "card-1",
            "Mega Gengar",
            "https://example.com/a",
            "NM",
            2670.0,
            now=now,
        )
        assert get_baseline(conn, "card-1", "NM") == BaselineRow(
            card_id="card-1",
            card_name="Mega Gengar",
            url="https://example.com/a",
            condition="NM",
            lowest_price=2670.0,
            created_at=now,
            updated_at=now,
        )

        insert_scan_result(
            conn,
            "card-1",
            "Mega Gengar",
            "https://example.com/a",
            "NM",
            2670.0,
            scanned_at=now,
        )
        scan_result = conn.execute(
            """
            SELECT id, card_id, card_name, url, condition, lowest_price, scanned_at
            FROM scan_results
            """
        ).fetchone()
        assert scan_result["id"] == 1
        assert scan_result["card_id"] == "card-1"
        assert scan_result["lowest_price"] == 2670.0
        assert scan_result["scanned_at"] == now

        insert_scan_error(
            conn,
            url="https://example.com/a",
            error_type="sprite_decode",
            card_id=None,
            error_message=None,
            occurred_at=now,
        )
        scan_error = conn.execute(
            """
            SELECT id, card_id, url, error_type, error_message, occurred_at
            FROM scan_errors
            """
        ).fetchone()
        assert scan_error["id"] == 1
        assert scan_error["card_id"] is None
        assert scan_error["error_type"] == "sprite_decode"
        assert scan_error["error_message"] is None
        assert scan_error["occurred_at"] == now
    finally:
        conn.close()


def test_init_db_is_idempotent(tmp_path: Path) -> None:
    conn = connect(tmp_path / "idempotent.sqlite3")
    try:
        init_db(conn)
        init_db(conn)
    finally:
        conn.close()


def test_upsert_baseline_preserves_created_at(tmp_path: Path) -> None:
    conn = _open_db(tmp_path)
    try:
        first_now = "2026-06-11T01:02:03-03:00"
        second_now = "2026-06-11T04:05:06-03:00"

        upsert_baseline(
            conn,
            "card-1",
            "Mega Gengar",
            "https://example.com/a",
            "NM",
            2670.0,
            now=first_now,
        )
        upsert_baseline(
            conn,
            "card-1",
            "Mega Gengar",
            "https://example.com/a",
            "NM",
            2500.0,
            now=second_now,
        )

        baseline = get_baseline(conn, "card-1", "NM")
        assert baseline == BaselineRow(
            card_id="card-1",
            card_name="Mega Gengar",
            url="https://example.com/a",
            condition="NM",
            lowest_price=2500.0,
            created_at=first_now,
            updated_at=second_now,
        )
        assert conn.execute("SELECT COUNT(*) FROM price_baselines").fetchone()[0] == 1
    finally:
        conn.close()


def test_insert_scan_result_appends_rows(tmp_path: Path) -> None:
    conn = _open_db(tmp_path)
    try:
        insert_scan_result(
            conn,
            "card-1",
            "Mega Gengar",
            "https://example.com/a",
            "NM",
            2670.0,
            scanned_at="2026-06-11T01:02:03-03:00",
        )
        insert_scan_result(
            conn,
            "card-1",
            "Mega Gengar",
            "https://example.com/a",
            "SP",
            2350.0,
            scanned_at="2026-06-11T01:02:04-03:00",
        )

        rows = conn.execute(
            "SELECT id, condition, lowest_price FROM scan_results ORDER BY id"
        ).fetchall()
        assert [(row["id"], row["condition"], row["lowest_price"]) for row in rows] == [
            (1, "NM", 2670.0),
            (2, "SP", 2350.0),
        ]
    finally:
        conn.close()


def test_insert_scan_error_accepts_null_values(tmp_path: Path) -> None:
    conn = _open_db(tmp_path)
    try:
        insert_scan_error(
            conn,
            url="https://example.com/a",
            error_type="sprite_decode",
            card_id=None,
            error_message=None,
            occurred_at="2026-06-11T01:02:03-03:00",
        )

        row = conn.execute(
            """
            SELECT id, card_id, url, error_type, error_message, occurred_at
            FROM scan_errors
            """
        ).fetchone()
        assert row["id"] == 1
        assert row["card_id"] is None
        assert row["error_message"] is None
    finally:
        conn.close()


# --- collection_products (catalog watch, FRD §8, §21) ---------------------


def _product(product_id: str, *, name: str = "Produto", in_stock: bool = True,
             price: float | None = 99.99) -> Product:
    return Product(
        product_id=product_id,
        name=name,
        url=f"https://shisuistore.com.br/produtos/{product_id}/",
        price=price,
        in_stock=in_stock,
    )


def _rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT watch_id, product_id, product_name, product_url, price, in_stock,
               first_seen_at, last_seen_at
        FROM collection_products
        ORDER BY watch_id, product_id
        """
    ).fetchall()


def test_init_db_is_idempotent_and_seeds_an_empty_catalog_watch(tmp_path: Path) -> None:
    conn = _open_db(tmp_path)
    try:
        init_db(conn)  # second call must not raise or drop data
        assert get_known_stock(conn, "watch-1") == {}
        assert is_watch_seeded(conn, "watch-1") is False
    finally:
        conn.close()


def test_mark_watch_scanned_is_independent_of_stored_products(tmp_path: Path) -> None:
    conn = _open_db(tmp_path)
    try:
        first = "2026-07-31T09:00:00-03:00"
        later = "2026-08-01T09:00:00-03:00"

        # A watch whose page was empty: seeded, but with no product rows.
        mark_watch_scanned(conn, "watch-1", now=first)
        assert is_watch_seeded(conn, "watch-1") is True
        assert get_known_stock(conn, "watch-1") == {}
        assert is_watch_seeded(conn, "watch-2") is False

        mark_watch_scanned(conn, "watch-1", now=later)
        row = conn.execute(
            "SELECT first_scanned_at, last_scanned_at FROM catalog_watches WHERE watch_id = ?",
            ("watch-1",),
        ).fetchone()
        assert (row["first_scanned_at"], row["last_scanned_at"]) == (first, later)
    finally:
        conn.close()


def test_upsert_collection_products_seeds_rows(tmp_path: Path) -> None:
    conn = _open_db(tmp_path)
    try:
        now = "2026-07-31T09:00:00-03:00"
        upsert_collection_products(
            conn,
            "watch-1",
            [_product("a"), _product("b", in_stock=False, price=None)],
            now=now,
        )

        rows = _rows(conn)
        assert [(row["product_id"], row["in_stock"], row["price"]) for row in rows] == [
            ("a", 1, 99.99),
            ("b", 0, None),
        ]
        assert all(row["first_seen_at"] == now and row["last_seen_at"] == now for row in rows)
        assert get_known_stock(conn, "watch-1") == {"a": True, "b": False}
    finally:
        conn.close()


def test_upsert_refreshes_state_but_preserves_first_seen_at(tmp_path: Path) -> None:
    conn = _open_db(tmp_path)
    try:
        first = "2026-07-31T09:00:00-03:00"
        later = "2026-08-01T09:00:00-03:00"
        upsert_collection_products(conn, "watch-1", [_product("a", in_stock=False)], now=first)

        upsert_collection_products(
            conn,
            "watch-1",
            [_product("a", name="Novo nome", in_stock=True, price=80.0)],
            now=later,
        )

        (row,) = _rows(conn)
        assert row["first_seen_at"] == first
        assert row["last_seen_at"] == later
        assert row["product_name"] == "Novo nome"
        assert row["price"] == 80.0
        assert row["in_stock"] == 1
    finally:
        conn.close()


def test_products_absent_from_a_later_scan_are_never_deleted(tmp_path: Path) -> None:
    conn = _open_db(tmp_path)
    try:
        first = "2026-07-31T09:00:00-03:00"
        later = "2026-08-01T09:00:00-03:00"
        upsert_collection_products(conn, "watch-1", [_product("a"), _product("b")], now=first)

        upsert_collection_products(conn, "watch-1", [_product("a")], now=later)
        upsert_collection_products(conn, "watch-1", [_product("b")], now="2026-08-02T09:00:00-03:00")

        rows = {row["product_id"]: row for row in _rows(conn)}
        assert set(rows) == {"a", "b"}
        # 'b' disappeared and came back: still its original first sighting.
        assert rows["b"]["first_seen_at"] == first
        assert rows["b"]["last_seen_at"] == "2026-08-02T09:00:00-03:00"
    finally:
        conn.close()


def test_watches_keep_independent_rows_for_the_same_product_id(tmp_path: Path) -> None:
    conn = _open_db(tmp_path)
    try:
        now = "2026-07-31T09:00:00-03:00"
        upsert_collection_products(conn, "watch-1", [_product("a", in_stock=True)], now=now)
        upsert_collection_products(conn, "watch-2", [_product("a", in_stock=False)], now=now)

        assert get_known_stock(conn, "watch-1") == {"a": True}
        assert get_known_stock(conn, "watch-2") == {"a": False}
        assert len(_rows(conn)) == 2
    finally:
        conn.close()


def test_empty_product_list_is_a_no_op(tmp_path: Path) -> None:
    conn = _open_db(tmp_path)
    try:
        upsert_collection_products(conn, "watch-1", [], now="2026-07-31T09:00:00-03:00")
        assert _rows(conn) == []
    finally:
        conn.close()
