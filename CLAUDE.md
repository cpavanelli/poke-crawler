# CLAUDE.md

Guidance for working in this repository. **`FRD.md` is the source of truth** — when in doubt, read it. This file only highlights the non-obvious rules and constraints that are easy to violate accidentally.

## What this is

Pokémon Card Price Watcher: a single-run Python app that scans configured LigaPokemon card pages, tracks the lowest listing price per condition, stores history + an all-time-low baseline in SQLite, and posts a Discord webhook when a new all-time-low is found. It runs on a Raspberry Pi via cron (one process per invocation, not a daemon).

## Hard constraints (do not break these)

These are deliberate decisions, not oversights. Don't "improve" them without asking:

- **No ORM.** Use raw `sqlite3` only.
- **One request at a time. No parallelism, no async fan-out.** Sequential processing only (FRD §14). Respect `REQUEST_DELAY_SECONDS` between cards and `SPRITE_REQUEST_DELAY_SECONDS` before a sprite fetch.
- **The digit sprite is never written to disk.** Load it via `io.BytesIO` from the HTTP response and discard after decoding (FRD §4). This is an SD-card-wear decision.
- **Shipping is never stored, compared, or notified.** Lowest price = listing price only (FRD §5).
- **Stop the current cycle on HTTP 403 or 429** (FRD §12, §17). No proxy rotation, no CAPTCHA bypass, no automated login. Stay polite to the source.
- **`card_id = SHA256(url)`.** Card names are display-only metadata (FRD §9).

## Raspberry Pi / 24/7 reliability rules

This runs unattended for long periods on an SD card. Keep changes SD-friendly and crash-safe:

- **SQLite opens with `journal_mode=WAL` and `synchronous=NORMAL`** on every connection (FRD §8).
- **Logs use a size-capped rotating handler** (`LOG_MAX_BYTES`, `LOG_BACKUP_COUNT`) — never let logs grow unbounded (FRD §16).
- **Lock file stores a PID** (FRD §15). On startup: stale lock (PID not alive) is overwritten and the run continues; live PID means another run is in progress, so exit. Always remove the lock on normal shutdown.
- **Timestamps are local time**, and the host is assumed to have NTP enabled (FRD §16, §18).
- **Sprite decode failure** = skip that listing, log to `scan_errors` with `error_type=sprite_decode`, send a Discord alert, and continue. It is NOT a per-card parser failure (FRD §7, §10, §12).

## Architecture

- **Parser interface** (FRD §11): each marketplace parser implements `can_handle(url) -> bool` and `parse(html, card_config)`. New marketplaces (e.g. MYP Cards) must follow this contract — don't special-case marketplaces in the scanner.
- **LigaPokemon parsing** (FRD §10): listing data is inline JS in the raw HTML (`cards_stock`, `cards_stores`, `dataQuality`), not rendered DOM. No headless browser needed. Most listings expose `precoFinal` directly; only `precoCss` (anti-scrape, `lj_tipo=15`) listings need the sprite-decode path.
- **Layout** (FRD §19): `app.py` entrypoint; `parsers/`, `services/` (scanner, notifier, storage, config), `models/`, `logs/`.

## Stack & commands

- Python 3.12+, `httpx`, `BeautifulSoup4`, `sqlite3`, `python-dotenv`, `pytest`.
- Run: `python app.py` (single scan cycle, guarded by the lock file).
- Test: `pytest`.
- Config: cards in `cards.json` (unknown JSON keys must be ignored for forward compatibility — FRD §3); secrets/tuning in `.env`.

## Conventions

- Error policy is "log and continue" for transient/per-item failures; only invalid configuration aborts startup (FRD §12).
- Retry transient fetch failures at most twice, 5s apart, then log and move on (FRD §13).
