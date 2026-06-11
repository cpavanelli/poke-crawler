"""SQLite storage helpers for baselines, scan results, and scan errors (FRD §8)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(slots=True, frozen=True)
class BaselineRow:
    """A stored baseline row from ``price_baselines``."""

    card_id: str
    card_name: str
    url: str
    condition: str
    lowest_price: float
    created_at: str
    updated_at: str


def connect(path: Path | str) -> sqlite3.Connection:
    """Open a SQLite connection and apply the FRD durability pragmas."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL").fetchone()
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create the schema for the three storage tables if needed."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS price_baselines (
            card_id TEXT NOT NULL,
            card_name TEXT NOT NULL,
            url TEXT NOT NULL,
            condition TEXT NOT NULL,
            lowest_price REAL NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(card_id, condition)
        );

        CREATE TABLE IF NOT EXISTS scan_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_id TEXT NOT NULL,
            card_name TEXT NOT NULL,
            url TEXT NOT NULL,
            condition TEXT NOT NULL,
            lowest_price REAL NOT NULL,
            scanned_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS scan_errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_id TEXT,
            url TEXT NOT NULL,
            error_type TEXT NOT NULL,
            error_message TEXT,
            occurred_at TEXT NOT NULL
        );
        """
    )
    conn.commit()


def get_baseline(
    conn: sqlite3.Connection, card_id: str, condition: str
) -> BaselineRow | None:
    """Return a stored baseline row for one card and condition, if present."""
    row = conn.execute(
        """
        SELECT card_id, card_name, url, condition, lowest_price, created_at, updated_at
        FROM price_baselines
        WHERE card_id = ? AND condition = ?
        """,
        (card_id, condition),
    ).fetchone()
    if row is None:
        return None
    return BaselineRow(
        card_id=row["card_id"],
        card_name=row["card_name"],
        url=row["url"],
        condition=row["condition"],
        lowest_price=float(row["lowest_price"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def upsert_baseline(
    conn: sqlite3.Connection,
    card_id: str,
    card_name: str,
    url: str,
    condition: str,
    lowest_price: float,
    *,
    now: str,
) -> None:
    """Insert or update a baseline row, preserving ``created_at`` on conflict."""
    conn.execute(
        """
        INSERT INTO price_baselines (
            card_id, card_name, url, condition, lowest_price, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(card_id, condition) DO UPDATE SET
            lowest_price = excluded.lowest_price,
            updated_at = excluded.updated_at
        """,
        (card_id, card_name, url, condition, lowest_price, now, now),
    )
    conn.commit()


def insert_scan_result(
    conn: sqlite3.Connection,
    card_id: str,
    card_name: str,
    url: str,
    condition: str,
    lowest_price: float,
    *,
    scanned_at: str,
) -> None:
    """Append one scan result row."""
    conn.execute(
        """
        INSERT INTO scan_results (
            card_id, card_name, url, condition, lowest_price, scanned_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (card_id, card_name, url, condition, lowest_price, scanned_at),
    )
    conn.commit()


def insert_scan_error(
    conn: sqlite3.Connection,
    *,
    url: str,
    error_type: str,
    card_id: str | None = None,
    error_message: str | None = None,
    occurred_at: str,
) -> None:
    """Append one scan error row."""
    conn.execute(
        """
        INSERT INTO scan_errors (
            card_id, url, error_type, error_message, occurred_at
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (card_id, url, error_type, error_message, occurred_at),
    )
    conn.commit()


def local_now_iso() -> str:
    """Return the host-local time as an ISO-8601 string (FRD §16)."""
    return datetime.now().astimezone().isoformat(timespec="seconds")
