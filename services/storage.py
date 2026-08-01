"""SQLite storage helpers for baselines, scan results, errors, and catalog state (FRD §8)."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from models.product import Product


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

        CREATE TABLE IF NOT EXISTS catalog_watches (
            watch_id TEXT PRIMARY KEY,
            first_scanned_at TEXT NOT NULL,
            last_scanned_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS collection_products (
            watch_id TEXT NOT NULL,
            product_id TEXT NOT NULL,
            product_name TEXT NOT NULL,
            product_url TEXT NOT NULL,
            price REAL,
            in_stock INTEGER NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            PRIMARY KEY(watch_id, product_id)
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


def is_watch_seeded(conn: sqlite3.Connection, watch_id: str) -> bool:
    """Whether this catalog watch has completed a scan before (FRD §21).

    Tracked separately from the product rows: a collection page can legitimately
    be empty, and "no products stored" must not be mistaken for "never scanned"
    or the first product to appear would be seeded silently instead of alerting.
    """
    row = conn.execute(
        "SELECT 1 FROM catalog_watches WHERE watch_id = ?", (watch_id,)
    ).fetchone()
    return row is not None


def mark_watch_scanned(conn: sqlite3.Connection, watch_id: str, *, now: str) -> None:
    """Record that a catalog watch completed a scan, preserving the first one."""
    conn.execute(
        """
        INSERT INTO catalog_watches (watch_id, first_scanned_at, last_scanned_at)
        VALUES (?, ?, ?)
        ON CONFLICT(watch_id) DO UPDATE SET last_scanned_at = excluded.last_scanned_at
        """,
        (watch_id, now, now),
    )
    conn.commit()


def get_known_stock(conn: sqlite3.Connection, watch_id: str) -> dict[str, bool]:
    """Return ``product_id -> in_stock`` for one catalog watch (FRD §21)."""
    rows = conn.execute(
        "SELECT product_id, in_stock FROM collection_products WHERE watch_id = ?",
        (watch_id,),
    ).fetchall()
    return {row["product_id"]: bool(row["in_stock"]) for row in rows}


def upsert_collection_products(
    conn: sqlite3.Connection,
    watch_id: str,
    products: Sequence[Product],
    *,
    now: str,
) -> None:
    """Insert new products and refresh known ones for a watch (FRD §8, §21).

    ``first_seen_at`` is preserved on conflict, so a product that disappears
    and later returns keeps its original first sighting. Rows are never deleted.
    """
    if not products:
        return

    conn.executemany(
        """
        INSERT INTO collection_products (
            watch_id, product_id, product_name, product_url, price, in_stock,
            first_seen_at, last_seen_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(watch_id, product_id) DO UPDATE SET
            product_name = excluded.product_name,
            product_url = excluded.product_url,
            price = excluded.price,
            in_stock = excluded.in_stock,
            last_seen_at = excluded.last_seen_at
        """,
        [
            (
                watch_id,
                product.product_id,
                product.name,
                product.url,
                product.price,
                int(product.in_stock),
                now,
                now,
            )
            for product in products
        ],
    )
    conn.commit()


def local_now_iso() -> str:
    """Return the host-local time as an ISO-8601 string (FRD §16)."""
    return datetime.now().astimezone().isoformat(timespec="seconds")
