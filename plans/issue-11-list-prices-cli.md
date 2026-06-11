# Plan: Issue #11 — CLI tool `tools/list_prices.py` · FRD §11, §19, §4

The first **runnable end-to-end path** in the project: fetch a live LigaPokemon
card page, run the full extraction (including live `precoCss` sprite decode), and
print every listing. No DB, no Discord, no lock file, no baseline — a pure
read-and-print inspection tool. It is the developer's smoke test that #5 (HTTP)
and #10 (`parse_listings`) actually compose against the real site.

Source of truth: `FRD.md` §11 (a debugging tool calls `parse_listings` on its own
to print every listing), §19 (`tools/list_prices.py` in the layout), §4 (sprite
delay + in-memory sprite). Issue #11 scope: print `CONDITION PRICE` per line,
sorted by condition (M, NM, SP, MP, HP, D) then price ascending; `precoCss` prices
decode live; a sprite-decode failure prints a warning and continues; 403/429
aborts cleanly with no retry storm.

**Depends on:** #5 (HTTP fetch layer — `HttpFetcher`, merged) and #10
(`LigaPokemonParser.parse_listings` + `Listing` — merged). Both done, so this is
unblocked.

Match repo conventions: `from __future__ import annotations`, FRD refs in
docstrings, typed signatures, separate pure/testable logic from the
network-touching `main()` so tests stay offline (mirror how `LigaPokemonParser`
and `HttpFetcher` inject their outside-world dependencies).

---

## Two verified facts that drive the design

1. **The sprite URL is read from the HTML per page, and is already normalised by
   the decoder — the tool does NOT re-normalise.** Verified against the live
   Greninja page on 2026-06-11 (HTTP 200, ~242 KB): the `background-image` rule
   is `url(//repositorio.sbrauble.com/.../imgnum/...jpg)` — protocol-relative, no
   scheme, and randomised per load (issue #4). It is extracted fresh from each
   page's inline `<style>` by `parse_style_css` (`sprite_decoder.py:46-49`); it is
   never hardcoded.
   - **Normalisation already happens upstream:** `parse_style_css`
     (`sprite_decoder.py:57-58`) converts `//host/...` → `https://host/...`
     before populating `SpriteStyle.sprite_url`, which `LigaPokemonParser` then
     hands to `self._sprite_fetcher(...)` (`ligapokemon_parser.py:95`). So by the
     time `HttpFetcher.get_sprite` is called in the real flow, the URL is already
     scheme-qualified.
   - **Therefore the tool wires `sprite_fetcher=fetcher.get_sprite` directly — no
     wrapper, no `//`→`https:` shim.** (A scheme-less URL *would* raise
     `httpx.UnsupportedProtocol` and — since that subclasses
     `httpx.TransportError` — would be retried twice by `HttpFetcher._request`
     before `FetchError`; but the decoder prevents that case from ever reaching
     the fetcher, so the tool relies on the decoder rather than duplicating the
     fix.)

2. **`load_app_config()` requires `DISCORD_WEBHOOK_URL`** (`services/config.py:81`),
   which is meaningless for a read-only inspection tool. So the CLI must **not**
   call `load_app_config()`. It reads only the HTTP-relevant settings itself
   (with FRD §3 defaults), via `load_dotenv()` so a present `.env` still works.
   The validated full-config path stays untouched (it legitimately requires a
   webhook for the real app) — no changes to `config.py`.

---

## New file: `tools/list_prices.py`

### Run contract (from the issue)
```
$ python tools/list_prices.py "https://www.ligapokemon.com.br/?view=cards/card&card=Mega%20Greninja%20ex%20(116%2F086)&ed=CRI&num=116"
NM 843.00
NM 934.15
SP 1200.00
```

### Import bootstrap (so `python tools/list_prices.py` works from repo root)
Running a script in `tools/` puts `tools/` on `sys.path[0]`, **not** the repo
root, so `import services` would fail. At the very top, before importing project
packages:
```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
```
(There is no `tools/__init__.py` today; keep it that way and use the path insert,
which matches the issue's documented `python tools/list_prices.py` invocation.
Don't switch to `-m tools.list_prices`.)

### Structure — separate pure logic from network wiring

Keep the sortable/printable/decision logic in small pure functions so tests never
touch the network, and confine HTTP construction to `main()`.

```python
CONDITION_ORDER = ("M", "NM", "SP", "MP", "HP", "D")  # FRD §10 quality order

def sort_listings(listings: list[Listing]) -> list[Listing]:
    """Sort by condition (M, NM, SP, MP, HP, D) then price ascending.
    Unknown conditions sort last (rank = len(CONDITION_ORDER))."""
    rank = {c: i for i, c in enumerate(CONDITION_ORDER)}
    return sorted(listings, key=lambda l: (rank.get(l.condition, len(CONDITION_ORDER)), l.price))

def format_listings(listings: list[Listing]) -> str:
    """One `CONDITION PRICE` line per listing, price as %.2f (e.g. 'NM 843.00')."""
    return "\n".join(f"{l.condition} {l.price:.2f}" for l in sort_listings(listings))

def run(url: str, *, fetcher: HttpFetcher, on_sprite_error) -> list[Listing]:
    """Fetch the page and return every listing (the testable core).
    sprite_fetcher is fetcher.get_sprite directly: the decoder already
    normalises the protocol-relative sprite URL (sprite_decoder.py:57-58), so
    get_sprite always receives a scheme-qualified URL — no wrapper needed."""
    parser = LigaPokemonParser(
        sprite_fetcher=fetcher.get_sprite,
        on_sprite_error=on_sprite_error,
    )
    html = fetcher.get_page(url)
    return parser.parse_listings(html)
```

### `main(argv) -> int`

1. **Argparse**: one positional `url`. Usage/description naming LigaPokemon.
2. **Build the fetcher from env (no webhook required):**
   ```python
   load_dotenv()
   fetcher = HttpFetcher(
       user_agent=os.getenv("USER_AGENT", "PokemonCardWatcher/1.0"),
       timeout_seconds=int(os.getenv("HTTP_TIMEOUT_SECONDS", "20")),
       request_delay_seconds=0,  # unused: the tool never loops over cards / calls wait_between_cards
       sprite_request_delay_seconds=int(os.getenv("SPRITE_REQUEST_DELAY_SECONDS", "2")),
   )
   ```
   Use the **same defaults as FRD §3** (`PokemonCardWatcher/1.0`, `20`, `2`). The
   small duplication of defaults is acceptable for a standalone dev tool and
   keeps the validated config path (and its webhook requirement) out of scope.
   `request_delay_seconds` is required by the constructor but never exercised —
   pass `0` and note why.
3. **`on_sprite_error`** = print `⚠️ sprite decode failed: {message}` to
   **stderr** and continue. (The parser already dedupes to one call per page —
   `ligapokemon_parser.py:60` — so at most one warning prints.)
4. **Wrap the call** in `with fetcher:` (context manager closes the client) and
   `try/except` (see error handling). On success:
   - `listings = run(url, fetcher=fetcher, on_sprite_error=...)`
   - if empty → print a note to **stderr** (`"no listings found for <url>"`) and
     return `0` (an empty page is not an error).
   - else `print(format_listings(listings))` to **stdout**, return `0`.
5. `if __name__ == "__main__": raise SystemExit(main(sys.argv[1:]))`.

### Error handling (FRD §12; issue acceptance "abort cleanly, no retry storm")

| Condition | Behaviour | Exit |
|---|---|---|
| `CycleStop` (403/429 from fetcher) | stderr: `"aborted: HTTP {status} from source — stopping (anti-abuse)"`. **No retry** — the fetcher already raises immediately without retrying (#5), so there is no retry storm by construction. | non-zero (e.g. `2`) |
| `FetchError` (timeout/network, retries exhausted) | stderr: `"fetch failed: {url}"` (the fetcher already retried twice per §13) | non-zero (e.g. `1`) |
| `ValueError` from `parse_listings` (page lacks `cards_stock` / malformed — e.g. not a card URL or site changed) | stderr: `"could not parse listings: {message}"` | non-zero (e.g. `1`) |

Print nothing to **stdout** on these paths (stdout is only the listing lines, so
the output stays pipe-clean). A sprite-decode failure is **not** in this table —
it is a per-listing warning to stderr and the run continues with the remaining
listings (FRD §10), exit `0`.

Optional nicety: if `not LigaPokemonParser().can_handle(url)`, print a clear
"only LigaPokemon URLs are supported" message and return non-zero before
fetching. (There is no parser registry yet; the tool instantiates
`LigaPokemonParser` directly — fine for v1, FRD §11 "initial parsers".)

---

## Tests: `tests/test_list_prices.py` (offline — fixtures + fakes, no network)

Reuse the committed issue-#4 fixtures (`tests/fixtures/ligapokemon/`): the
`greninja_116_precocss.html` page and its matching `greninja_116_sprite.jpg`,
whose decoded values are already verified (lowest NM `precoCss` = `843.00`,
lowest `precoFinal` = `934.15`).

Inject deps so nothing hits the network:
- A **fake `HttpFetcher`** is awkward (concrete class); prefer building a **real
  `HttpFetcher` backed by `httpx.MockTransport`** (as `test_fetcher.py` already
  does) whose handler returns the fixture HTML for the page URL and the fixture
  sprite bytes for the (already `https://…`) sprite URL the decoder requests. Use
  a spy `sleep` to stay instant.

Cases:

1. **`sort_listings` / `format_listings` (pure):** hand-built listings out of
   order → exact expected string, e.g. input `[SP 1200, NM 934.15, NM 843.00]`
   (any order) → `"NM 843.00\nNM 934.15\nSP 1200.00"`. Asserts condition-order
   then price-ascending and the `%.2f` formatting. Include an unknown condition
   to prove it sorts last.
2. **Full decode via `run` (the headline):** MockTransport fetcher serving the
   Greninja HTML + sprite, `card`-agnostic (`parse_listings` returns all
   conditions) → the returned listings include the `precoCss`-decoded `843.00`,
   and `format_listings(...)` puts `NM 843.00` first and below the `precoFinal`
   `934.15`. Proves the live decode path composes (sprite fetched once). Note the
   sprite handler must answer the **already-normalised** `https://…` URL the
   decoder produces — there is no `//` URL in this flow. (The decoder's own
   `//`→`https:` normalisation is already covered by `test_sprite_decoder.py`;
   the tool does not re-test it.)
3. **`main` happy path (capsys):** point the page handler at the fixture, call
   `main([url])` → returns `0`, stdout is the sorted lines, stderr empty.
   (Inject the MockTransport client — see note below.)
4. **403/429 aborts cleanly:** fetcher whose page handler returns `403` → `main`
   returns non-zero, stderr carries the abort message, **stdout empty**, and the
   handler was called **once** (no retry storm).
5. **`FetchError` path:** page handler raises `httpx.ConnectError` twice → `main`
   returns non-zero, stderr message, stdout empty (the fetcher retried per §13;
   the tool does not add its own retries).
6. **Sprite-decode warning continues:** a small hand-crafted HTML with one good
   `precoFinal` NM listing + one `precoCss` listing whose sprite decodes to
   garbage → stdout still prints the good listing, stderr has exactly one
   `⚠️ sprite decode failed` line, exit `0`. Proves per-listing skip (FRD §10),
   not a hard failure.
7. **Empty page:** HTML with empty `cards_stock` → stdout empty, stderr "no
   listings found", exit `0`.

**Testability seam for `main`:** give `main` an optional injected fetcher (or an
optional `client`/`fetcher` parameter, default `None` → build the real one) so
tests pass a MockTransport-backed `HttpFetcher` without monkeypatching env/network.
Keep the production `__main__` call using the default (real) construction. Document
the seam as test-only.

---

## Dependencies

None new. Uses `httpx` (via `HttpFetcher`), the existing parser/decoder, and
`python-dotenv` (already a dep) for `load_dotenv()`. Pillow is pulled in
transitively by the decoder, unchanged.

---

## Acceptance (from issue #11)

- ✅ Running against a live LigaPokemon URL prints every listing, **including
  `precoCss`-decoded prices** (sprite URL read live from the page and normalised
  by the decoder; covered offline by test 2).
- ✅ 403/429 (surfaced by #5's `CycleStop`) aborts cleanly with a message and
  **no retry storm** (test 4; the fetcher never retries stop statuses by
  construction).
- ✅ Output is `CONDITION PRICE` per line, sorted by condition (M, NM, SP, MP,
  HP, D) then price ascending (tests 1, 2, 3).
- ✅ A sprite-decode failure prints a warning and continues (test 6; FRD §10).

---

## Manual smoke test (what unlocks "test a full call")

From the repo root, after this lands:
```
python tools/list_prices.py "https://www.ligapokemon.com.br/?view=cards/card&card=Mega%20Greninja%20ex%20(116%2F086)&ed=CRI&num=116"
```
Expect a sorted list of NM/SP listings with at least one `precoCss`-decoded price
(the Greninja page's cheapest NM is sprite-obfuscated). A `403`/`429` aborts with
the polite message instead of hammering the site. This is the first real network
exercise of the #5 + #10 + #4 stack end-to-end.

---

## Out of scope (later issues / leave seams)

- The **scanner** loop, baseline comparison, `scan_results`/`scan_errors` writes,
  Discord alerts (issues #6, #7) — this tool only reads and prints; its
  `on_sprite_error` just warns to stderr, it does **not** write `scan_errors`.
- Inter-card delay / multi-URL batching — the tool takes **one** URL.
- A parser **registry** / multi-marketplace dispatch — instantiate
  `LigaPokemonParser` directly (FRD §11 initial parsers).
- Full `load_app_config` wiring / webhook — deliberately bypassed (read-only tool).

## Suggested PR

Single PR — `tools/list_prices.py` + `tests/test_list_prices.py` (reusing the
existing `tests/fixtures/ligapokemon/` assets; no new fixtures needed except a
tiny inline broken-`precoCss` HTML string for test 6). `pytest` green, fully
offline. Commit references `closes #11`.
