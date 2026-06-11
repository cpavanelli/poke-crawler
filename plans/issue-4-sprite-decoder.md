# Plan: Issue #4 — Sprite decoder (`precoCss`) · FRD §10, §7, §12, §4

The fragile anti-scrape path. The happy-path parser (issue #3) already **skips**
`precoCss` listings, leaving a clear seam (`# TODO(precoCss): sprite-decode path,
FRD §10` in `parsers/ligapokemon.py`). This issue fills that seam **in isolation**
so a decode failure can never cascade past the single listing it affects.

Source of truth: `FRD.md` §10 (parsing + failure path), §7 (Discord alert), §12
(error policy), §4 (sprite memory policy). Match existing conventions: frozen
`@dataclass(slots=True)`, `from __future__ import annotations`, FRD section refs
in docstrings, hermetic offline tests, "log/skip and continue" for per-item
failures.

**Depends on:** #2 (storage — `scan_errors`), #3 (parser happy path). Both merged.

---

## What the page actually looks like (verified against a live capture)

Captured once with a polite browser-UA `httpx` GET of the issue's example URL
(`Mega Greninja ex (116/086)`, ed CRI) → HTTP 200, ~235 KB. Findings that drive
the design below — **all verified, not assumed**:

- **`precoCss` is a string field inside `cards_stock` entries** (the same array
  issue #3 already `json.loads`-es). A listing has **either** `precoFinal`
  **or** `precoCss`, never both. This capture: 35 `precoFinal` listings + 16
  `precoCss` listings, all condition **NM**.
- **Format of `precoCss`** — semicolon-separated groups, each group a
  space-separated set of CSS class names; `V` is the decimal separator (`,`):
  ```
  fCqMf hYoQx kGmDr;hYoQx fCqMf lGfTa;hYoQx fCqMf oAfYs;V;fCqMf hYoQx hEoJi;hYoQx fCqMf hEoJi
  ```
  Per group, exactly **one** class carries a `background-position` (the digit);
  the others are structural (`hYoQx` = the `background-image` sprite class,
  `fCqMf` = a `width/float/height` sizing class). The digit class is "the class
  in the group that exists in the position map" (FRD §10 step 6).
- **One inline `<style>` block** holds `class → background-position` rules, e.g.
  `.kGmDr{background-position:-360px -44px;}`, plus one rule
  `.hYoQx{background-image:url(//repositorio.sbrauble.com/...​/imgnum/...jpg)}`.
- **Sprite** is a JPEG, `600×84`, i.e. a grid of **8×21px** cells. A digit's
  bitmap is the `8×21` crop at origin `(-bg_x, -bg_y)` (background-position is the
  negative offset). `8×21` matches FRD §10; the rendered `width:7px` from the
  sizing class is just visual clipping — crop the full 8px.
- **Decode verified:** the example above resolves to digits `8 4 3 , 0 0` →
  **`843,00`**.

### The decisive design finding — recognition strategy

The CSS class names, the sprite arrangement, **and the sprite filename are
randomised on every page load** (confirmed: two fetches gave different class
names, different cell positions, and different JPEG bytes). The open question was
whether the *digit glyph bitmaps themselves* are re-rendered (would force OCR/ML)
or merely repositioned.

**Tested directly:** a known `'8'` glyph crop from load #1 was compared pixel-for-
pixel against every digit cell in a freshly fetched load #2 sprite → an **exact
`0.00` pixel-difference match**, with the next-closest digit at `6.30`. The glyph
bitmaps are **pixel-stable across loads**; only their *placement* and *class
names* randomise.

**Therefore: recognition = nearest-neighbour template match against a committed,
one-time reference set of the 10 digit glyphs. No OCR, no ML, no extra heavy
dependency** — only Pillow (already needed to decode/crop the JPEG; FRD §4's
`io.BytesIO`). This keeps the decoder pure, deterministic, and fully offline-
testable. The site *can* change the font/sprite without notice — when that
happens the reference set stops matching, decode fails, and the listing is
skipped + alerted (exactly the FRD §10 failure path). That fragility is by
design, not a gap.

---

## New file: `parsers/sprite_decoder.py` — the pure decoder (the meat of #4)

A standalone, network-free module. Everything it needs is passed in as bytes/str.

```python
class SpriteDecodeError(Exception): ...

def decode_price(preco_css: str, style_css: str, sprite_bytes: bytes) -> float:
    """Decode one obfuscated LigaPokemon precoCss price (FRD §10).
    Raises SpriteDecodeError on any failure (missing style entry, undecodable
    crop, unparseable result). Never returns a wrong-but-plausible number."""
```

Steps (FRD §10 decode algorithm):

1. **Build the position map** from `style_css`: regex every
   `.<class>{...background-position:<x>px <y>px...}` → `{class: (x, y)}`. Also
   capture the `/imgnum/` sprite URL via the `background-image` rule (the caller
   needs it to fetch the sprite — see seam below; the decoder itself receives the
   already-fetched `sprite_bytes`).
2. **Open the sprite** from `sprite_bytes` via `io.BytesIO`, `Image.open(...)
   .convert("L")` — grayscale, in memory only, **never written to disk** (FRD §4,
   hard constraint). Discard on return.
3. **Per group** in `preco_css.split(";")`:
   - `"V"` → decimal separator.
   - else pick the class present in the position map; `crop((-x, -y, -x+8,
     -y+21))`; `recognise()` it to a digit `0–9`.
   - A group with no position-mapped class, or a crop that matches no reference
     within tolerance → `raise SpriteDecodeError`.
4. **Assemble** the digit string around the single separator and
   `float(... .replace(",", "."))`. Guard: exactly one `V`, only digits
   otherwise, parses cleanly — else `SpriteDecodeError`.

**`recognise(crop) -> str`**: nearest-neighbour over the 10 reference templates by
mean absolute pixel difference (`PIL.ImageChops.difference` summed / area).
Return the argmin digit; if even the best distance exceeds a sane cutoff (ambiguous
/ unknown glyph), raise `SpriteDecodeError`. Verified margin in the capture:
correct digit at `0.00`, runner-up `≥6`, so a cutoff well below the inter-digit
gap cleanly separates "recognised" from "the site changed the font."

### Reference templates: `parsers/digit_templates.png` (committed)

- An `80×21` grayscale strip = digits `0–9` left-to-right, each cell `8×21`,
  extracted **once** from a captured sprite and labelled by eye (the glyphs are
  unambiguous). Committed as a small binary a reviewer can open and verify.
- The decoder loads it at import, slices into 10 templates, caches them.
- Document provenance in a header comment / sibling `digit_templates.md`: which
  capture it came from and the one-off extraction recipe, so it can be rebuilt if
  the site ever changes its font.
- Provide a dev-only helper `tools/build_digit_templates.py` (not run at runtime)
  that, given a saved HTML + its sprite, emits the labelled strip — so refreshing
  the references is reproducible, not hand-magic.

---

## Wiring into `LigaPokemonParser` — keep `parse(html, card)` pure, inject network

The parser interface is fixed: `parse(html, card) -> list[PriceResult]`
(FRD §11, hard constraint — no network inside `parse`). But decoding needs a
**second HTTP request** for the sprite, with `SPRITE_REQUEST_DELAY_SECONDS` and
403/429 handling (FRD §4, §12, §17). Resolve via **dependency injection on the
parser**, not by changing the signature:

```python
class LigaPokemonParser(MarketplaceParser):
    def __init__(self, *, sprite_fetcher=None, on_sprite_error=None):
        # sprite_fetcher: Callable[[str], bytes] | None
        # on_sprite_error: Callable[[SpriteDecodeContext], None] | None
```

- **`sprite_fetcher`** is supplied later by the **scanner** (a separate issue),
  which owns HTTP, the intra-card delay, retries, and the 403/429 stop-cycle
  rule. The scanner's fetcher takes the `/imgnum/` URL and returns the sprite
  bytes (held in memory). In **tests**, it's a trivial fake returning the
  committed fixture sprite bytes — fully offline.
- **When `sprite_fetcher is None`** (current default, and all happy-path/#3
  tests): behaviour is unchanged — `precoCss` listings are skipped exactly as
  today. This keeps #3's tests green and lets #4 land without the scanner.
- **Replace the seam** at `parsers/ligapokemon.py:49`
  (`# TODO(precoCss): sprite-decode path`): when `precoFinal` is absent but
  `precoCss` is present **and** a `sprite_fetcher` is configured →
  - lazily fetch the sprite **once per page** (cache the bytes on the call, since
    one sprite serves every `precoCss` listing on the page — the example page
    has 16 `precoCss` listings but only **one** `/imgnum/` sprite, so one fetch),
  - extract the inline `<style>` once,
  - `decode_price(...)` → treat the result like any other listing price for the
    lowest-per-condition reduction.
- **On `SpriteDecodeError` (FRD §10 failure path):** skip *that listing only*,
  continue the remaining listings (a decodable one may still yield the lowest),
  and invoke `on_sprite_error(ctx)` (carrying `card`, `url`, `error_message`)
  **once per failed listing**. The parser does **not** itself touch `scan_errors`
  or Discord — those are storage/notifier concerns. The callback is the seam the
  future scanner uses to: record `scan_errors` with `error_type="sprite_decode"`
  (FRD §10 step 3, §12) and send the `⚠️ Sprite decode failed …` Discord alert
  (FRD §7). A sprite-fetch HTTP 403/429 is **not** caught here — it propagates so
  the scanner can stop the cycle (FRD §12, §17).

This keeps the failure strictly **per-listing**, never per-card, never per-cycle
(issue acceptance + FRD §10 "not a per-card parser failure").

---

## Test fixtures: `tests/fixtures/ligapokemon/`

Capture **HTML and its matching sprite in the same session** (the sprite URL is
per-load — a sprite fetched later will not match an older HTML). Commit:

- `greninja_116_precocss.html` — the issue's example page (`Mega Greninja ex
  (116/086)`, ed CRI). ~235 KB, 16 `precoCss` NM listings + 35 `precoFinal` NM
  listings, one inline `<style>`, one `/imgnum/` sprite. The **regression anchor
  for the decode path**.
- `greninja_116_sprite.jpg` — the exact `/imgnum/` sprite that page's
  `precoCss`/style reference (`600×84`). Committed so the test is fully offline;
  the fake `sprite_fetcher` returns these bytes.
- `digit_templates.png` lives under `parsers/` (production asset), not fixtures.

**Verified expected values from this capture** (use as exact assertions):

| listing kind | lowest NM price |
|---|---|
| `precoFinal` only (what #3 sees today) | **934.15** |
| `precoCss` decoded (this issue) | **843.00** |
| combined (decoder wired in) | **843.00** |

The cheapest NM listing on the page is `precoCss`-obfuscated (`843,00`) and is
**lower than every plain `precoFinal`** — so the decoder demonstrably changes the
reported lowest price (`934.15 → 843.00`). That is the headline correctness
assertion. (All 16 `precoCss` NM listings decoded clean in the capture: 843,00 /
846,50 / 849,89 / 849,90 / 850,00 / 871,39 / 879,75 / 899,00 / 899,75 / 949,99 /
950,29 / 982,79 / 999,00 / 999,90 / 999,90 / 999,99.)

For the **broken-fixture** path, prefer a small **hand-crafted** HTML string in
the test (one `precoCss`-only listing whose group references a class **absent**
from the `<style>` map, or a sprite that decodes to garbage) rather than mutating
the 235 KB anchor — keep failure cases isolated and readable.

---

## Tests

### `tests/test_sprite_decoder.py` (pure unit, no parser, no network)
- `decode_price` on the captured `precoCss` + style + sprite bytes → `843.00`
  (and a couple more of the verified values above).
- Each reference template recognises itself at distance ~0.
- Missing style-map class for a group → `SpriteDecodeError`.
- Unrecognisable crop (e.g. blank/garbage region beyond cutoff) →
  `SpriteDecodeError`.
- `V` placement / multiple separators / non-digit result → `SpriteDecodeError`.
- Sanity: the sprite is opened from bytes via `io.BytesIO` and nothing is written
  to disk (FRD §4) — assert no temp file created (or simply that the function
  takes/returns only in-memory objects).

### `tests/test_ligapokemon_parser.py` (extend)
- **Decode wired in:** `LigaPokemonParser(sprite_fetcher=fake)` on the Greninja
  fixture, `card.conditions=("NM",)` → `NM == 843.00` (and assert it's lower than
  the `precoFinal`-only `934.15`). `fake` returns the committed sprite bytes and
  asserts it was called **once** (single sprite per page).
- **Default unchanged:** `LigaPokemonParser()` (no fetcher) on the same fixture →
  `precoCss` listings skipped → `NM == 934.15`. Confirms #3 behaviour preserved
  and the seam is opt-in.
- **Per-listing failure isolation:** a fixture with one good `precoFinal` NM
  listing + one broken `precoCss` NM listing, with a `sprite_fetcher` whose
  sprite makes decode fail → result still returns the good price, **no exception
  escapes**, and `on_sprite_error` fired exactly once. Proves "graceful per-
  listing skip, not a per-card failure" (issue acceptance).
- **403/429 on sprite fetch propagates:** a `sprite_fetcher` that raises the
  HTTP-403/429 signal is **not** swallowed by the parser (cycle-stop is the
  scanner's call, FRD §12/§17).
- Existing #3 tests stay green untouched.

---

## Dependencies

- **Pillow** — add to `requirements.txt` / `pyproject.toml`. Needed only for JPEG
  decode + crop + pixel diff. No OCR/ML library (the stability finding makes that
  unnecessary). FRD §20's stack doesn't list it, but §4 already implies in-memory
  image decoding — note the addition in the PR.

---

## Acceptance (from issue #4)
- ✅ Fixture page + saved sprite → expected price (`843,00`; lowest NM
  `934.15 → 843.00`).
- ✅ Broken fixture → graceful per-listing skip, no crash, **not** a per-card
  failure; surfaced via `on_sprite_error` for the scanner to log
  `scan_errors(sprite_decode)` + Discord alert.
- ✅ Parse inline `<style>` → `class → background-position` map; `/imgnum/` sprite
  URL extracted; `8×21` digit crops; `V` decimal separator handled.
- ✅ Pure, deterministic, offline tests.

---

## Out of scope (deferred to the scanner / later issues — leave the seams)
- The real `sprite_fetcher`: HTTP GET of the sprite with
  `SPRITE_REQUEST_DELAY_SECONDS`, retry policy (FRD §13), 403/429 stop-cycle
  (FRD §12, §17), and in-memory-only handling at the call site.
- Actually **writing** `scan_errors(error_type="sprite_decode")` and **sending**
  the `⚠️ Sprite decode failed …` Discord alert — the parser exposes
  `on_sprite_error`; the scanner/notifier wire it (FRD §7, §10, §12).
- `app.py` wiring, lock file, logging, baseline comparison.

## Suggested PR
Single PR — `parsers/sprite_decoder.py`, `parsers/digit_templates.png` (+ provenance
note), `tools/build_digit_templates.py`, the `LigaPokemonParser` injection seam,
fixtures, and both test files. `pytest` green, no network. Commit references
`closes #4`.
