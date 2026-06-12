# Plan: Issue #12 — Sprite decoder multi-exemplar bank · FRD §10, §7, §4 (follow-up to #4)

The `precoCss` decoder from #4 is correct on the committed fixture but **drops
~20–25% of digit crops on the live site today**, which cascades into most
obfuscated listings being skipped. This plan makes the decoder robust to the
sprite's per-position JPEG variation **without ever emitting a wrong price** —
the safety property is the whole point.

Source of truth: `FRD.md` §10 (decode + per-listing safe-skip), §7 (Discord
alert), §4 (sprite in memory, never to disk). Builds directly on #4's pure
decoder (`parsers/sprite_decoder.py`) and the existing
`tools/build_digit_templates.py` labeling machinery.

**Depends on:** #4 (merged). Touches the decoder, its committed asset, the build
tool, and (optionally) the parser's warning. Does **not** touch the HTTP/scanner
layers.

---

## AS-BUILT (implemented) — simpler than this plan assumed

The investigation below proposed a large multi-exemplar bank + k-NN consensus.
Empirically the structure is **much simpler**, so the shipped solution is smaller:

- Each digit renders as just **two pixel-stable bitmaps** (e.g. `9`'s two variants
  are ~24 mean-abs-diff apart; correct matches land at ~0; nearest cross-digit
  >7). So the asset is a tiny atlas — `parsers/digit_bank.png`, **2 templates per
  digit** (20 total) — and recognition is the decoder's **existing nearest-match
  + cutoff** over that list. **No k-NN consensus, no numpy, no 1,600-exemplar
  bank.** Cutoff stays `4.0` (safe window measured as [2, 7]).
- Labelling needs only the **price set** (read once from the page), not per-listing
  order: `tools/build_digit_bank.py` bootstraps confident digits from the current
  bank, unions them per listing `lj_id` across captures, and matches each listing
  to a unique price. `tools/capture_page.py` grabs the in-session HTML+sprite
  pairs. The old single-strip `digit_templates.*` and `build_digit_templates.py`
  are retired.
- **Measured result:** leave-one-capture-out over **18 live renders (1,620 digit
  crops)** → **100% coverage, 0 misreads**. Held-out gate in
  `tests/test_digit_bank.py` (2 fresh captures + the #4 anchor fixture, which is
  independent of the bank and predates it) decodes every obfuscated price with
  zero misreads. Live `list_prices` run decodes all 18 sub-R$1.000 prices.
- **Deferred:** the dropped-listing count in the parser warning (kept this PR
  focused on the decoder; failures are now rare). Tracked as a follow-up.

The rest of this document is the original investigation/diagnosis, kept for the
record. Where it says "k-NN consensus" / "many exemplars," read the As-Built
above — the diagnosis (root cause, safety hazard, zero-misread gate) all held;
only the matcher turned out simpler.

---

## The measured root cause (verified live, not assumed)

Probed against the live Mega Greninja page (`?...num=116`, ed CRI):

- The sprite is a **lossy JPEG**, `600×84` = 75 cols × 4 rows of `8×21` cells.
  Rows sit at `y ∈ {−2, −23, −44, −65}` (21px pitch, offset start).
- The decoder matches each `8×21` crop to **one** clean template per digit
  (`digit_templates.png`, an `80×21` strip) by mean-absolute pixel difference,
  accepting only `≤ _MAX_MEAN_ABSOLUTE_DIFF = 4.0`.
- **Home-row crops match at exactly `0.00`; the same digit elsewhere misses by
  9–18.** A full **±3px 2-D shift search recovers 0 failures** → not crop
  geometry, not template staleness. The glyph **pixels genuinely differ**: the
  `8px` cells don't align to JPEG's `8×8` blocks, so every instance carries
  position-dependent compression noise.
- **The dangerous part:** the noise is large enough to **flip the nearest
  neighbour**. On one page, price `999,90` appeared twice — once with a clean
  leading `9` (dist ~0), once with a `9` whose nearest template was `6`
  (dist ~9). So a naive cutoff raise would decode `999,90` as `699,90`.

### What this rules out (do not implement these)
- **Raising `_MAX_MEAN_ABSOLUTE_DIFF`** — emits wrong prices (the failure above).
  Forbidden by FRD §10 ("never a wrong-but-plausible number").
- **A "2nd-nearest margin" guard** — the *wrong* digit can be the clear nearest,
  so a margin gate still accepts it.
- **Crop re-alignment / sub-pixel shift** — the ±3px search recovered nothing.
- **Re-capturing a single fresh template** — doesn't address per-position
  variation; the new strip would again only match its own home positions.
- **Naive binarisation** — a flat `<128` threshold collapsed all digits to one
  blob in testing (glyph contrast is too low for a fixed global threshold).

---

## The fix: multi-exemplar bank + k-NN, with a zero-misread gate

The brittleness is "one template can't represent the per-position render
variation." Cover the variation with **many labelled exemplars per digit** and
match to the nearest exemplar. Crucially, labelling is **free** via the trick
already in `build_digit_templates.py`: within one page, each CSS class maps to
exactly one digit cell, so a human-read price labels every cell it touches.

### 1. New committed asset: `parsers/digit_exemplars.png` (+ provenance)
- A grayscale **atlas**: 10 rows (digits `0`–`9`), each row holding up to `N`
  exemplar cells of `8×21` (`N` a fixed cap, e.g. 24 → atlas `192×210`). Cells
  filled left-to-right; unused trailing cells left blank and recorded as unused.
- Companion `parsers/digit_exemplars.md` (replaces/extends `digit_templates.md`):
  list every source capture (URL, date, the human-read price sequence) and the
  rebuild command, so the bank is reproducible and a reviewer can audit it.
- Keep cell geometry `8×21` (unchanged constants). Retire the single-strip
  `digit_templates.png` once the bank loader is in (or keep it only as a
  provenance artefact — note which in the PR).

### 2. Build tool: extend `tools/build_digit_templates.py` → emit the bank
Generalise the existing tool (its `_build_digit_map` already labels classes from
known prices and cross-checks consistency):
- Accept **multiple** `--html/--sprite/--prices` capture triples (repeatable
  group), not just one.
- For every capture, label each digit cell via the known prices, crop its
  `8×21`, and collect per digit. **De-duplicate** near-identical crops (drop a
  new crop whose min mean-abs-diff to an already-kept same-digit exemplar is
  `< ~1.0`) so the bank captures *variation*, not 30 copies of the home glyph.
- Cap at `N` exemplars/digit (keep the most diverse — e.g. farthest-point
  sampling by mutual distance). Emit the atlas + update the `.md` provenance.
- Fail loudly if any digit ends with too few exemplars (coverage hole).

### 3. Decoder: bank loader + k-NN matcher (`parsers/sprite_decoder.py`)
- Replace `_load_reference_templates()` with a bank loader: slice the atlas into
  `{digit: [Image, ...]}`, skipping blank trailing cells. Load once at import,
  cache (as today). Still **never writes to disk**; sprite still opened from
  `io.BytesIO` (FRD §4) — unchanged.
- Replace `_recognise_digit(crop)`:
  ```
  score every exemplar by mean-abs-diff; take the k nearest (k=3 default).
  ACCEPT digit d iff:
    - the single nearest exemplar has distance <= ACCEPT_CUTOFF, AND
    - all k nearest exemplars agree on digit d (k-NN consensus).
  else raise SpriteDecodeError  (-> safe per-listing skip, unchanged §10 path)
  ```
  The consensus check is the guard against the wrong-nearest case: a noisy `9`
  near a `6` exemplar will only be accepted if its 3 nearest are *all* the same
  digit. `ACCEPT_CUTOFF` is **chosen empirically** (see gate below) as the
  largest value with zero held-out misreads, then backed off for margin
  (expect ~5–7, comfortably under the ~9 confusion distance, but data decides).
- Everything else (`_decode`, `V` separator handling, single-separator and
  digits-only guards, `decode_price`, `SpriteDecoder`) stays as #4 built it.
- Performance: bank ≈ 10×24 exemplars × ~90 crops/page ≈ 22k diffs — trivial in
  Pillow; **no numpy/OCR/ML dependency** (keep the stack lean, FRD §20).

### 4. (Bundled) surface a dropped-listing count
Today the parser dedupes the sprite warning to once per page
(`ligapokemon_parser.py:60`), hiding how many listings were skipped. Have it
count failed `precoCss` listings and pass the count to `on_sprite_error`
(e.g. message `"17 of 18 obfuscated listings could not be decoded: <reason>"`).
Small change; makes decoder breakage visible in the CLI (#11) and future
notifier (#7). Keep it to "one alert per page" but with a count.

---

## Test fixtures: `tests/fixtures/ligapokemon/`

Capture **multiple** page+sprite pairs in-session (the sprite is per-load — each
HTML must ship with its own sprite). Commit, say, 5–8 captures with their
human-read price sequences recorded in the provenance `.md`. Split them:
- **Bank captures** (most) → build `digit_exemplars.png`.
- **Held-out captures** (≥2, never used to build the bank) → the validation gate.

The existing `greninja_116_*` fixture stays (regression anchor for #4's tests).

---

## Tests: `tests/test_sprite_decoder.py` (extend) + build-tool test

### The acceptance gate (the important one)
- **Zero misreads on held-out captures:** decode every `precoCss` listing in each
  held-out capture; for every digit the decoder *accepts*, assert it equals the
  human-read ground-truth digit. **Any wrong digit fails the test.** Skips
  (SpriteDecodeError) are allowed but counted.
- **Coverage threshold:** assert the held-out *skip rate* is below an agreed bar
  (e.g. ≥90% of obfuscated listings decode). Tune the bank/`N`/cutoff until both
  hold. (If coverage can't reach the bar without misreads, ship the safe
  lower-coverage version — never trade safety for coverage.)

### Unit
- Bank loader slices the atlas into the right per-digit exemplar counts; blanks
  skipped; cells are `8×21`.
- k-NN consensus: a crafted crop whose nearest exemplars disagree → raises
  (proves the wrong-nearest guard).
- A clearly-garbage crop (blank/noise) → raises `SpriteDecodeError`.
- `V` / multiple-separator / non-digit-result guards still raise (unchanged).
- FRD §4: still opens from `io.BytesIO`, writes nothing to disk.

### Build tool (`tests/test_build_digit_templates.py`, new or extend)
- Given a small synthetic HTML+sprite+prices, emits an atlas with the expected
  per-digit exemplar counts and correct labels; dedup drops a duplicate; missing
  digit → exits non-zero.

### Existing #4 tests
- The single-template tests are superseded; update them to the bank API (or keep
  the `greninja` end-to-end decode assertion `843.00`, now via the bank). Keep
  `test_ligapokemon_parser.py` green; update the warning assertion to the new
  counted message if #4 part is bundled.

---

## Validation workflow (how the implementer tunes it)

1. Capture N pairs, read prices in a browser, record in `.md`.
2. Build the bank from the non-held-out captures.
3. Run the held-out gate; record misread count (must be 0) and skip rate.
4. If skip rate too high → add captures / raise `N` / slightly raise
   `ACCEPT_CUTOFF` **only while misreads stay 0**; re-run.
5. Lock `ACCEPT_CUTOFF` a notch below the misread boundary for margin.

This is data-driven on purpose: the exact `N` and cutoff come from the captures,
not from a guess in this plan.

---

## Acceptance (issue #12)

- ✅ Live/held-out captures: **zero wrong digits** ever emitted (safety, FRD §10).
- ✅ Obfuscated-listing decode coverage materially restored (target ≥90% on
  held-out, data permitting) vs the current ~all-skipped.
- ✅ A crop the bank can't match cleanly is still a **per-listing safe skip**,
  surfaced once per page **with a dropped count**.
- ✅ Pillow-only; sprite stays in memory (FRD §4); deterministic offline tests.

---

## Out of scope
- Any change to the HTTP fetcher (#5), scanner (#7), notifier (#7), or the
  `list_prices` CLI beyond consuming the new counted warning message.
- OCR/ML or a heavy image dependency (explicitly avoided).
- Auto-refreshing the bank at runtime — the bank is a committed, reviewed asset;
  refreshing it is a manual, reproducible `build_digit_templates.py` run.

## Suggested PR
Single PR — extended `tools/build_digit_templates.py`, new
`parsers/digit_exemplars.png` (+ `digit_exemplars.md` provenance), the bank
loader + k-NN matcher in `parsers/sprite_decoder.py`, the counted warning in
`parsers/ligapokemon_parser.py`, new multi-capture fixtures, and the updated
tests including the **zero-misread held-out gate**. `pytest` green, no network.
Commit references `closes #12`.
