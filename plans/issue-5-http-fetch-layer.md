# Plan: Issue #5 — HTTP fetch layer (`services/fetcher.py`) · FRD §4, §12, §13, §17

The shared, polite HTTP layer every network call flows through. It is the one
place that holds the `httpx` client, sets the `User-Agent` + timeout, applies the
retry policy, enforces the inter-card and intra-card delays, and converts a
`403`/`429` into the **stop-the-cycle** signal. The parser (issue #4) and the
future scanner consume it; neither should ever call `httpx` directly.

Source of truth: `FRD.md` §4 (delays + sprite memory policy), §12 (error table:
403/429 → stop cycle; timeout/network → log & continue), §13 (retry: max 2
attempts, 5s apart), §17 (anti-abuse: one request at a time, respect delays, use
User-Agent, no proxy/CAPTCHA/login).

**Depends on:** #1 (config — merged; `AppConfig` already exposes `user_agent`,
`http_timeout_seconds`, `request_delay_seconds`, `sprite_request_delay_seconds`).

Match existing conventions seen across the repo: `from __future__ import
annotations`, FRD section refs in docstrings, typed signatures, dependency
injection for everything that touches the outside world (clock, network) so tests
stay hermetic and offline — mirror how `LigaPokemonParser` injects
`sprite_fetcher`/`on_sprite_error`.

---

## The seam this must satisfy (already in the codebase)

`parsers/ligapokemon_parser.py:19` already declares the contract this layer fills:

```python
SpriteFetcher = Callable[[str], bytes]
```

and at `ligapokemon_parser.py:95` calls `self._sprite_fetcher(style.sprite_url)`
to get sprite bytes, with the documented expectation (lines 88–92) that **a fetch
HTTP 403/429 is NOT a `SpriteDecodeError` — it must propagate so the scanner stops
the cycle**. So the fetcher's `get_sprite(url) -> bytes` is exactly the
`SpriteFetcher` the parser is injected with, and its 403/429 exception must be
distinct from `SpriteDecodeError` and must propagate through `parse_listings`
untouched (issue #4 already verified the parser does not catch it).

The `httpx` client is reused across the page request and the sprite request so
the sprite is fetched on the **same session/headers** as the page — FRD §10 step 5
("same session/headers") and §4.

---

## New file: `services/fetcher.py`

### Exceptions

```python
class FetchError(Exception):
    """A transient fetch (timeout/network/retryable status) that exhausted the
    retry budget. Per-card: the scanner logs it and continues (FRD §12, §13)."""

class CycleStop(Exception):
    """HTTP 403 or 429 from the source. The whole scan cycle must stop
    immediately — no retry, no proxy rotation, no bypass (FRD §12, §17).
    Carries the status code and URL for logging."""
    def __init__(self, status_code: int, url: str) -> None: ...
```

Two distinct exceptions because they have **opposite blast radius**: `FetchError`
is caught per-card and the loop continues; `CycleStop` is *not* caught by the
parser and aborts the entire cycle. Keeping them separate is what lets the parser
swallow decode failures while letting 403/429 escape (the issue #4 invariant).

### Module constants (FRD §13 — fixed policy, not env-configurable)

```python
MAX_ATTEMPTS = 2          # total attempts, FRD §13
RETRY_DELAY_SECONDS = 5   # wait between attempts, FRD §13
STOP_STATUSES = frozenset({403, 429})  # FRD §12, §17
```

There is **no** `.env` knob for these in FRD §3 — keep them module constants. (Do
not invent new env vars.)

### Class `HttpFetcher`

```python
class HttpFetcher:
    def __init__(
        self,
        *,
        user_agent: str,
        timeout_seconds: float,
        request_delay_seconds: float,
        sprite_request_delay_seconds: float,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None: ...

    @classmethod
    def from_app_config(cls, app: AppConfig, *, client=None, sleep=time.sleep) -> "HttpFetcher":
        """Build a fetcher straight from the validated AppConfig (FRD §3)."""

    def get_page(self, url: str) -> str:
        """GET a card page → response text. Retry transient failures (FRD §13);
        raise CycleStop on 403/429 (FRD §12, §17); raise FetchError when the
        retry budget is exhausted. No delay is applied before a page fetch — the
        inter-card delay is the scanner's call (see wait_between_cards)."""

    def get_sprite(self, url: str) -> bytes:
        """Sleep SPRITE_REQUEST_DELAY_SECONDS, then GET the digit sprite →
        raw bytes (held in memory; the caller/parser decodes via io.BytesIO and
        never writes to disk — FRD §4). Same retry + 403/429 rules as get_page.
        This is the Callable[[str], bytes] injected as LigaPokemonParser's
        sprite_fetcher."""

    def wait_between_cards(self) -> None:
        """Sleep REQUEST_DELAY_SECONDS — the inter-card delay (FRD §4). The
        scanner calls this between cards; it is a method here so all sleeping
        goes through the one injected `sleep` and stays uniformly testable."""

    def close(self) -> None: ...
    def __enter__(self) -> "HttpFetcher": ...
    def __exit__(self, *exc) -> None: ...   # closes the client
```

#### Key design decisions (call these out, don't silently vary)

- **One reused `httpx.Client`.** Constructed once with
  `headers={"User-Agent": user_agent}` and
  `timeout=httpx.Timeout(timeout_seconds)`. Reused for every request so page +
  sprite share a session (FRD §10 step 5). When `client` is injected (tests pass
  `httpx.Client(transport=httpx.MockTransport(handler))`), use it as-is but still
  apply the UA header per-request if the injected client lacks it — simplest: set
  the UA header on each `client.get(url, headers=...)` call so behaviour is
  identical whether or not the client carries default headers. (Pick one and be
  consistent; setting per-request UA is the most test-friendly.)
- **Injected `sleep`.** Every delay (retry backoff, sprite delay, inter-card
  delay) goes through the single `sleep` callable, default `time.sleep`. Tests
  pass a spy and assert *which* delays fired and with *what* durations — this is
  the issue's "assert delay invocations" acceptance. Never call `time.sleep`
  directly.
- **Private `_request(url, *, delay_before=0.0) -> httpx.Response`** shared by
  `get_page`/`get_sprite`:
  1. If `delay_before`, `self._sleep(delay_before)` first (sprite delay path).
  2. Attempt loop, up to `MAX_ATTEMPTS`:
     - `resp = self._client.get(url, headers={"User-Agent": ...})`.
     - If `resp.status_code in STOP_STATUSES` → `raise CycleStop(status, url)`
       **immediately, no retry** (politeness — FRD §12/§17).
     - If `resp.status_code` is otherwise a non-success (`resp.is_error`) →
       treat as a transient failure (see retry rule below).
     - Else return `resp`.
     - Transient failures (`httpx.TimeoutException`, `httpx.TransportError`, or a
       retryable error status): if attempts remain, `self._sleep(RETRY_DELAY_SECONDS)`
       and retry; otherwise `raise FetchError(...)` chaining the last cause.
  3. Log each retry at WARNING and the final failure at ERROR via
     `logging.getLogger(__name__)` (FRD §16 logs retries + errors). Do **not**
     build logging handlers here — that's the logging-setup issue; just use the
     stdlib logger.
- **Retry classification.** `403`/`429` → `CycleStop` (never retried). Timeouts
  and transport/network errors → retried. Other error statuses (e.g. 5xx, 404):
  treat as transient/retryable then `FetchError` — simplest uniform rule, and a
  one-off 5xx blip retrying once is harmless and polite. *(If the reviewer/owner
  prefers 4xx-other to fail fast without retry, that's a small tweak — flag it,
  don't block on it.)*
- **`get_page` returns `response.text`** (httpx charset handling);
  **`get_sprite` returns `response.content`** (raw bytes; the parser opens it via
  `io.BytesIO`, FRD §4 — nothing here writes to disk).

---

## Wiring notes (for the future scanner — leave the seam, don't build it here)

The scanner (separate issue) will:
```python
fetcher = HttpFetcher.from_app_config(config.app)
parser  = LigaPokemonParser(sprite_fetcher=fetcher.get_sprite, on_sprite_error=...)
for card in config.cards:
    try:
        html = fetcher.get_page(card.url)
        listings = parser.parse_listings(html)   # may call fetcher.get_sprite internally
        ...
    except FetchError:
        log_and_continue()        # FRD §12
    # CycleStop is NOT caught here in the per-card try — it breaks the whole loop
    fetcher.wait_between_cards()   # FRD §4
```
This plan delivers only the `HttpFetcher`; the loop above is illustration of the
contract, not in scope.

---

## Tests: `tests/test_fetcher.py` (offline, `httpx.MockTransport`, spy clock)

All hermetic — no real network, no real sleeping. Build the fetcher with
`client=httpx.Client(transport=httpx.MockTransport(handler))` and a spy `sleep`
that records its call durations into a list.

Helper: a programmable handler that pops from a pre-seeded queue of either
`httpx.Response(...)` or an exception to raise (to simulate timeouts/network
errors), and records each call so the test can assert attempt counts.

Cases:

1. **Happy page fetch** — handler returns `200` with HTML body →
   `get_page(url) == "<html>…"`; handler called **once**; `sleep` never called;
   the request carried the configured `User-Agent` header and the client timeout
   matches `http_timeout_seconds`.
2. **403 → CycleStop, no retry** — handler returns `403`; `get_page` raises
   `CycleStop` with `status_code == 403` and the URL; handler called **once**;
   `sleep` **not** called (no backoff before stopping).
3. **429 → CycleStop, no retry** — same as above for `429`.
4. **Transient fail twice → FetchError** — handler raises
   `httpx.ConnectTimeout`/`httpx.ConnectError` on both attempts; `get_page`
   raises `FetchError`; handler called **twice** (`MAX_ATTEMPTS`); `sleep` called
   **once** with `5` (`RETRY_DELAY_SECONDS`).
5. **Transient then success → recovers** — attempt 1 raises timeout, attempt 2
   returns `200`; `get_page` returns the body; handler called twice; exactly one
   `5`s backoff sleep.
6. **`get_sprite` applies the sprite delay first** — handler returns `200` with
   JPEG-ish bytes; `get_sprite(url) == bytes`; `sleep` called with
   `sprite_request_delay_seconds` **before** the request (assert ordering: the
   sprite delay is the first recorded sleep, the request happened after).
7. **`get_sprite` 403/429 propagates `CycleStop`** — the seam issue #4 relies on:
   `get_sprite` raises `CycleStop`, *not* `FetchError`, and not a
   `SpriteDecodeError`. (Optionally also assert, via a tiny integration check,
   that `LigaPokemonParser(sprite_fetcher=fetcher.get_sprite)` lets the
   `CycleStop` escape `parse_listings` — proves the contract end-to-end, but the
   unit assertion on `get_sprite` is the core requirement.)
8. **`wait_between_cards` sleeps the inter-card delay** — calling it records a
   single `sleep(request_delay_seconds)`; no HTTP performed.
9. **`from_app_config`** maps `AppConfig` fields onto the fetcher (UA, timeout,
   both delays) — construct a minimal `AppConfig` and assert a page fetch uses
   the expected UA / a `wait_between_cards` sleeps the expected duration.
10. **Client lifecycle** — `with HttpFetcher(...) as f:` closes the underlying
    client on exit (assert the injected client's `is_closed`, or that `close()`
    is idempotent).

---

## Dependencies

None new. `httpx>=0.27` is already in `requirements.txt`/`pyproject.toml` and
ships `httpx.MockTransport`. No proxy libs, no retry libs — the policy is small
and lives in this module by hand (FRD §13/§17 keep it deliberately minimal).

---

## Acceptance (from issue #5)

- ✅ `httpx` GET with `User-Agent` and `HTTP_TIMEOUT_SECONDS` (cases 1, 9).
- ✅ Retry max 2 attempts, 5s apart (cases 4, 5 — assert attempt count **and**
  the single 5s backoff via the spy clock; FRD §13).
- ✅ 403/429 raise a **stop-cycle** signal (`CycleStop`), not retried, distinct
  from `FetchError`/`SpriteDecodeError` (cases 2, 3, 7; FRD §12, §17).
- ✅ Inter-card delay (`wait_between_cards`) and intra-card sprite delay
  (`get_sprite`) applied and assertable via the injected `sleep` (cases 6, 8;
  FRD §4).
- ✅ Mock-transport tests asserting retry count + delay invocations + stop-cycle
  raise (the issue's exact acceptance wording).

---

## Out of scope (leave the seams; later issues)

- **Scanner loop** that composes `get_page` → `parse_listings` → `lowest_prices`,
  catches `FetchError` per card, lets `CycleStop` abort the cycle, and calls
  `wait_between_cards` between cards (its own issue — only sketched above).
- **Logging setup** — the rotating file handler / log format (FRD §16). This
  module only emits via `logging.getLogger(__name__)`; it configures nothing.
- **`scan_errors` / Discord** for fetch failures — storage/notifier concerns;
  the fetcher just raises typed exceptions for the scanner to route.
- **Lock file, `app.py` wiring, baseline comparison.**

## Suggested PR

Single PR — `services/fetcher.py` + `tests/test_fetcher.py`. `pytest` green, no
network (all via `httpx.MockTransport` + a spy `sleep`). Commit references
`closes #5`. No production wiring changes (the scanner adopts it in a later
issue), so existing tests stay untouched and green.
