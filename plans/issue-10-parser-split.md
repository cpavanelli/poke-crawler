# Plan: Issue #10 — Split parser: extraction (`parse_listings`) vs lowest-price reduction

Separate the parser's two jobs. Today `LigaPokemonParser.parse(html, card)`
conflates **extraction** (HTML → listings) with **reduction** (lowest price per
configured condition). Split them so:

- the parser only **extracts** every listing (marketplace-specific), and
- the **reduction** becomes a small pure function (marketplace-agnostic) the
  scanner composes — and which a listing-level inspection tool (#11) can skip.

Source of truth: `FRD.md` §11 (already updated this iteration: `parse_listings`
+ `lowest_prices`), §5 (price = listing price only), §6 (workflow). Match
existing conventions: frozen `@dataclass(slots=True)`, `from __future__ import
annotations`, FRD section refs in docstrings, hermetic offline tests.

**Depends on:** #4 (merged — sprite decoder). Pure, offline, no network.

---

## 1. New model: `models/listing.py`

```python
@dataclass(slots=True, frozen=True)
class Listing:
    """One marketplace listing: a condition and its listing price (FRD §5).

    Distinct from PriceResult, which is the *reduced* lowest price per condition.
    Price is the listing price only; shipping is never included (FRD §5).
    """
    condition: str   # acronym, e.g. "NM"
    price: float
```

Mirror `models/price_result.py` exactly (style, docstring shape).

---

## 2. Parser contract: `parsers/base.py`

Replace the `parse(html, card)` abstract method with extraction-only:

```python
class MarketplaceParser(ABC):
    @abstractmethod
    def can_handle(self, url: str) -> bool: ...

    @abstractmethod
    def parse_listings(self, html: str) -> list[Listing]:
        """Return every listing on the page (all conditions, unfiltered)."""
```

- Import `Listing` (not `PriceResult`); drop the `Card` import (the extraction
  contract no longer knows about configured cards).

---

## 3. `parsers/ligapokemon_parser.py` — `parse(html, card)` → `parse_listings(html)`

Rename and refactor the existing method. Mechanics stay; two behavior changes:

1. **No config filter.** Today it skips listings whose condition isn't in
   `card.conditions` (`ligapokemon_parser.py:77-78`). Remove that — keep only the
   "skip when the condition can't be resolved" guard. Extraction returns **all**
   conditions.
2. **Collect, don't reduce.** Replace the `lowest_by_condition` dict + final
   `PriceResult` comprehension with a flat `list[Listing]`, appending
   `Listing(condition=condition, price=price)` for each priced listing. Return in
   page order (callers sort/reduce as needed).

Everything else is unchanged and reused as-is: `_extract_js_literal`,
`_build_condition_map`, `_resolve_condition`, `_parse_preco_final`,
`_extract_inline_style`, and the whole precoCss path — the one-time sprite
setup (`parse_style_css` + fetch + `SpriteDecoder`), per-listing `decode`,
HTTP-403/429 propagation, and the **one-warning-per-page** dedup
(`report_sprite_error` / `sprite_error_reported`). The sprite still opens once.

### Sprite-error callback is now card-agnostic

`parse_listings(html)` no longer receives a `Card`, so the error callback can't
carry one. Simplify:

- Drop the `SpriteDecodeContext` dataclass and the `Card`-carrying context.
- `on_sprite_error: Callable[[str], None] | None` — receives just the decode
  **error message**. The caller (scanner #7 / tool #11) attaches card/url context
  in its own closure, where it actually knows them.
- `_emit_sprite_error` collapses to: `if self._on_sprite_error: self._on_sprite_error(message)`.
- `report_sprite_error(str(exc))` at the two call sites.

This removes the parser's last dependency on `Card`; drop that import too.

> Note for the scanner issue (#7): since the parser is card-agnostic and may be
> reused across cards, the scanner supplies a per-card `on_sprite_error` closure
> (capturing the current card) and composes `parse_listings` → `lowest_prices`.
> Recording `scan_errors(sprite_decode)` + the Discord alert (FRD §7, §10, §12)
> still happens in that closure — unchanged in spirit, just the message crosses
> the boundary instead of a context object.

---

## 4. Reduction: `services/pricing.py`

New pure, marketplace-agnostic function:

```python
def lowest_prices(
    listings: Iterable[Listing], conditions: Sequence[str]
) -> list[PriceResult]:
    """Lowest listing price per requested condition (FRD §5, §11).

    Keeps only listings whose condition is in `conditions`, takes the minimum
    price per condition, and returns one PriceResult per condition that had at
    least one listing — ordered to follow `conditions`.
    """
```

- Filter to `set(conditions)`, fold to a `min` per condition, emit in
  `dict.fromkeys(conditions)` order (preserves the old `card.conditions` output
  ordering). A condition with no listings is simply absent (scanner treats
  "no matching condition" as log-and-continue, FRD §12).
- No `Card` dependency — takes a plain `conditions` sequence (scanner passes
  `card.conditions`).

---

## 5. Tests

### `tests/test_listing.py` (new, trivial)
- Construct a `Listing`, assert fields, frozen/immutable.

### `tests/test_pricing.py` (new)
- `lowest_prices` picks the minimum per condition; multiple listings → lowest
  wins.
- Conditions not requested are filtered out.
- Output order follows the `conditions` argument; a requested condition with no
  listings is absent.
- Empty input → `[]`.

### `tests/test_ligapokemon_parser.py` (rework to the new surface)
- **`parse_listings` returns everything, unfiltered.** Mega Gengar fixture
  (`mega_gengar_284.html`, has M/NM/SP) → 25 priced `Listing`s
  (1 M + 17 NM + 7 SP; the 1 listing lacking both `precoFinal`/`precoCss` is
  skipped). Assert as a `Counter` of conditions and the known lowest per
  condition (M 2687.04, NM 2670.00, SP 2350.00) so order doesn't matter.
- **No config filtering at extraction:** M is present in `parse_listings` output
  (it was filtered out under the old `parse`).
- **precoCss decode still in extraction.** Greninja fixture
  (`greninja_116_precocss.html`, all NM, with `sprite_fetcher` wired to the
  committed sprite) → the decoded `Listing(condition="NM", price=843.00)` is
  present; sprite opened once (keep the existing `_open_sprite` count-once
  assertion, retargeted to `parse_listings`).
- **Compose extraction + reduction** (the old behavior, now in two steps):
  `lowest_prices(parser.parse_listings(gengar_html), ("NM","SP"))` ==
  `[NM 2670.00, SP 2350.00]`; with Greninja + fetcher and `("NM",)` == `[NM 843.00]`,
  and `< 934.15` (precoCss beats the cheapest precoFinal).
- **Sprite-error callback simplified:** update the isolation/dedup test and the
  403/429 propagation test to the `Callable[[str], None]` signature (assert the
  message string; one call across multiple failing precoCss listings; httpx
  403/429 still propagates out of `parse_listings`). Remove `SpriteDecodeContext`
  references.
- `can_handle` tests unchanged.

All tests stay offline (fixtures + fake `sprite_fetcher`).

---

## 6. FRD

Already updated this iteration — no further FRD edits needed:
- §11 — `parse_listings` + `lowest_prices` with example outputs.
- §6 — workflow steps mapped to the two methods.
- §19 — `models/listing.py`, `services/pricing.py` in the layout.

---

## Out of scope (later issues — don't build here)
- The shared HTTP fetcher and the `tools/list_prices.py` CLI — **#11** (which
  depends on this and on **#5** for fetching).
- Scanner wiring of `parse_listings` → `lowest_prices`, `scan_errors`, Discord —
  **#7**.
- `parsers/mypcards.py`.

## Acceptance (from issue #10)
- ✅ `parse_listings` on the Greninja fixture returns every listing (incl. the
  precoCss-decoded 843.00) as `Listing` items.
- ✅ `lowest_prices(listings, ("NM","SP"))` reproduces the prior behavior
  (filtered + reduced).
- ✅ No network in tests.

## Suggested PR
One PR — `models/listing.py`, `parsers/base.py`, `parsers/ligapokemon_parser.py`,
`services/pricing.py`, and the test changes. `pytest` green, no network. Commit
references `closes #10`.
