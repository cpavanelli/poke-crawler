# Plan: Issues #2 (Storage layer) and #3 (LigaPokemon parser, happy path)

Both issues depend only on #1 (already merged) and are independent of each other,
so they can be built in either order or in parallel. Both are **pure, offline,
unit-testable** — no network, no Discord, no scanner wiring yet.

Source of truth: `FRD.md`. Match existing conventions from `models/` and
`services/config.py`: frozen `@dataclass(slots=True)`, `from __future__ import
annotations`, FRD section references in docstrings, hermetic tests.

---

## Issue #2 — Storage layer (SQLite) · FRD §8

### New file: `services/storage.py`

Raw `sqlite3` only — no ORM (hard constraint). The module owns connection
creation, schema init, and the read/write helpers for the three tables.

**Connection factory**
- `connect(path) -> sqlite3.Connection`: open the DB and apply the durability
  pragmas on **every** connection (FRD §8, hard constraint):
  - `PRAGMA journal_mode=WAL`
  - `PRAGMA synchronous=NORMAL`
  - Set `row_factory = sqlite3.Row` so reads return mapping-style rows.
  - Accept `Path | str | ":memory:"` so tests can use in-memory / temp-file DBs.

**Schema init**
- `init_db(conn) -> None`: `CREATE TABLE IF NOT EXISTS` for all three tables,
  copied verbatim from FRD §8:
  - `price_baselines` — PK `(card_id, condition)`; columns `card_id, card_name,
    url, condition, lowest_price, created_at, updated_at`.
  - `scan_results` — autoincrement `id`; columns `card_id, card_name, url,
    condition, lowest_price, scanned_at`.
  - `scan_errors` — autoincrement `id`; columns `card_id (nullable), url,
    error_type, error_message (nullable), occurred_at`.
- Idempotent: safe to call on every run.

**Read/write functions** (thin, explicit, one statement each)
- `get_baseline(conn, card_id, condition) -> BaselineRow | None`
- `upsert_baseline(conn, card_id, card_name, url, condition, lowest_price, *, now)`
  — `INSERT ... ON CONFLICT(card_id, condition) DO UPDATE SET lowest_price=...,
  updated_at=...`. On insert, `created_at == updated_at == now`; on update,
  `created_at` is preserved.
- `insert_scan_result(conn, card_id, card_name, url, condition, lowest_price, *, scanned_at)`
- `insert_scan_error(conn, *, url, error_type, card_id=None, error_message=None, occurred_at)`
  — `error_type` is a free string today; the known value `sprite_decode`
  (FRD §10) is supplied by callers, not enumerated here.

**Timestamps**
- Callers pass timestamps in (local-time strings, FRD §16). Storage does **not**
  call `datetime.now()` itself — keeps it pure and tests deterministic. Provide a
  small `local_now_iso()` helper here or defer it to a shared util; callers in the
  scanner (issue beyond this plan) will use it.

**Return shape**
- Either return `sqlite3.Row` directly, or a tiny frozen `BaselineRow`
  dataclass for `get_baseline`. Prefer a `BaselineRow` dataclass for a typed,
  self-documenting baseline read; raw rows are fine for the others.

### Tests: `tests/test_storage.py`
- Use a temp-file DB (`tmp_path`) and/or `:memory:`; no network.
- `init_db` round-trips: insert then read back each table.
- Pragmas verified as applied: query `PRAGMA journal_mode` (==`wal` for a file
  DB — note `:memory:` reports `memory`, so assert WAL against a `tmp_path`
  file) and `PRAGMA synchronous` (==`1`, NORMAL).
- `upsert_baseline`: first call inserts (created_at==updated_at); second call
  with a lower price updates `lowest_price`+`updated_at` and **preserves**
  `created_at`; PK conflict does not duplicate rows.
- `insert_scan_result` appends rows (autoincrement id grows).
- `insert_scan_error` accepts a `NULL` `card_id` and `NULL` `error_message`.
- `init_db` is idempotent (call twice, no error).

### Acceptance (from issue #2)
- ✅ Temp/in-memory DB round-trips each table.
- ✅ Pragmas verified as applied.
- ✅ Pure unit tests, no network.

---

## Issue #3 — LigaPokemon parser, happy path · FRD §10–11

Happy path = `precoFinal` listings only. The `precoCss` sprite-decode path
(FRD §10 obfuscated prices) is **explicitly out of scope** here — leave a clear
seam for it and skip such listings for now (a later issue adds decoding).

### New file: `parsers/base.py`

The marketplace parser contract (FRD §11, hard architectural constraint — the
scanner must never special-case marketplaces):

```python
class MarketplaceParser(Protocol):  # or abc.ABC
    def can_handle(self, url: str) -> bool: ...
    def parse(self, html: str, card: Card) -> list[PriceResult]: ...
```

- Reuse existing `models.card.Card` and `models.price_result.PriceResult`.
- `parse` returns `list[PriceResult]` — one entry per **configured** condition
  that had at least one listing, holding the lowest listing price (FRD §5:
  listing price only, shipping never involved).
- Decide ABC vs `typing.Protocol`. Recommend a lightweight `abc.ABC` base so
  parsers are discoverable/registerable by the scanner later.

### New file: `parsers/ligapokemon.py` — `LigaPokemonParser`

**`can_handle(url)`**
- `True` when the host is `ligapokemon.com.br` (parse with `urllib.parse`, match
  on netloc suffix — robust to `www.` and query strings).

**`parse(html, card)`** steps (FRD §10):
1. Extract the three inline JS variables from raw HTML (not DOM — no headless
   browser). Locate `var cards_stock = ...`, `var cards_stores = ...`,
   `var dataQuality = ...` and read each assigned JS array/object literal.
   - Approach: regex to find the `var NAME = ` anchor, then balanced-bracket
     scan to capture the literal up to its terminating `;`, then `json.loads`.
     LigaPokemon emits valid-JSON literals; if a fixture proves otherwise,
     isolate any JS→JSON normalization in one helper.
2. Build the condition map from `dataQuality`: `id -> acron` (the table in
   FRD §10: 1→M, 2→NM, 3→SP, 4→MP, 5→HP, 6→D). Read it from the page rather than
   hardcoding, so a site change surfaces in the fixture.
   - **Gotcha (verified against the captured fixture):** in `dataQuality` the
     key is **`id`** (an **int**), while in `cards_stock` the listing field is
     **`qualid`** (a **string**, e.g. `"3"`). The map must coerce — `int(qualid)`
     — or every lookup silently misses. The plan's earlier "`qualid -> acron`"
     shorthand really means `cards_stock.qualid → int → dataQuality.id → acron`.
3. Iterate `cards_stock` listings:
   - Resolve the listing's `qualid` (coerced to int) to its `acron` via the map.
   - Keep only listings whose `acron` is in `card.conditions`.
   - **Happy path:** if `precoFinal` present, parse it to `float`. Note
     `precoFinal` is a **decimal string** (e.g. `"2670.00" -> 2670.00`). If a
     listing has no `precoFinal` (some carry `precoCss` instead, others simply
     lack it), **skip** it for now — leave a `# TODO(precoCss): sprite-decode
     path, FRD §10` seam. Do not crash.
4. Compute the lowest `precoFinal` per matched condition.
5. Return `list[PriceResult]` — only for conditions that had ≥1 usable listing.
   A configured condition with no listings simply isn't in the output (the
   scanner treats "no matching condition" as log-and-continue, FRD §12).

**Robustness within happy path**
- Missing/empty `cards_stock` → return `[]` (parser failure handling is the
  caller's job; here, return empty rather than raising for an empty page).
- Malformed individual listing (missing `qualid`, unparseable price) → skip that
  listing, continue. Raising for a structural failure (e.g. `cards_stock`
  variable entirely absent) is acceptable and surfaces as parser failure upstream.

### Test fixtures: `tests/fixtures/ligapokemon/`
- ✅ **Real fixture already captured** (regression anchor):
  `tests/fixtures/ligapokemon/mega_gengar_284.html`. Captured with a single
  polite `httpx` GET of the configured Mega Gengar URL using a browser-like
  User-Agent → HTTP 200, ~226 KB. **Not** wired into the test suite as a live
  fetch — it's a static committed file.
  - The captured page contains all three inline-JS vars (`cards_stock` = 26
    listings, `cards_stores` = 22 stores, `dataQuality` = 6 conditions) and
    **no** `precoCss` listings, so it exercises the happy path fully across
    three conditions (M, NM, SP).
  - Because it has no `precoCss` listing, the "precoCss is skipped" test should
    use a **small hand-crafted fixture** (or an inline HTML string in the test)
    that injects one `precoCss`-only listing — keep that separate from the real
    anchor.
- **Expected values from the captured fixture** (use as exact test assertions):
  | condition | lowest_price | usable listings |
  |---|---|---|
  | M  | 2687.04 | 1 |
  | NM | 2670.00 | 17 |
  | SP | 2350.00 | 7 |

  (Total stock is 26; one listing has no `precoFinal` and is skipped, leaving 25
  priced listings — 1 M + 17 NM + 7 SP.) For a `card` configured with
  `conditions=("NM","SP")`, `parse` must return exactly NM→2670.00 and
  SP→2350.00, with M filtered out.

### Tests: `tests/test_ligapokemon_parser.py`
- `can_handle`: true for ligapokemon URLs (with/without `www`, with query
  string), false for other hosts (e.g. a future mypcards URL).
- Saved HTML fixture → expected `[{condition, lowest_price}]` (issue acceptance).
- Multiple listings for one condition → lowest wins.
- Conditions not in `card.conditions` are filtered out.
- A `precoCss`-only listing is skipped (not counted, no crash).
- A condition with no listings is absent from the result.
- `qualid -> acron` mapping resolves correctly from `dataQuality`.
- No network in any test.

### Acceptance (from issue #3)
- ✅ Saved HTML fixtures → expected `[{condition, lowest_price}]`.
- ✅ No network.
- ✅ A real fixture page committed as the regression anchor.

---

## Out of scope (do not build here)
- `precoCss` sprite decode + sprite fetch/delay (FRD §10) — later issue; leave the seam.
- Page fetching / retry / 403–429 handling (scanner + HTTP, FRD §12–13, §17).
- Discord notifier, baseline-comparison logic, lock file, logging, `app.py` wiring.
- `parsers/mypcards.py`.

## Suggested order & PRs
1. **PR A — issue #2:** `services/storage.py` + `tests/test_storage.py`. Smallest, fully isolated.
2. **PR B — issue #3:** `parsers/base.py`, `parsers/ligapokemon.py`, fixture(s), `tests/test_ligapokemon_parser.py`.

Each PR: `pytest` green, no network, references the issue number in the commit
(`closes #2` / `closes #3`).
