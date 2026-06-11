"""Tests for the SQLite storage layer (FRD §8)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from services.storage import (
    BaselineRow,
    connect,
    get_baseline,
    init_db,
    insert_scan_error,
    insert_scan_result,
    upsert_baseline,
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
