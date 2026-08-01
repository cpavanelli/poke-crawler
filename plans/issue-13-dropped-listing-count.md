# Plan: Issue #13 — Report dropped-listing count in sprite-decode warning · FRD §7, §10 (follow-up to #12)

After #12 the `precoCss` decoder is reliable on the current sprite, so decode
failures are now **rare — and that makes them the early-warning signal that the
site rotated its font/sprite.** The problem is the alert understates the breakage:
the parser dedupes the sprite warning to **one per page** and passes only a bare
reason string, so a single `⚠️ sprite decode failed` line looks identical whether
**1 of 18** listings was skipped (one odd listing — ignore) or **18 of 18** (the
decoder is fully broken — fix now). This plan adds a dropped-listing **count** to
that one-per-page alert so the two cases are distinguishable at a glance.

This is purely a **reporting** change. It does not touch decode behaviour, the
held-out zero-misread gate from #12 (`tests/test_digit_bank.py`), or the
per-listing safe-skip semantics (FRD §10).

Source of truth: `FRD.md` §10 (per-listing safe skip), §7 (Discord alert format).

**Depends on:** #12 (merged). Touches the parser's `on_sprite_error` contract and
its two consumers — the `list_prices` CLI (#11) stderr warning and the
scanner→notifier (#7) Discord alert (FRD §7) — plus the tests that assert the
current message text. Does **not** touch the HTTP fetcher (#5) or the decoder
(#4/#12).

---

## Current behaviour (the gap)

`parsers/ligapokemon_parser.py` (the `parse_listings` loop, ~lines 55–111):

- A `nonlocal sprite_error_reported` flag in `report_sprite_error` (line 60) fires
  `_emit_sprite_error` **only on the first failure**, then stays silent for the
  rest of the page. Subsequent skips are invisible.
- The handler type is `SpriteErrorHandler = Callable[[str], None]` (line 20). The
  message is just the `SpriteDecodeError` reason string.

There are **two distinct failure paths**, and they must both be counted:

1. **Per-listing decode failure** (lines 103–107): setup succeeded, but one
   listing's crop won't match. Skips that one listing.
2. **Page-level setup failure** (lines 85–98): the style block won't parse or the
   sprite won't open. `sprite_decoder` stays `None`, so **every** subsequent
   `precoCss` listing is skipped at the `if sprite_decoder is None: continue`
   guard (lines 100–101) **without ever calling `report_sprite_error`.** This is
   the subtle case: a setup failure dooms *all* obfuscated listings on the page,
   so the count must read `N of N`, not `1 of N`.

The three consumers today:

- **Scanner** (`services/scanner.py:214`) — writes the reason to `scan_errors`
  (`error_message`), logs a warning, and calls
  `notify_sprite_decode_failure(card_name=…, url=…)` — note the message/count is
  **not** currently passed to the notifier.
- **`list_prices` CLI** (`tools/list_prices.py:141`) — prints
  `⚠️ sprite decode failed: {message}` to stderr, then notifies.
- **Notifier** (`services/notifier.py:116`) —
  `notify_sprite_decode_failure(*, card_name, url)` builds a **fixed** string via
  `format_sprite_decode_alert`; FRD §7 format is
  `⚠️ Sprite decode failed - [card name] - [url] - listing skipped`.

---

## The fix

### 1. Parser: accumulate counts, emit one alert with the count

Replace the first-failure `sprite_error_reported` flag with simple counters that
cover **both** failure paths, and emit a single alert **after** the loop:

```python
preco_css_total = 0          # obfuscated listings we attempted (fetcher present)
preco_css_failed = 0         # of those, how many produced no price
first_failure_reason: str | None = None
setup_error: str | None = None   # reason captured if page-level setup fails

def record_failure(reason: str) -> None:
    nonlocal preco_css_failed, first_failure_reason
    preco_css_failed += 1
    if first_failure_reason is None:
        first_failure_reason = reason
```

In the loop, for a listing with no `precoFinal`, a string `precoCss`, **and a
configured `sprite_fetcher`** (i.e. one we actually try to decode):

- `preco_css_total += 1`.
- On the first such listing, run setup; on `SpriteDecodeError` store
  `setup_error = str(exc)` (instead of reporting immediately).
- If `sprite_decoder is None` → `record_failure(setup_error or "sprite decoder unavailable")`; `continue`. **This is what makes a setup failure count as `N of N`.**
- Else `try: decode` / `except SpriteDecodeError as exc: record_failure(str(exc)); continue`.

After the loop:

```python
if preco_css_failed:
    self._emit_sprite_error(
        f"{preco_css_failed} of {preco_css_total} obfuscated listings "
        f"could not be decoded: {first_failure_reason}"
    )
```

This is still **exactly one alert per page**, now carrying `failed`/`total` and
the first reason. It also reads cleaner than the inline `nonlocal` flag.

> **Do not count** listings skipped because no `sprite_fetcher` is configured
> (the "decoding disabled" path, line 82) — those are not decode *failures*. Only
> count listings the parser genuinely attempted to decode.

### 2. Contract: keep `Callable[[str], None]`, put the count in the message (recommended)

The issue's own phrasing (`"17 of 18 … could not be decoded: <reason>"`) puts the
count **in the message string**, so the `SpriteErrorHandler = Callable[[str], None]`
contract stays unchanged. Every existing consumer (`scan_errors.error_message`,
the logger, the CLI stderr line) then surfaces the count **for free** — no
signature changes there.

> **Alternative (not recommended for this small change):** widen the contract to
> `Callable[[SpriteDecodeReport], None]` with a `(failed, total, reason)` dataclass
> so each consumer formats independently. Cleaner separation, but it churns every
> consumer signature and all their tests for little gain. Note it in the PR if you
> disagree, but default to the string approach.

### 3. Thread the count into the Discord alert (FRD §7)

This is the one consumer that does **not** get the count for free, because the
scanner calls `notify_sprite_decode_failure(card_name, url)` without the message.
Make the count reach Discord:

- `services/scanner.py:214` — pass the parser's message through, e.g.
  `self._notifier.notify_sprite_decode_failure(card_name=card.name, url=card.url, detail=message)`.
- `services/notifier.py:116` — add the `detail` parameter and include it in
  `format_sprite_decode_alert`.
- **Update FRD §7** — the alert's trailing `listing skipped` becomes the counted
  form, e.g.
  `⚠️ Sprite decode failed - [card name] - [url] - [N of M listings skipped]`.
  Keep it one line; the FRD is the source of truth so the format change must land
  there too.

Decide whether the Discord line carries just the count or count + reason. The
**count** is the at-a-glance triage signal for Discord; the **reason** is most
useful in `scan_errors`/logs. Including both on the Discord line is acceptable but
keep it short — recommend count on Discord, full reason in `scan_errors`/logs/CLI.

### 4. CLI stderr — no code change needed

`tools/list_prices.py:141` already interpolates the message
(`⚠️ sprite decode failed: {message}`), so the counted string flows through
unchanged. (Its `notify_sprite_decode_failure` call at line 144 takes the same
`detail` addition as the scanner if you want the CLI's optional notify to match.)

---

## Tests to update

- **`tests/test_ligapokemon_parser.py:175`** (`…warns_once_for_sprite_decode_failures`):
  two `precoCss` listings fail at the decode step (blank sprite, setup succeeds),
  so the assertion at line 211 changes from
  `["Sprite digit crop did not match a known template"]` to the counted form,
  e.g. `["2 of 2 obfuscated listings could not be decoded: Sprite digit crop did not match a known template"]`.
- **Add a setup-failure test**: a page whose style block / sprite can't initialise
  the decoder, with ≥2 `precoCss` listings, asserts a single `N of N` alert (the
  doomed-all path, lines 100–101) — this is the case the old flag never counted
  correctly. Confirms `total` counts the never-attempted listings too.
- **Add a mixed test** (optional but valuable): one decodable `precoCss` + one
  failing → `1 of 2`, and the decodable price still appears in the returned
  listings (proves the count doesn't suppress valid results).
- **`tests/test_list_prices.py`** — update any assertion on the stderr warning
  text to the counted message.
- **`tests/test_notifier.py`** — if `format_sprite_decode_alert` /
  `notify_sprite_decode_failure` gains `detail`, update its assertion to the new
  FRD §7 string.
- Keep the existing "one sprite fetch per page" assertion
  (`tests/test_ligapokemon_parser.py:210`, `sprite_fetch_calls == 1`) green — the
  counting change must not alter fetch/decode behaviour.
- `tests/test_digit_bank.py` (the #12 held-out zero-misread gate) is untouched.

---

## Acceptance (issue #13)

- ✅ A page with `K` failed `precoCss` listings out of `M` attempted emits
  **exactly one** alert reading `K of M … could not be decoded: <reason>`.
- ✅ A page-level setup failure reports `M of M` (all obfuscated listings), not
  `1 of M`.
- ✅ The count reaches all three sinks: `scan_errors.error_message`, the CLI
  stderr warning, and the Discord alert (FRD §7 updated to match).
- ✅ Decode behaviour, per-listing safe-skip, and one-fetch-per-page are
  unchanged; `pytest` green with no network.

## Out of scope

- Any change to decode logic, the digit bank, or the #12 held-out gate.
- Per-listing alerts (still one per page) or storing per-listing failure rows.
- Widening the handler contract to a struct (noted as the rejected alternative).

## Suggested PR

Single PR: the counter rework in `parsers/ligapokemon_parser.py`, the `detail`
thread-through in `services/scanner.py` + `services/notifier.py`, the FRD §7
format update, and the updated/added parser + notifier + CLI tests. `pytest`
green, no network. Commit references `closes #13`.
