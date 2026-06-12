# Plan: Issue #7 — Scanner orchestration (`services/scanner.py`) · FRD §6, §7, §12

The scanner is the **composition root for one scan cycle**. It ties together the
pieces delivered by #2–#6 (and #4/#12 sprite decoding): the HTTP fetcher, the
marketplace parser, the lowest-price reduction, SQLite storage, and the Discord
notifier. It owns the **10-step per-card workflow** of FRD §6 and the loop over
the configured card list, and nothing else.

Source of truth: `FRD.md` §6 (the 10 steps), §7 (when a notification fires and
the previous-lowest semantics), §12 (the per-error behaviour table), §13/§17
(retry + 403/429 stop-cycle — already enforced by the fetcher).

**Depends on (all merged):** #2 storage (`services/storage.py`), #3 LigaPokemon
parser (`parsers/ligapokemon_parser.py`), #5 fetcher (`services/fetcher.py`), #6
notifier (`services/notifier.py`), plus pricing (`services/pricing.py`) and
config (`services/config.py`). #4/#12 sprite decoding is consumed transitively
through the parser.

Match the conventions already established across the repo (read
`services/fetcher.py`, `services/notifier.py`, and `tools/list_prices.py` before
writing): `from __future__ import annotations`; FRD section refs in docstrings;
typed signatures; keyword-only arguments for multi-field calls; dependency
injection of every collaborator so tests run **offline**; module logger via
`logging.getLogger(__name__)` and **no** logging-handler setup here.

---

## Scope boundary (decided)

**In scope:** `services/scanner.py` + `tests/test_scanner.py`.

**Out of scope — explicit seam for a follow-up `app.py` issue:** the entrypoint
that *builds* the scanner's collaborators and runs one cycle. The scanner does
**not** load config, set up the rotating log handler (§16), acquire/release the
lock file (§15), or open the DB connection. Those are constructor-injected. The
"wiring notes" section below sketches the future `app.py` for context only — do
not build it here. This mirrors how the #6 notifier plan left scanner wiring out.

The cron-driven "scan every CHECK_INTERVAL_MINUTES" cadence (§4, §18) is **not**
the scanner's concern either — each cron invocation is a single run; the scanner
performs exactly one pass over the card list per call.

---

## The 10-step workflow (FRD §6), mapped to code

For **each** configured card:

| # | FRD step | Implementation |
|---|----------|----------------|
| 1 | Load configuration | `Card` is passed in (already validated by `services/config.py`). |
| 2 | Fetch page | `fetcher.get_page(card.url)` (shared fetcher: retry §13, 403/429 stop §17). |
| 3 | Parse listings | `parser.parse_listings(html)` → `list[Listing]` (all conditions). |
| 4 | Filter by configured conditions | inside `lowest_prices(...)`. |
| 5 | Determine lowest per condition | `lowest_prices(listings, card.conditions)` → `list[PriceResult]`. |
| 6 | Store scan history | `storage.insert_scan_result(...)` per `PriceResult`. |
| 7 | Compare against baseline | `storage.get_baseline(card_id, condition)`. |
| 8 | Notify if new all-time-low | `notifier.notify_all_time_low(...)` **only** when `current < baseline`. |
| 9 | Update baseline | `storage.upsert_baseline(...)` (first-seen, or on a new low). |
| 10 | Wait request delay | `fetcher.wait_between_cards()` — **between** cards, owned by the loop. |

Steps 6–9 run **once per `PriceResult`** (per configured condition that had at
least one listing). Step 10 is loop-level, not per-condition.

---

## Public surface

A small `Scanner` class holding its injected collaborators (the repo uses classes
for stateful service wrappers — `HttpFetcher`, `DiscordNotifier` — and functions
for stateless reductions — `pricing`, `storage`; the scanner *holds*
collaborators, so a class fits).

```python
class Scanner:
    def __init__(
        self,
        *,
        fetcher: HttpFetcher,
        notifier: DiscordNotifier,
        conn: sqlite3.Connection,
        parsers: Sequence[ParserFactory] | None = None,  # see "Parser selection"
        send_initial_baseline: bool = False,
        clock: Callable[[], str] = local_now_iso,
    ) -> None: ...

    def run(self, cards: Sequence[Card]) -> ScanSummary:
        """One full pass over the card list (FRD §6). Stops early on 403/429."""

    def scan_card(self, card: Card) -> CardOutcome:
        """Steps 1–9 for a single card. Raises CycleStop on 403/429."""
```

- `send_initial_baseline` is the already-validated
  `AppConfig.send_initial_baseline_notification` value — the scanner is the
  component that *gates* on it (the notifier deliberately does not, per the #6
  plan). Pass the bool, not the whole `AppConfig`, to keep the scanner decoupled
  from config shape.
- `clock` injects `local_now_iso` (`services/storage.py:167`) so tests can pin
  timestamps. One `now = self._clock()` is captured **per card** and reused for
  that card's `scan_results` and baseline rows (consistent timestamps across a
  card's rows; §16 local-time).

### Return values (lightweight, for logging + test assertions)

Define two frozen slotted dataclasses (module-local, mirror `BaselineRow`):

```python
@dataclass(slots=True, frozen=True)
class CardOutcome:
    card_id: str
    results: tuple[PriceResult, ...]   # lowest per condition that had listings
    new_lows: tuple[str, ...]          # conditions that fired an all-time-low
    initial_baselines: tuple[str, ...] # conditions seen for the first time
    error_type: str | None = None      # 'fetch' | 'parse' | None (per-card fault)

@dataclass(slots=True, frozen=True)
class ScanSummary:
    cards_scanned: int      # cards that completed scan_card without a per-card fault
    cards_failed: int       # cards logged to scan_errors (fetch/parse)
    new_lows: int           # total all-time-lows notified across the cycle
    stopped_early: bool     # True if a 403/429 cut the cycle short
```

These exist so `run()` can log a one-line cycle summary (§16) and so tests assert
behaviour without scraping logs or the DB. Keep them minimal; do not gold-plate.

---

## `run(cards)` — the loop (steps 2–10 orchestration)

```
summary counters = 0
for index, card in enumerate(cards):
    try:
        outcome = self.scan_card(card)
        update counters from outcome
    except CycleStop as exc:
        log.warning("Stopping cycle: HTTP %s from %s", exc.status_code, exc.url)
        record scan_error(error_type=f"http_{status}")   # see taxonomy below
        stopped_early = True
        break                      # FRD §12/§17: stop the WHOLE cycle
    if index < len(cards) - 1:
        self.fetcher.wait_between_cards()   # step 10 — polite delay BETWEEN cards
return ScanSummary(...)
```

Notes:
- The between-card delay is applied **after** each card except the last, and is
  **skipped** when a `CycleStop` breaks the loop (we stop being polite by
  stopping entirely). It runs even after a per-card `fetch`/`parse` fault was
  logged-and-swallowed (politeness to the source still applies before the next
  request).
- `scan_card` is the only place `CycleStop` escapes to; everything else inside
  `scan_card` is contained (log-and-continue).

---

## `scan_card(card)` — steps 1–9, per-card error isolation (FRD §12)

```
card_id = card.card_id
now = self._clock()
parser = self._select_parser(card)            # by can_handle(url); see below

# Step 2 — fetch. CycleStop is NOT caught here: it must propagate to run().
try:
    html = self._fetcher.get_page(card.url)
except CycleStop:
    raise                                      # 403/429 → stop cycle
except FetchError as exc:                      # timeout/network after retries §13
    log.error(...); record scan_error(card_id, url, "fetch", str(exc), now)
    return CardOutcome(..., error_type="fetch")

# Step 3 — parse. CycleStop from an inner sprite fetch also propagates.
try:
    listings = parser.parse_listings(html)
except CycleStop:
    raise
except (ValueError, Exception-the-parser-may-raise) as exc:
    log.error(...); record scan_error(card_id, url, "parse", str(exc), now)
    return CardOutcome(..., error_type="parse")

# Steps 4–5 — reduce.
results = lowest_prices(listings, card.conditions)

# "No matching condition" (FRD §12): a configured condition with zero listings
# simply produces no PriceResult. Log it as a no-op; NO scan_errors row, NO DB
# write, NO notification (decided).
missing = [c for c in card.conditions if c not in {r.condition for r in results}]
if missing: log.info("No listings for %s conditions %s", card.name, missing)

# Steps 6–9 — per condition.
for result in results:
    self._record_and_compare(card, card_id, result, now)   # see below

return CardOutcome(card_id, tuple(results), ...)
```

### `_record_and_compare(card, card_id, result, now)` — steps 6–9 for one condition

```
# Step 6 — always store history (independent of baseline outcome).
storage.insert_scan_result(conn, card_id, card.name, card.url,
                           result.condition, result.lowest_price, scanned_at=now)

# Step 7 — compare.
baseline = storage.get_baseline(conn, card_id, result.condition)
current  = result.lowest_price

if baseline is None:
    # First time we've ever seen this card+condition → create baseline (§16
    # "baseline creation"). Optionally notify (gated on the env flag).
    storage.upsert_baseline(conn, card_id, card.name, card.url,
                            result.condition, current, now=now)
    log.info("Baseline created: %s %s = %s", card.name, result.condition, current)
    if self._send_initial_baseline:
        self._notifier.notify_initial_baseline(
            card_name=card.name, condition=result.condition,
            price=current, url=card.url)
    return "initial"

if current < baseline.lowest_price:                      # Step 8 — strict '<' (§7)
    log.info("New all-time-low: %s %s %s -> %s", card.name, result.condition,
             baseline.lowest_price, current)
    self._notifier.notify_all_time_low(                  # notify BEFORE update,
        card_name=card.name, condition=result.condition, #   so previous_lowest =
        price=current, previous_lowest=baseline.lowest_price, url=card.url)
    storage.upsert_baseline(conn, card_id, card.name, card.url,  # Step 9
                            result.condition, current, now=now)
    return "new_low"

# current >= baseline: no notification, baseline unchanged (it only ever
# decreases — it is the all-time LOW, §7).
return None
```

**Critical ordering / semantics (state them; do not silently vary):**
- **Strict `<`.** `current == baseline` is *not* a new low — no notify, no
  update (FRD §7: "Current Price < Stored Baseline"). A test pins this exact
  boundary.
- **Notify uses the *old* baseline** as `previous_lowest`. Capture it before the
  `upsert_baseline`, then notify, then update. Even if the notify returns `False`
  (Discord failed-and-logged, §12), the baseline is still updated — a delivery
  failure must not corrupt the all-time-low record.
- **Initial baseline is always created on first sight**, regardless of the env
  flag. The flag gates only the *notification*, not the DB write.
- **`scan_results` is written for every condition with a lowest price**, whether
  or not a notification fires — it is the price history (§6 step 6), separate
  from the baseline.

---

## Parser selection (FRD §11 — "don't special-case marketplaces in the scanner")

The LigaPokemon parser needs `sprite_fetcher` + `on_sprite_error` injected at
construction, and `on_sprite_error` needs **per-card** context (card name/url for
the `scan_errors` insert and the Discord alert). So the parser is built **per
card** behind a tiny registry, keeping the scanner marketplace-agnostic.

```python
# A factory builds a parser given the per-card sprite hooks.
ParserFactory = Callable[[SpriteFetcher, SpriteErrorHandler], MarketplaceParser]

DEFAULT_PARSERS: tuple[ParserFactory, ...] = (
    lambda fetch, on_err: LigaPokemonParser(sprite_fetcher=fetch, on_sprite_error=on_err),
)
```

`_select_parser(card)`:
1. Build the per-card sprite-error handler closure (see below).
2. For each factory in `self._parsers`, construct the parser and return the first
   whose `can_handle(card.url)` is `True`.
3. If none handle the URL → log + `record scan_error(card_id, url, "parse",
   "no parser for url", now)` and treat as a per-card parse fault (return early
   from `scan_card` with `error_type="parse"`). This should never happen for a
   validated config, but fail closed and politely.

A future `MypCardsParser` is added by appending one factory to `DEFAULT_PARSERS`
— its factory simply ignores the sprite hooks. The registry lives in
`services/scanner.py` for now (issue scope); it is trivially extractable to
`parsers/registry.py` later. **Flagged as a deliberate placement decision.**

### Per-card `on_sprite_error` closure (wires §10 step 3 + §7 alert)

When a `precoCss` listing can't be decoded, the parser invokes this callback
(at most **once per page** — the parser already dedupes, see
`parsers/ligapokemon_parser.py:60`). The scanner's callback must do **both**
halves of FRD §10:

```python
def on_sprite_error(message: str) -> None:
    storage.insert_scan_error(conn, card_id=card_id, url=card.url,
                              error_type="sprite_decode",
                              error_message=message, occurred_at=now)   # §10 step 3
    self._notifier.notify_sprite_decode_failure(                       # §7 alert
        card_name=card.name, url=card.url)
    log.warning("Sprite decode failed for %s: %s", card.name, message)
```

A sprite-decode failure is a **per-listing skip**, not a card fault — the parser
swallows it and keeps decoding the rest, so a decodable listing can still yield a
valid lowest price (FRD §10). The scanner does nothing special beyond this
callback; `scan_card` continues normally with whatever listings survived.

---

## `scan_errors` taxonomy (decided)

`error_type` values written by the scanner:

| `error_type` | When | Source |
|---|---|---|
| `sprite_decode` | precoCss listing undecodable (per-listing skip) | parser callback (FRD §10/§12, the only name FRD fixes) |
| `fetch` | `FetchError` — timeout/network after the §13 retry budget | step 2 |
| `parse` | parser raised (`ValueError`, malformed page) or no parser matched | step 3 / selection |
| `http_403` / `http_429` | `CycleStop` — recorded in `run()` before breaking the cycle | step 2 (or inner sprite fetch) |

**"No matching condition" is NOT written to `scan_errors`** — it is a logged
no-op (no DB write, no notification). It is an empty result, not a failure.

`storage.insert_scan_error` already accepts `card_id`, `url`, `error_type`,
`error_message`, `occurred_at` (`services/storage.py:145`). For `CycleStop` the
`error_message` is e.g. `"HTTP 429 from <url>"`.

---

## Error behaviour — the full FRD §12 table, realised

| Error | Where | Action |
|---|---|---|
| Timeout / network | fetcher raises `FetchError` after 2 attempts (§13) | log + `scan_errors('fetch')`, **continue** to next card |
| Parser failure | `parse_listings` raises | log + `scan_errors('parse')`, **continue** |
| Sprite decode | parser callback | skip listing, `scan_errors('sprite_decode')`, Discord alert, **continue** |
| No matching condition | empty `lowest_prices` for a condition | log only, **continue** (no DB, no notify) |
| Discord failure | notifier returns `False` | already log-and-continue inside the notifier (§6 plan); scanner does not react |
| HTTP 403 / 429 | fetcher raises `CycleStop` | log + `scan_errors('http_4xx')`, **stop the whole cycle** (break the loop) |
| Invalid config | n/a | handled at config load (§3); never reaches the scanner |

The scanner never retries — retry is the fetcher's job (§13). The scanner's only
"stop everything" trigger is `CycleStop`.

---

## Tests: `tests/test_scanner.py` (offline; real parser + real storage)

Per the issue acceptance — **real parser/storage, mocked HTTP + Discord.**
Mirror the existing offline patterns:
- **HTTP:** build a real `HttpFetcher` over `httpx.Client(transport=
  httpx.MockTransport(handler))` with `sleep=list.append` (no real waits), as in
  `tests/test_fetcher.py`'s `QueueHandler`/`_client`/`_fetcher` helpers.
- **Discord:** either a real `DiscordNotifier` over a `MockTransport` (assert the
  posted `{"content": ...}` body, as in `tests/test_notifier.py`), or a small
  spy/fake notifier recording calls. Prefer the **spy** for the comparison-logic
  tests (cleaner assertions on *which* notify fired) and one real-transport test
  to prove wiring.
- **Storage:** real `sqlite3` via `storage.connect(":memory:")` +
  `storage.init_db(conn)`; assert rows directly with SQL.
- **Parser:** the real `LigaPokemonParser`, fed via fixtures
  `tests/fixtures/ligapokemon/mega_gengar_284.html` (plain `precoFinal`) and
  `greninja_116_precocss.html` + `greninja_116_sprite.jpg` (the sprite path).
  Map the page URL → fixture HTML and the sprite URL → fixture bytes in the
  `MockTransport` handler (route by `request.url.path`).

**Core acceptance tests (the issue calls these out explicitly):**

1. **First sight → baseline created, no all-time-low notification** (flag off).
   `get_baseline` was `None`; after the scan `price_baselines` has the row,
   `scan_results` has the row, and `notify_all_time_low` was **not** called.
2. **First sight with `send_initial_baseline=True` → initial-baseline notify
   fires** exactly once per condition; baseline still created.
3. **New all-time-low: `current < baseline` → notify fires and baseline
   updates.** Seed a baseline at 500.0; scan yields 490.0; assert
   `notify_all_time_low` called once with `price=490.0, previous_lowest=500.0`,
   and `price_baselines.lowest_price == 490.0`, and a new `scan_results` row.
4. **No new low: `current > baseline` → no notify, baseline unchanged.** Seed
   500.0, scan yields 510.0; assert no all-time-low notify, baseline still 500.0,
   but a `scan_results` row **was** appended (history always recorded).
5. **Boundary: `current == baseline` → no notify, no update.** Seed 500.0, scan
   yields exactly 500.0 — pins the strict `<` (FRD §7).
6. **Multi-condition independence.** Card with `["NM","SP"]`: NM hits a new low,
   SP does not — only NM notifies; both get `scan_results` rows; SP baseline
   unchanged.
7. **No matching condition.** Configured condition absent from the page → no
   `scan_results`/baseline rows for it, no notify, no `scan_errors` row; the
   present condition still processed normally.

**Error-isolation tests:**

8. **`FetchError` → logged to `scan_errors('fetch')`, cycle continues.** Two-card
   list, first card's page 500s twice (exhausts retry); assert a
   `scan_errors` row with `error_type='fetch'`, the second card still scanned,
   and `wait_between_cards` (a recorded sleep) happened between them.
9. **Parser failure → `scan_errors('parse')`, continue.** Feed malformed HTML
   (no `cards_stock`) for one card; assert the `parse` row and that the next card
   is still processed.
10. **Sprite decode failure → `scan_errors('sprite_decode')` + Discord alert,
    listing skipped, card not aborted.** Use a precoCss fixture whose sprite/style
    is broken so the decoder fails, but include at least one decodable/`precoFinal`
    listing; assert the `sprite_decode` row, that
    `notify_sprite_decode_failure` fired once, and that the surviving listing
    still produced a lowest price + baseline.
11. **`CycleStop` (403/429) stops the whole cycle.** Three-card list, second card
    returns 429; assert: first card fully processed, a `scan_errors` row
    `http_429`, the **third card never fetched** (handler not called for it),
    `ScanSummary.stopped_early is True`, and no between-card delay after the stop.
12. **403 inside a sprite fetch also stops the cycle.** precoCss page returns 200
    but the sprite URL returns 403 → `CycleStop` propagates out of
    `parse_listings` and `run` stops. Proves the parser doesn't swallow it.

**Loop / delay tests:**

13. **Between-card delay.** N cards → `wait_between_cards` invoked exactly N−1
    times (recorded via the injected `sleep`); none after the last card.
14. **`ScanSummary` counters** are correct over a mixed run (some new lows, one
    fetch failure): `cards_scanned`, `cards_failed`, `new_lows`, `stopped_early`.
15. **Timestamps.** With an injected `clock` returning a fixed string, all of a
    card's `scan_results` / `price_baselines` rows carry that exact value.

All hermetic — no real network, no real sleeps (`sleep=list.append`), DB in
`:memory:`.

---

## Wiring notes (future `app.py` — leave the seam, do NOT build here)

Illustrative contract only; **out of scope** for this issue:

```python
config = load_config()                      # §3; invalid config aborts startup
setup_logging(config.app)                   # §16 rotating handler — separate issue
with acquire_lock("watcher.lock"):          # §15 PID lock — separate issue
    conn = storage.connect(config.app.database_path)
    storage.init_db(conn)
    with HttpFetcher.from_app_config(config.app) as fetcher, \
         DiscordNotifier.from_app_config(config.app) as notifier:
        scanner = Scanner(
            fetcher=fetcher, notifier=notifier, conn=conn,
            send_initial_baseline=config.app.send_initial_baseline_notification,
        )
        summary = scanner.run(config.cards)
    log.info("Scan complete: %s", summary)
```

The scanner takes everything pre-built; it never reads env, opens files, or
manages the lock. That keeps it a pure, fully-testable orchestrator.

---

## Dependencies

None new. `httpx` (+ `MockTransport`), `sqlite3`, `pytest` already present. No new
production deps — the scanner is glue over modules that already exist.

---

## Acceptance (from issue #7)

- ✅ **Real parser/storage with mocked HTTP + Discord** — tests 1–15 use the real
  `LigaPokemonParser` and real `sqlite3`, with `MockTransport` HTTP and a Discord
  spy/MockTransport.
- ✅ **Notification fires only when `current < baseline`** — tests 3 (fires), 4
  (above → silent), 5 (equal → silent), 6 (per-condition).
- ✅ **Baseline updates correctly** — created on first sight (1/2), lowered on a
  new low (3), untouched otherwise (4/5).
- ✅ Ties #2–#6 together via the §6 10-step workflow, with the §12 error table and
  §17 stop-cycle realised (tests 8–12).

---

## Out of scope (later issues)

- **`app.py` entrypoint** — config load, lock file (§15), rotating log setup
  (§16), dependency construction, single-run guard. The "wiring notes" block is
  illustration, not delivery.
- **Cron scheduling / `CHECK_INTERVAL_MINUTES`** (§4, §18) — deployment concern;
  each run is one pass.
- **New marketplaces** (MypCards) — added later by appending one `ParserFactory`;
  the registry seam is built here, the parser is not.
- **Parser registry extraction to `parsers/registry.py`** — kept inline in the
  scanner for now; extract when the second marketplace lands.

## Suggested PR

Single PR — `services/scanner.py` + `tests/test_scanner.py`. `pytest` green, no
network, no real sleeps. Commit references `closes #7`. No changes to existing
modules (the scanner only *consumes* them), so all current tests stay green.
