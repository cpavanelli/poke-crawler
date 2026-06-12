# Plan: Issue #6 — Notifier (Discord) (`services/notifier.py`) · FRD §7, §12

The Discord output layer. It owns three responsibilities and nothing else:

1. **Format** the three message types defined in FRD §7 — new all-time-low,
   initial baseline, sprite-decode alert — using the exact templates.
2. **Format prices** as Brazilian currency (`R$1.250,00`) — the templates show
   `.` thousands separators and `,` decimal separator.
3. **Send** a message to the configured Discord webhook, and on **any** webhook
   failure **log and continue** — never raise, never abort the scan (FRD §12).

Source of truth: `FRD.md` §7 (the three templates, verbatim) and §12 (the error
table: *Discord failure → log and continue*).

**Depends on:** #1 (config — merged). `AppConfig.discord_webhook_url` and
`AppConfig.send_initial_baseline_notification` already exist
(`services/config.py:34`, `:42`). This issue consumes the webhook URL; it does
**not** read the `send_initial_baseline_notification` flag itself (see "Out of
scope" — the *scanner* decides whether to call the initial-baseline method).

Match the conventions already established across the repo (look at
`services/fetcher.py` and `tools/list_prices.py` before writing):
`from __future__ import annotations`; FRD section refs in docstrings; typed
signatures; keyword-only arguments for anything with several fields; dependency
injection of the `httpx.Client` so tests run **offline** via
`httpx.MockTransport` (mirror `tests/test_fetcher.py`); module logger via
`logging.getLogger(__name__)` and **no** logging-handler setup here.

---

## The three templates (copy these character-for-character from FRD §7)

Watch the spacing and the literal strings — tests will assert exact equality.

**New all-time-low** (§7):
```
[card name] - [condition] - [price found] - Previous lowest: [latest lowest price] - [url]
```
Example:
```
Mega Charizard X - NM - R$1.250,00 - Previous lowest: R$1.350,00 - https://...
```

**Initial baseline** (§7, sent by the scanner only when
`SEND_INITIAL_BASELINE_NOTIFICATION=true`):
```
Mega Gengar - NM - R$500,00 - Initial baseline - https://...
```

**Sprite-decode alert** (§7, §10) — note the leading warning emoji `⚠️`
(`⚠️`), and that it carries **no price** (the listing was skipped):
```
⚠️ Sprite decode failed - [card name] - [url] - listing skipped
```

---

## Brazilian price formatting (`R$1.250,00`)

There is no existing helper (`tools/list_prices.py:38` prints raw `:.2f` for
debugging — do **not** reuse that for Discord). Add a small pure function:

```python
def format_brl(value: float) -> str:
    """Format a price as Brazilian currency, e.g. 1250.0 -> 'R$1.250,00' (FRD §7).

    Thousands separator is '.', decimal separator is ',', always two decimals.
    """
    grouped = f"{value:,.2f}"                       # '1,250.00' (US grouping)
    swapped = grouped.translate(str.maketrans({",": ".", ".": ","}))
    return f"R${swapped}"
```

Verify against the FRD examples in tests:
- `1250.0   -> "R$1.250,00"`
- `1350.0   -> "R$1.350,00"`
- `500.0    -> "R$500,00"`
- `843.0    -> "R$843,00"`
- `1234567.5 -> "R$1.234.567,50"` (multi-group sanity check)
- `0.0      -> "R$0,00"`

No `R$` plus a space, no trailing currency code — match the templates exactly
(`R$1.250,00`, no space after `R$`).

---

## New file: `services/notifier.py`

### Pure message builders (module-level functions)

Keep formatting **separate** from sending so the templates can be unit-tested
with zero HTTP. Each returns the finished `content` string:

```python
def format_all_time_low(
    *, card_name: str, condition: str, price: float, previous_lowest: float, url: str
) -> str: ...

def format_initial_baseline(
    *, card_name: str, condition: str, price: float, url: str
) -> str: ...

def format_sprite_decode_alert(*, card_name: str, url: str) -> str: ...
```

- `format_all_time_low` →
  `f"{card_name} - {condition} - {format_brl(price)} - Previous lowest: {format_brl(previous_lowest)} - {url}"`
- `format_initial_baseline` →
  `f"{card_name} - {condition} - {format_brl(price)} - Initial baseline - {url}"`
- `format_sprite_decode_alert` →
  `f"⚠️ Sprite decode failed - {card_name} - {url} - listing skipped"`

### Class `DiscordNotifier`

```python
class DiscordNotifier:
    def __init__(
        self,
        webhook_url: str,
        *,
        client: httpx.Client | None = None,
        timeout_seconds: float = 10.0,
    ) -> None: ...

    @classmethod
    def from_app_config(cls, app: AppConfig, *, client=None) -> "DiscordNotifier":
        """Build straight from the validated AppConfig (uses discord_webhook_url)."""

    def notify_all_time_low(
        self, *, card_name: str, condition: str,
        price: float, previous_lowest: float, url: str,
    ) -> bool: ...

    def notify_initial_baseline(
        self, *, card_name: str, condition: str, price: float, url: str,
    ) -> bool: ...

    def notify_sprite_decode_failure(self, *, card_name: str, url: str) -> bool: ...

    def close(self) -> None: ...
    def __enter__(self) -> "DiscordNotifier": ...
    def __exit__(self, *exc) -> None: ...   # closes the client
```

Each `notify_*` method just builds its message via the matching
`format_*` function and hands it to the private `_send`. They return whatever
`_send` returns (`True` = delivered, `False` = failed-and-logged) so the scanner
can log the outcome (FRD §16 "Notification results") but is never forced to.

#### `_send(content: str) -> bool` — the one network method

```python
def _send(self, content: str) -> bool:
    """POST {"content": content} to the webhook. Any failure is logged and
    swallowed — Discord failure must never abort the scan (FRD §12)."""
```

Behaviour:
1. `resp = self._client.post(self._webhook_url, json={"content": content})`.
2. On a 2xx (Discord returns **204 No Content** on success) → log at INFO/DEBUG
   ("notification sent") and return `True`.
3. On a non-2xx status → log at WARNING/ERROR with the status code and return
   `False`. **Do not raise.** (Use `resp.is_success`; do not call
   `raise_for_status` — we never want it to propagate.)
4. Wrap the `post` in `try/except httpx.HTTPError` (covers timeouts, transport,
   connection errors); on exception log at ERROR with the exception and return
   `False`. **Do not re-raise.**

This is the whole "Discord failure = log and continue" rule (FRD §12). It is the
behavioural opposite of the fetcher: a `CycleStop`/`FetchError` from the *source*
propagates, but a Discord failure is fully contained here.

#### Key design decisions (state them; don't silently vary)

- **Its own `httpx.Client`, NOT the fetcher's.** Discord is a different host than
  the marketplace source, so the notifier constructs its own client with a
  modest timeout (`timeout_seconds`, default 10s). The fetcher's polite-source
  client (User-Agent, retry, 403/429 stop-cycle) is irrelevant here — do not
  share it.
- **No retry.** FRD §12 says Discord failure → *log and continue*, full stop. The
  §13 two-attempt retry policy is for the source fetch layer, not Discord. Keep
  `_send` single-shot. *(If the owner later wants one retry on a Discord 5xx,
  that's a small additive tweak — flag it, don't build it now.)*
- **The notifier does not gate on `SEND_INITIAL_BASELINE_NOTIFICATION`.** It
  exposes `notify_initial_baseline` unconditionally; the scanner checks the flag
  and decides whether to call it. Keeps the notifier a dumb, fully-testable
  sender.
- **The notifier does not touch `scan_errors` or the DB.** Recording the
  sprite-decode failure in `scan_errors` (FRD §10 step 3) is the scanner/storage
  job; the notifier only sends the §7 alert. Two separate concerns wired by the
  scanner.
- **Injected client per test.** Construct with
  `client=httpx.Client(transport=httpx.MockTransport(handler))` (offline). When
  `client is None`, build a real `httpx.Client(timeout=httpx.Timeout(timeout_seconds))`.

---

## Wiring notes (for the future scanner — leave the seam, don't build it here)

Illustrative only; not in scope:

```python
notifier = DiscordNotifier.from_app_config(config.app)

# new all-time-low found for a condition:
notifier.notify_all_time_low(
    card_name=card.name, condition=result.condition,
    price=result.lowest_price, previous_lowest=baseline.lowest_price, url=card.url,
)

# first time we ever see this card+condition, and the env flag is on:
if config.app.send_initial_baseline_notification:
    notifier.notify_initial_baseline(
        card_name=card.name, condition=result.condition,
        price=result.lowest_price, url=card.url,
    )

# sprite decode failed for a listing (also recorded in scan_errors separately):
notifier.notify_sprite_decode_failure(card_name=card.name, url=card.url)
```

The parser's `on_sprite_error` callback (`parsers/ligapokemon_parser.py:30`,
type `Callable[[str], None]`) is the scanner's hook to trigger both the
`scan_errors` insert and `notify_sprite_decode_failure`; the notifier itself is
not passed into the parser.

---

## Tests: `tests/test_notifier.py` (offline, `httpx.MockTransport`)

All hermetic — no real network. Mirror `tests/test_fetcher.py`'s `QueueHandler`
+ `httpx.Client(transport=httpx.MockTransport(handler))` pattern. The handler
records each request so tests can assert the posted JSON body.

**Formatting (pure, no HTTP):**

1. `format_brl` matches every example in the table above (parametrize).
2. `format_all_time_low` produces **exactly**
   `"Mega Charizard X - NM - R$1.250,00 - Previous lowest: R$1.350,00 - https://x"`
   for `price=1250.0, previous_lowest=1350.0` — assert full-string equality
   against the FRD §7 example.
3. `format_initial_baseline` produces **exactly**
   `"Mega Gengar - NM - R$500,00 - Initial baseline - https://x"` (FRD §7 example).
4. `format_sprite_decode_alert` produces **exactly**
   `"⚠️ Sprite decode failed - <card> - <url> - listing skipped"`,
   including the leading `⚠️`.

**Sending (MockTransport):**

5. **Happy send** — handler returns `204`; `notify_all_time_low(...)` returns
   `True`; handler called **once**; the posted request went to the webhook URL,
   used POST, and its JSON body is `{"content": "<the §7 all-time-low string>"}`
   (decode `request.content` / `json.loads`). Proves builder → `_send` wiring.
6. **Initial-baseline send** — `204`; body `content` equals the §7 initial
   baseline string; returns `True`.
7. **Sprite-alert send** — `204`; body `content` equals the §7 sprite alert
   string; returns `True`.
8. **Non-2xx is swallowed** — handler returns `500` (or `400`);
   `notify_all_time_low(...)` returns `False` and **does not raise**; handler
   called once (no retry).
9. **Transport error is swallowed** — handler raises
   `httpx.ConnectError("boom")`; the notify call returns `False` and **does not
   raise** (this is the core "Discord failure = log and continue" acceptance,
   FRD §12). Optionally assert a WARNING/ERROR log via `caplog`.
10. **No retry** — confirm that on failure (case 8 or 9) the handler is invoked
    exactly once (single-shot, unlike the fetcher).
11. **`from_app_config`** — build a minimal `AppConfig` with a known
    `discord_webhook_url`, inject a mock client, and assert a send posts to that
    URL.
12. **Client lifecycle** — `with DiscordNotifier(...) as n:` closes the injected
    client on exit (assert `client.is_closed`), and `close()` is idempotent.

---

## Dependencies

None new. `httpx` is already in `requirements.txt` / `pyproject.toml` and ships
`httpx.MockTransport`. No Discord SDK — a plain webhook POST is all FRD §7
requires.

---

## Acceptance (from issue #6)

- ✅ Message formatting matches the §7 templates — asserted by full-string
  equality in tests 2–4 (and `format_brl` in test 1).
- ✅ Mocked webhook failure is handled without aborting — tests 8 (non-2xx) and
  9 (transport error) both return `False` and never raise (FRD §12).
- ✅ All three message types implemented: all-time-low, initial baseline,
  sprite-decode alert (tests 5–7).

---

## Out of scope (leave the seams; later issues)

- **Scanner** deciding *when* to notify (baseline comparison, the
  `SEND_INITIAL_BASELINE_NOTIFICATION` gate, calling the notify methods) — its
  own issue. This plan delivers only the notifier; the wiring block above is
  illustration of the contract, not in scope.
- **`scan_errors` insert** for sprite-decode failures (storage concern, already
  in `services/storage.py:insert_scan_error`) — the scanner pairs it with the
  notifier alert; the notifier does not write to the DB.
- **Logging setup** — the rotating file handler / format (FRD §16). This module
  only emits via `logging.getLogger(__name__)`.
- **Rich Discord embeds, retries, rate-limit handling.** FRD §7 specifies plain
  `content` strings; keep it minimal.

## Suggested PR

Single PR — `services/notifier.py` + `tests/test_notifier.py`. `pytest` green,
no network (all via `httpx.MockTransport`). Commit references `closes #6`. No
production wiring changes (the scanner adopts it in a later issue), so existing
tests stay untouched and green.
