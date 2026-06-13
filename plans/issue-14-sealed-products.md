# Plan: Issue #14 — Monitor sealed products (single lowest price) · FRD §1, §3, §5, §6, §10, §11

Add support for monitoring **sealed products** (e.g. Elite Trainer Boxes) on
LigaPokemon. A sealed product is scanned through the **exact same §6 workflow** as
a card — fetch, parse, reduce, store history + baseline, notify on a new
all-time-low — but it is tracked as a **single lowest price** under one synthetic
condition label rather than per condition.

Source of truth: `FRD.md` §3 (config: omit `conditions` ⇒ sealed), §5 (the sealed
price rule and the `L`-only filter), §6 (the workflow, with the sealed branch at
steps 4–5), §10 (`prod_stock`/`prod_stores` page layout + sealed `dataQuality`
acronyms), §11 (the `lowest_sealed_price` reduction sits beside `lowest_prices`).
The FRD edits for all six sections are already in the working tree.

**Depends on (all merged):** #1 config/models, #3 LigaPokemon parser, #7 scanner,
#10 parser split (`parse_listings` vs reduction), #12 sprite decoder. This issue
touches the seams those left and adds **no new module**.

Match the conventions already established across the repo (read
`parsers/ligapokemon_parser.py`, `services/config.py`, `services/pricing.py`,
`services/scanner.py`, and `models/card.py` before writing): `from __future__
import annotations`; FRD section refs in docstrings; typed signatures;
keyword-only args for multi-field calls; dependency injection so tests run
**offline**; raw `sqlite3` only; no new production deps.

---

## Decisions (locked — do not silently re-decide)

1. **Lowest price = factory-sealed only.** Include `qualid == L` (Lacrado) only;
   exclude every other acronym (`A`, `N`, `NEA`, `NSA`, `U`, `D`). One lowest
   price across that set — no per-condition tracking. (FRD §5)
   - *Rationale, verified against live HTML:* a sealed-product page mixes sealed
     boxes with the occasional used/defective unit, and on the Ascended Heroes
     page the only plain-`precoFinal` listing was a **defective** box (`D`, R$805)
     while the genuinely-sealed listings were sprite-obfuscated. Absolute-lowest
     would track the damaged unit. `L`-only is the strict "factory-sealed price".
2. **Config marker = omit `conditions`.** No new JSON field. An entry with no
   `conditions` array (or empty) is a sealed product; entries with `conditions`
   behave exactly as today. (FRD §3)
3. **Synthetic label `SEALED`.** The single result is stored, compared, and
   notified under condition `"SEALED"`, reusing the existing `(card_id,
   condition)` schema and Discord format. **No DB schema change.** (FRD §5, §7, §8)

---

## Scope boundary (decided)

**In scope:** `services/config.py`, `models/card.py`, `parsers/ligapokemon_parser.py`,
`services/pricing.py`, `services/scanner.py`, plus tests and a `cards.json`
sample. Net change is small because the parser already builds its condition map
**dynamically** from `dataQuality` (`_build_condition_map`), so the sealed acronym
set already flows through with no special-casing.

**Out of scope:** MYP Cards / other marketplaces; per-condition tracking for
sealed products; any change to the sprite decoder itself; the cron cadence (§4).

**Invariant:** card-mode behaviour stays byte-for-byte unchanged. Every existing
test must remain green without modification.

---

## Why this is small (the seams already exist)

| Concern | Already handled by | This issue |
|---|---|---|
| Sealed acronyms (`L`/`D`/…) | `_build_condition_map` reads `dataQuality` dynamically | nothing |
| `precoCss` sprite decode | reused as-is (23/26 listings on the example) | nothing |
| Per-`(card_id, condition)` storage, baseline compare, notify | scanner steps 6–9 | reused under `SEALED` |
| Page variable names | hardcoded `cards_stock`/`cards_stores` | add `prod_stock`/`prod_stores` fallback |
| Mode selection | — | `is_sealed` on `Card`; scanner branch |
| `L`-only filter + single result | — | new `lowest_sealed_price` |

---

## Step 1 — Config + model: sealed mode (`services/config.py`, `models/card.py`) · FRD §3

**`models/card.py`.** Add an explicit mode flag rather than overloading "empty
conditions" implicitly:

```python
@dataclass(slots=True, frozen=True)
class Card:
    name: str
    conditions: tuple[str, ...]   # () for a sealed product
    url: str
    is_sealed: bool = False       # True ⇒ single SEALED price (FRD §3, §5)
```

`card_id` (`SHA256(url)`, §9) is unchanged and mode-independent. A sealed card has
`conditions == ()` **and** `is_sealed is True`; the flag is the authoritative
signal so no caller has to infer mode from an empty tuple.

**`services/config.py` — `_parse_card`.** Make `conditions` optional:

- `conditions` **absent or empty** ⇒ sealed product: build
  `Card(name, conditions=(), url, is_sealed=True)`. Do **not** validate against
  `VALID_CONDITIONS` (those are card acronyms; sealed has none).
- `conditions` **present and non-empty** ⇒ existing path unchanged: must be a
  list of strings, each normalised/upper-cased and in `VALID_CONDITIONS`, else
  `ConfigError`. Resulting `is_sealed=False`.
- `conditions` present but **not a list** (e.g. a string/number) ⇒ still a
  `ConfigError` (malformed), not silently sealed — only *absence* means sealed.

Keep "unknown keys ignored" (§3) intact. Update the `load_cards` / `_parse_card`
docstrings to state the sealed rule.

**Tests (`tests/test_config.py`):**
- missing `conditions` ⇒ `is_sealed True`, `conditions ()`.
- `"conditions": []` ⇒ sealed.
- `"conditions": ["NM"]` ⇒ `is_sealed False`, `("NM",)` (existing validation
  preserved — unknown acronym still raises).
- `"conditions": "NM"` (wrong type) ⇒ `ConfigError` (not treated as sealed).

---

## Step 2 — Parser: `prod_stock` fallback (`parsers/ligapokemon_parser.py`) · FRD §10

Sealed pages (`view=prod/view&pcode=...`) embed the listing/store arrays as
`prod_stock` / `prod_stores` instead of `cards_stock` / `cards_stores`; all fields
(`precoFinal`, `precoCss`, `qualid`, `lj_id`) and `dataQuality` are identical in
shape. Only the variable name differs.

In `parse_listings`, select the stock variable by presence:

```python
cards_stock = _extract_first_js_literal(html, ("prod_stock", "cards_stock"))
```

i.e. a small helper that tries each name via the existing `_extract_js_literal`
machinery and returns the first that matches (raising the current "variable not
found" `ValueError` only if **none** match). Mirror the same fallback for the
stores variable **iff/when** store data is consumed (today `parse_listings` does
not read `cards_stores`, so a single-line stock fallback is sufficient — do not
add dead store-parsing).

Everything else is untouched: `precoFinal`, the `precoCss` sprite path, the
`_build_condition_map(dataQuality)` dynamic acronym map (which already yields
`L`/`N`/`D`/… for sealed pages), and the at-most-one sprite-error dedupe. **The
parser stays mode-agnostic** — it emits every listing with its raw acronym and
knows nothing about the `L`-only filter or `SEALED`.

**Tests (`tests/test_ligapokemon_parser.py`):**
- A `prod_stock` fixture with a mix of `L` `precoCss` listings and a `D`
  `precoFinal` listing ⇒ `parse_listings` returns all of them with correct
  acronyms and decoded prices (route the sprite URL to a fixture sprite in the
  test's fetcher, as existing sprite tests do).
- Existing `cards_stock` fixtures still parse unchanged (regression).

**Fixture:** capture a trimmed real `prod_stock` page under
`tests/fixtures/ligapokemon/` (e.g. `etb_ascended_heroes_prod.html`) plus its
sprite, following the naming of the existing card fixtures. Keep it minimal — a
handful of listings covering `L` precoCss + `L` precoFinal + a `D`/`U` to prove
the filter in step 3.

---

## Step 3 — Pricing: sealed reduction (`services/pricing.py`) · FRD §5, §11

Add a pure, marketplace-agnostic sibling to `lowest_prices`:

```python
SEALED_CONDITIONS: frozenset[str] = frozenset({"L"})   # FRD §5 (factory-sealed only)
SEALED_LABEL = "SEALED"

def lowest_sealed_price(listings: Iterable[Listing]) -> PriceResult | None:
    """Lowest factory-sealed listing price as a single result (FRD §5, §11).

    Keeps only listings whose condition is in SEALED_CONDITIONS, takes the
    minimum listing price, and returns one PriceResult under SEALED_LABEL, or
    None when no sealed listing exists. Shipping is never included (FRD §5).
    """
```

Implementation mirrors `lowest_prices`' min-fold but collapses to one bucket.
Returning `None` (not an empty list) makes the scanner's "no result" branch
explicit.

**Tests (`tests/test_pricing.py`):**
- Only `L` counts; a cheaper `N`/`NEA`/`NSA`/`A`/`U`/`D` listing is ignored.
- Min of multiple `L` listings is chosen; result condition is `"SEALED"`.
- No `L` listing ⇒ `None`.
- `lowest_prices` is untouched (regression).

---

## Step 4 — Scanner: branch on mode (`services/scanner.py`) · FRD §6

`scan_card` reduces listings differently per mode; steps 6–9 (`_record_and_compare`)
are **shared and unchanged**. After `parse_listings` succeeds, replace the single
reduction line with a mode branch:

```python
if card.is_sealed:
    result = lowest_sealed_price(listings)
    results = (result,) if result is not None else ()
else:
    results = tuple(lowest_prices(listings, card.conditions))
```

Then the existing per-result loop (`_record_and_compare`) runs as-is — it already
stores `scan_results`, compares the `(card_id, condition)` baseline, notifies on a
strict new low, and updates the baseline. With `condition="SEALED"` it all works
unchanged.

The "no result" path: for a sealed card with no `L` listing, `results == ()`, so
nothing is stored/notified — log it (`"No sealed listing for %s"`) and continue,
exactly like the card-mode "no matching condition" no-op (FRD §12: log only, no
`scan_errors` row, no DB write, no notify). Adjust the existing `missing`-conditions
log so it doesn't emit a misleading message in sealed mode.

`CycleStop` (403/429) propagation, the sprite-error callback, and per-card
`fetch`/`parse` isolation are all untouched — sealed mode rides the same paths.

**Tests (`tests/test_scanner.py`), mirroring the existing offline harness
(real parser + real `sqlite3` `:memory:`, `MockTransport` HTTP, Discord spy):**
- Sealed card, first sight ⇒ one `price_baselines` row with `condition='SEALED'`,
  one `scan_results` row, no all-time-low notify (flag off).
- Sealed card, then a lower scan ⇒ `notify_all_time_low` fires once with the
  `SEALED` label and correct `previous_lowest`; baseline updated.
- Sealed page where the cheapest listing is `D`/`U` but a pricier `L` exists ⇒
  the **`L`** price is tracked, not the cheaper non-sealed one.
- Sealed page with **no** `L` listing ⇒ no rows, no notify, no `scan_errors`,
  cycle continues to the next card.
- A mixed card list (one card-mode, one sealed) ⇒ both processed; existing
  card-mode assertions unchanged.

---

## Step 5 — Sample config

Add a commented/sample sealed entry documenting the "omit `conditions`" marker,
using one of the example ETB URLs, so an operator sees the contract:

```json
{
  "name": "ETB - Megaevolution Series - Ascended Heroes",
  "url": "https://www.ligapokemon.com.br/?view=prod/view&pcode=135115&prod=..."
}
```

Put it where the repo keeps its config example (a sample/README block, or as an
additional entry in `cards.json` if that file is the documented sample). Do not
commit a live webhook or secrets.

---

## Storage / notifications — unchanged (stated, not re-derived)

- **No schema change.** `SEALED` occupies the existing `condition` column in
  `scan_results` and `price_baselines` (PK `(card_id, condition)`).
- **Discord format unchanged** (§7): `[name] - SEALED - [price] - Previous
  lowest: [...] - [url]`. The `SEALED` label flows through the existing
  `notify_all_time_low` / `notify_initial_baseline` calls with no formatter change.
- **WAL/`synchronous=NORMAL`**, lock file, rotating logs (§8, §15, §16) — all
  orthogonal to this change.

---

## Hard-constraint checklist (CLAUDE.md / FRD)

- ✅ Raw `sqlite3`, no ORM. No schema change.
- ✅ One request at a time, sequential; `REQUEST_DELAY_SECONDS` /
  `SPRITE_REQUEST_DELAY_SECONDS` respected (scanner/fetcher untouched).
- ✅ Sprite never written to disk (reused decoder).
- ✅ Shipping never stored/compared/notified — listing price only.
- ✅ Stop the cycle on 403/429 (`CycleStop` path untouched).
- ✅ `card_id = SHA256(url)`; names display-only.

---

## Dependencies

None new. `httpx` (+ `MockTransport`), `sqlite3`, `pytest`, `BeautifulSoup4`
already present. The change is glue + one fallback + one reduction over existing
modules.

---

## Acceptance (from issue #14)

- ✅ Entry without `conditions` loads as sealed; entries with `conditions`
  unaffected (step 1).
- ✅ `view=prod/view` page parses from `prod_stock`, incl. sprite-decoded
  `precoCss` (step 2).
- ✅ Lowest sealed price counts only `L`; a cheaper `N`/`NEA`/`NSA`/`A`/`U`/`D`
  unit does not become the tracked price (steps 3–4).
- ✅ Result stored/compared/notified under `SEALED`; new all-time-low fires the
  standard Discord notification (step 4).
- ✅ No DB schema change; card-mode behaviour unchanged (invariant; regression
  tests).

---

## Suggested PR

Single PR — steps 1–4 as four commits (step 5 folds into step 1 or a trailing
docs commit), `closes #14`. `pytest` green, no network, no real sleeps. Existing
tests stay green untouched (sealed mode is additive). Reference the FRD sections
already updated in the working tree (§1/§3/§5/§6/§10/§11) in the PR body.
