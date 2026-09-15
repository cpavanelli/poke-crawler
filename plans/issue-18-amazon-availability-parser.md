# Plan: Issue #18 — `AmazonParser` availability watch (amazon.com.br product pages) · FRD §11, §21

Watch a single **Amazon Brasil product page** and send a Discord alert when the item
becomes **available**. First target: the 30th Elite Trainer Box (English),
`https://www.amazon.com.br/dp/B0H78BB9TY`, which is currently **"Não disponível"**.

**This reuses the New-Product (Catalog) Watch from #15 with no new mode.** A product
page is treated as a catalog holding exactly **one** `Product`. The existing machinery
already covers the use case:

- first scan ⇒ silent seed (`in_stock=False` today, no message);
- sold-out → in-stock ⇒ `detect_changes` emits `RESTOCK` ⇒ one Discord message;
- in-stock → sold-out, price moves ⇒ no alert.

`models/`, `services/catalog.py`, `services/storage.py`, `services/notifier.py`,
`services/fetcher.py`, and the `cards.json` schema are **untouched**. The shared-file
changes are one entry in `DEFAULT_CATALOG_PARSERS` and moving Fourse's BRL helper into
a shared module (Step 2).

---

## Verified against the live pages (2026-09-15) — read this before writing the parser

All fetches were plain `GET`s sending only a `User-Agent` header, exactly as
`HttpFetcher` does. **HTTP 200** for both the app's default UA
(`PokemonCardWatcher/1.0`, ~226 KB) and a Chrome UA (~690 KB). No CAPTCHA, no
`validateCaptcha` form, fully server-rendered, served in pt-BR with no
`Accept-Language` header. **No `application/ld+json`** on any page.
`robots.txt` for `User-agent: *` has `Allow: /*/dp/`.

Three buy-box states were captured:

| State | Sample ASIN | Buy-box markers (all inside `#desktop_buybox`) | Price |
|---|---|---|---|
| **Sold out** | `B0H78BB9TY` (target) | `#outOfStock`; no `#add-to-cart-button`; no `#corePrice_feature_div` | none |
| **In stock (buy box)** | `B0H7XF3LJL` | `#qualifiedBuybox` → `#add-to-cart-button`; `#availability` = `Em estoque`; seller `Amazon.com.br` | `#corePrice_feature_div .a-offscreen` = `R$147,49` |
| **Offers only (no buy box)** | `B0C8Y8MJ36` | `#unqualifiedBuyBox` → `#buybox-see-all-buying-choices` = `Ver todas as opções de compra`; no add-to-cart | none on page |

Fields on the target page:

| Field | Source | Verified value (`B0H78BB9TY`) |
|---|---|---|
| `product_id` | `input#ASIN[value]`; fallback: `/dp/([A-Z0-9]{10})` in `link[rel=canonical]` | `B0H78BB9TY` |
| `name` | `#productTitle` text, whitespace-collapsed | `Pokémon TCG: 30th Elite Trainer Box - Inglês` |
| `url` | **built** as `https://www.amazon.com.br/dp/<ASIN>` | `https://www.amazon.com.br/dp/B0H78BB9TY` |
| `price` | buy-box price (see trap 2) | `None` |
| `in_stock` | buy-box state (see rules below) | `False` |

### Trap 1: the sold-out markup depends on the User-Agent

The same sold-out page renders **different wrappers** depending on the UA:

| | App UA (`PokemonCardWatcher/1.0`) | Chrome UA |
|---|---|---|
| `#outOfStock` wrapper | `#outOfStockBuyBox_feature_div` | `#FALLBACK_OFFER_DISPLAY_desktop` |
| `#availability` | present, `Não disponível. Não temos previsão…` | **absent** |

`USER_AGENT` is configurable in `.env`, so both variants will reach production.
**Rule: key stock on `#add-to-cart-button` / `#buybox-see-all-buying-choices` /
`#aod-ingress-link` / `#outOfStock`. Never on `#availability` text** (it is missing
under a browser UA, and empty on the offers-only page). **Never on a wrapper id**
(`outOfStockBuyBox_feature_div`, `FALLBACK_OFFER_DISPLAY_desktop`, `qualifiedBuybox`),
since those are layout artifacts that already vary.

### Trap 2: the page is full of unrelated prices

Every page carries 9 to 18 `.a-offscreen` nodes from sponsored and "related" carousels
(`CardInstance…` ancestors, `ProductSpecs-N`). The sold-out target page has **9 prices
and none of them belong to the product** (`R$449,90`, `R$139,00`, `R$99,99`, …). Some
are malformed (`R$1.04688`, `R$97022`, `$00`). The in-stock page also has an **empty**
`#corePriceDisplay_desktop_feature_div .a-offscreen`, plus `#aod-ingress-link` =
`Comparar outras 14 ofertas a partir de R$138,90`, which is *lower* than the buy-box
`R$147,49`.

**Rule: no document-rooted price selector.** Price comes only from
`#desktop_buybox #corePrice_feature_div .a-offscreen` (first non-empty text). If that is
absent, fall back to `#aod-ingress-link .a-offscreen` (the "a partir de" offer price,
display-only). Otherwise `None`. A naive `soup.select_one(".a-offscreen")` on the target
would report the item "available at R$449,90".

### Trap 3: the configured URL carries wish-list tracking

The URL as pasted is
`/dp/B0H78BB9TY/?coliid=I3Q7XNBNIOVQFK&colid=1EFQV8H980ZM9&psc=0&ref_=list_c_wl_lv_ov_lig_dp_it`.
`watch_id = SHA256(url)`, so a later edit to that URL silently re-seeds a new watch.
**Configure the clean `https://www.amazon.com.br/dp/B0H78BB9TY`.** The parser builds
`Product.url` from the ASIN, never from the page or config URL, so alerts link cleanly.

### Price format

`R$147,49` / `R$1.561,31`: the same Brazilian format Fourse already parses (Step 2).

---

## Decisions (locked; do not silently re-decide)

1. **Reuse `type: "new_product"`, no new entry type.** Chosen by the user. The Discord
   line reads `🆕 <name> — 1 update(s)` / `• RESTOCK: …`. Accepted; no notifier change.
2. **"Available" = any offer at all** (user's choice). `in_stock` is `True` when **any**
   of these is true:
   - `#add-to-cart-button` inside `#desktop_buybox` (buy box, any seller);
   - `#buybox-see-all-buying-choices` inside `#desktop_buybox` (offers without a buy box);
   - `#aod-ingress-link` exists (the "other sellers" link).

   Otherwise, if `#outOfStock` is inside `#desktop_buybox`, `in_stock` is `False`. The
   seller is **not** checked: third-party sellers count. Offer signals are checked
   **before** `#outOfStock`, so a page showing both is available.
3. **Unrecognised page ⇒ raise, do not degrade.** This intentionally differs from
   Fourse/Nuvemshop's "ambiguous stock ⇒ sold out + warn". A product page holds one
   product, so its stock state *is* the whole signal. A silent `False` after an Amazon
   layout change would hide restocks forever. `parse_catalog` raises `ValueError`
   (message names the cause) when:
   - a robot check / CAPTCHA page is served (`form[action*="validateCaptcha"]`, or no
     `#productTitle` **and** no `input#ASIN`). Markers are from Amazon's known
     robot-check page and are **not verified live**; one was not triggered;
   - no ASIN can be read, or `#productTitle` is missing or empty;
   - none of the stock signals in decision 2 and no `#outOfStock` are present.

   The scanner already turns any parser exception into a `scan_errors` row with
   `error_type=parse`, **without touching** watch state. A block therefore can never
   seed an empty watch, and can never make the next real scan fire a false `NEW`.
4. **No bypass, no header tricks, no cycle stop on CAPTCHA.** CAPTCHA/robot pages log and
   continue (FRD §12). The cycle still stops **only** on HTTP 403/429, as the fetcher
   already does. Amazon often answers a block with **503**; the fetcher already retries
   that once and records `fetch`. No fetcher change, no `Accept-Language`, no cookies,
   no rotating UA.
5. **`can_handle` = host allowlist + product path.** Host `amazon.com.br` or a subdomain,
   **and** a path matching `/dp/<ASIN>` or `/gp/product/<ASIN>` (ASIN = `[A-Z0-9]{10}`).
   Search, wish-list, and store pages return `False`, so they get the scanner's existing
   `no parser for url` error instead of a confusing parse failure.
6. **Identity = ASIN.** `input#ASIN[value]`, fallback canonical `/dp/<ASIN>`. The watch
   URL's own ASIN is *not* used (the parser never sees the URL). Title and price are
   display-only.
7. **Shared BRL helper.** Move `parsers/fourse.py::_parse_brl` to
   `parsers/money.py::parse_brl` **unchanged**. Fourse imports it and `AmazonParser` uses
   it. No second copy.
8. **Variants are out of scope.** The target has no variant picker (`parentAsin` equals
   its own ASIN). A variant-child ASIN is watched as its own URL.

## Scope boundary

Out of scope: a new entry type, seller filtering (Amazon-only), variant handling,
price-drop alerts for Amazon, delivery-location (CEP) selection, CAPTCHA handling,
Amazon search/wish-list pages, other Amazon domains, and any change to `models/`,
`services/catalog.py`, `services/storage.py`, `services/notifier.py`, or
`services/fetcher.py`.

**Known limitations** (document in README, do not fix):
- Availability is **location-dependent**. Anonymous requests get Amazon's default
  address (`Entregando em Bela Vista, 01319900`), and the sold-out text says
  `…para o endereço selecionado`.
- "Any offer" includes third-party sellers, often at scalper prices. The alert shows the
  price (or `—`) so the user can judge.
- Amazon may start serving robot checks at any time. They appear as `parse`/`fetch`
  rows in `scan_errors` and are never worked around.

---

## Step 1: Capture fixtures (`tests/fixtures/amazon/`)

Follow the `tests/fixtures/fourse/` convention: real HTML committed to the repo, read once
at module import with `encoding="utf-8"`. Fetch **one page at a time with a 5 s sleep
between requests.**

| File | URL | UA | Must show |
|---|---|---|---|
| `oos_app_ua.html` | `/dp/B0H78BB9TY` | `PokemonCardWatcher/1.0` | `#outOfStock` in `#outOfStockBuyBox_feature_div` |
| `oos_browser_ua.html` | `/dp/B0H78BB9TY` | Chrome UA | `#outOfStock` in `#FALLBACK_OFFER_DISPLAY_desktop`, **no** `#availability` |
| `in_stock.html` | `/dp/B0H7XF3LJL` | app UA | `#add-to-cart-button`, core price `R$147,49` |
| `offers_only.html` | `/dp/B0C8Y8MJ36` | app UA | `#buybox-see-all-buying-choices`, no add-to-cart |

**Live stock changes.** Before committing, check each capture against its "Must show"
column. If a sample changed state, pick another ASIN in that state and update the
expected values in the tests. **Do not** relax the assertions. Prices in the tests must
match the committed fixture, not this plan.

Trim aggressively (`<script>`, `<style>`, SVG, footer, the `#dp` center column except
`#title_feature_div`), but **keep**:
- `link[rel=canonical]`, `input#ASIN`, `#productTitle`;
- the entire `#rightCol` / `#desktop_buybox` subtree;
- `#olpLinkWidget_feature_div` / `#aod-ingress-link` on `in_stock.html`;
- **at least one carousel with `.a-offscreen` prices on every fixture.** This is the
  regression surface for trap 2, and a test asserts it survived trimming.

## Step 2: Shared BRL helper (`parsers/money.py`)

Move `_parse_brl` from `parsers/fourse.py:163` verbatim to `parsers/money.py` as public
`parse_brl(raw: str) -> float | None`, with a short module docstring. In `fourse.py`,
`from parsers.money import parse_brl`, and replace the call site. Move
`test_parse_brl_handles_thousands_separator_and_nbsp` from `tests/test_fourse_parser.py`
to `tests/test_money.py`, and drop `_parse_brl` from the fourse test import. The Fourse
suite must stay green unchanged otherwise.

## Step 3: `AmazonParser` (`parsers/amazon.py`) · FRD §21

Mirror `parsers/fourse.py`: a module docstring documenting the three traps, a
module-level `logger`, constants, the class, then private `Tag`-based helpers.

```python
SUPPORTED_HOSTS = frozenset({"amazon.com.br"})
CANONICAL_PRODUCT_URL = "https://www.amazon.com.br/dp/{asin}"

_PRODUCT_PATH_RE = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:[/?]|$)")
_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")
_BUYBOX = "#desktop_buybox"


class AmazonParser(CatalogParser):
    def can_handle(self, url: str) -> bool:
        # host allowlist (incl. subdomains, as FourseParser) AND _PRODUCT_PATH_RE.search(path)
    def parse_catalog(self, html: str) -> list[Product]:
        soup = BeautifulSoup(html, "html.parser")
        # raises ValueError per decision 3; otherwise returns exactly one Product
        return [Product(product_id=asin, name=name,
                        url=CANONICAL_PRODUCT_URL.format(asin=asin),
                        price=_parse_price(soup), in_stock=_parse_stock(soup))]
```

`parse_catalog` returns **exactly one** product or raises. It never returns `[]`.

Helpers:

- `_check_not_robot_page(soup)`: raise `ValueError("amazon robot check page")` per
  decision 3.
- `_parse_asin(soup) -> str`: `input#ASIN` value, stripped, must match `_ASIN_RE`.
  Otherwise canonical href `_PRODUCT_PATH_RE`. Otherwise raise.
- `_parse_name(soup) -> str`: `#productTitle`, `" ".join(get_text().split())`. Empty ⇒
  raise. BeautifulSoup already decodes entities; do not unescape again.
- `_parse_stock(soup) -> bool`: decision 2, in that order. Buy-box selectors are
  **scoped to the `#desktop_buybox` Tag** (`buybox.select_one(...)`).
  `#aod-ingress-link` may be looked up anywhere (verified: unique id, only present when
  offers exist). None matched and no `#outOfStock` ⇒ raise
  `ValueError("amazon buy box state not recognised")`. Missing `#desktop_buybox` ⇒ the
  same raise.
- `_parse_price(soup) -> float | None`: trap 2 order. Take the first `.a-offscreen` whose
  stripped text is non-empty, then `parse_brl`. Unreadable ⇒ `logger.warning` + `None`
  (price is display-only, so this never raises).

## Step 4: Tests (`tests/test_amazon_parser.py`)

Mirror `tests/test_fourse_parser.py`: module-level fixture reads, plus a `_page(...)`
synthetic builder for the degradation cases. `_page()` must emit a `#desktop_buybox`
wrapper **and** a decoy carousel with an `.a-offscreen` price outside it, so the scoping
rule is exercised by every synthetic test.

Fixture tests (assert values against the committed fixture):

- `test_can_handle` (parametrized). `True`: `https://www.amazon.com.br/dp/B0H78BB9TY`,
  the full wish-list URL from trap 3, `https://amazon.com.br/gp/product/B0H78BB9TY`,
  and a slugged `/Pok%C3%A9mon-TCG-Elite-Trainer-Ingl%C3%AAs/dp/B0H78BB9TY`.
  `False`: `amazon.com` (`.com`, not `.com.br`), `notamazon.com.br`, a search URL
  `/s?k=pokemon`, a wish-list URL `/hz/wishlist/ls/1EFQV8H980ZM9`, `fourse.com.br`,
  `shisuistore.com.br`, `"not-a-url"`. Also assert that `FourseParser` and
  `NuvemshopParser` return `False` for the Amazon URL, so no two parsers claim it.
- `test_oos_app_ua_fixture`: exactly 1 product: `product_id == "B0H78BB9TY"`,
  `name == "Pokémon TCG: 30th Elite Trainer Box - Inglês"`,
  `url == "https://www.amazon.com.br/dp/B0H78BB9TY"`, `price is None`,
  `in_stock is False`.
- `test_oos_browser_ua_fixture`: same product, same values. Inline: assert
  `'id="availability"' not in html` and `"FALLBACK_OFFER_DISPLAY_desktop" in html`
  (trap 1 regression guard; fails loudly if a recapture changes it).
- `test_carousel_prices_are_never_the_product_price`: assert the OOS fixture contains
  `.a-offscreen` prices (count > 0), yet the parse gives `price is None`.
- `test_in_stock_fixture`: `in_stock is True`, `price` equals the fixture's core buy-box
  price (`147.49` at plan time), **not** the lower `#aod-ingress-link` price (`138.90`).
- `test_offers_only_fixture`: `in_stock is True`, `price is None`.

Synthetic tests:

- `#outOfStock` **plus** `#buybox-see-all-buying-choices` ⇒ `in_stock is True` (offers
  win).
- `#outOfStock` plus `#aod-ingress-link` with `R$ 1.234,56` ⇒ `in_stock is True`,
  `price == 1234.56`.
- An `#add-to-cart-button` *outside* `#desktop_buybox` (decoy) with `#outOfStock` inside
  ⇒ `in_stock is False`.
- An empty first `.a-offscreen` in `#corePrice_feature_div` is skipped for the next
  non-empty one.
- Unparseable core price ⇒ `price is None`, product still returned, warning logged.
- ASIN fallback: no `input#ASIN` ⇒ ASIN read from canonical. Neither ⇒ `ValueError`.
- Missing or empty `#productTitle` ⇒ `ValueError`.
- `form action="/errors/validateCaptcha"` page ⇒ `ValueError`.
- Buy box with none of the four signals ⇒ `ValueError`. No `#desktop_buybox` ⇒
  `ValueError`.
- `parse_catalog` never returns `[]` (covered by the raise cases above).

## Step 5: Register the parser (`services/scanner.py`)

```python
from parsers.amazon import AmazonParser
...
DEFAULT_CATALOG_PARSERS: tuple[CatalogParserFactory, ...] = (
    NuvemshopParser,
    FourseParser,
    AmazonParser,
)
```

Extend the catalog cases in `tests/test_scanner.py`, using synthetic Amazon pages and a
fake fetcher, the same way the existing Fourse dispatch tests do:

- **Restock end-to-end.** Scan 1 serves an OOS page for ASIN X ⇒ watch seeded, **zero**
  Discord calls, one `collection_products` row with `in_stock=0`. Scan 2 serves an
  in-stock page for the same ASIN ⇒ **exactly one** Discord call whose content contains
  `RESTOCK:`, the title, and `https://www.amazon.com.br/dp/X`. Scan 3 serves in-stock
  again ⇒ zero calls.
- **Block never seeds.** Scan 1 serves a CAPTCHA page ⇒ one `scan_errors` row with
  `error_type="parse"`, **no** `catalog_watches` row, zero Discord calls. Scan 2 serves
  the OOS page ⇒ silent seed (no false `NEW`).
- An Amazon watch and a Fourse watch in the same run each dispatch to their own parser.

## Step 6: Config sample + docs

**`cards.json`**: append (clean URL per trap 3; write as UTF-8):

```json
{
  "name": "Amazon — 30th ETB (Inglês)",
  "type": "new_product",
  "url": "https://www.amazon.com.br/dp/B0H78BB9TY"
}
```

**`FRD.md`**
- §11 "Initial Parsers": add `- AmazonParser (catalog watch — Amazon Brasil single-product availability)`.
- §21 "Page Fetch & Parsing", after the Fourse table: add an Amazon subsection. It says a
  product page is parsed as a one-product catalog, gives the field table from the top of
  this plan, the any-offer stock rule (decision 2), and the raise-on-unrecognised rule
  (decision 3). Note that "RESTOCK" is the availability alert for this store.
- §21 "Architecture": add a sentence that `AmazonParser` implements the same contract.

**`README.md`**: add Amazon to the supported catalog-watch stores, with: use the clean
`/dp/<ASIN>` URL; "available" includes third-party offers; availability is based on
Amazon's default delivery address; robot checks show up as scan errors and are not
bypassed.

---

## Hard-constraint checklist (CLAUDE.md / FRD)

- [ ] One request per watch; no extra request for offers (`/gp/offer-listing`, AOD AJAX),
      variants, or sprites.
- [ ] `REQUEST_DELAY_SECONDS` between entries (scanner-level, unchanged).
- [ ] HTTP 403/429 stops the cycle (unchanged). CAPTCHA pages **do not** stop the cycle
      and are **never** bypassed: no cookies, header spoofing, UA rotation, or retries
      beyond the fetcher's.
- [ ] `services/fetcher.py` unchanged; the parser performs no I/O and writes nothing to disk.
- [ ] Raw `sqlite3` only; no schema change.
- [ ] Parser implements `can_handle` + `parse_catalog` only, with no new/restock, storage,
      or notification logic (FRD §11). No Amazon special case in `scanner.py` beyond the
      registration line.
- [ ] Shipping (`Entrega GRÁTIS…`, `Frete GRÁTIS…`) is never parsed, stored, or compared.
- [ ] No document-rooted price selector; no stock decision from `#availability` text or
      wrapper ids.
- [ ] `_parse_brl` exists once, in `parsers/money.py`.

## Acceptance

- [ ] `can_handle` matches amazon.com.br `/dp/` and `/gp/product/` URLs (including the
      wish-list-tracked one) and rejects `.com`, search, wish-list, and other-store URLs.
      No other parser claims an Amazon URL.
- [ ] Both OOS fixtures (app UA and browser UA) ⇒ `B0H78BB9TY`, the exact title,
      the clean URL, `price=None`, `in_stock=False`.
- [ ] In-stock fixture ⇒ `in_stock=True`, core buy-box price, not the `aod-ingress` price.
- [ ] Offers-only fixture ⇒ `in_stock=True`, `price=None`.
- [ ] Carousel prices never become the product price.
- [ ] CAPTCHA, missing ASIN, missing title, and unrecognised buy box ⇒ `ValueError`
      (logged as `parse`, no watch state written).
- [ ] End-to-end: silent seed, then exactly one `RESTOCK` message on sold-out → in-stock,
      then nothing on a repeat scan.
- [ ] Fourse suite green after the `parse_brl` move; Shisui/Fourse/LigaPokemon behaviour
      unchanged.

## Verification

```
pytest                      # full suite green
python app.py               # one real cycle
```

After the first `python app.py`, confirm a silent seed (no Discord message) and one row:

```sql
SELECT product_id, product_name, product_url, price, in_stock
FROM collection_products
WHERE product_url LIKE 'https://www.amazon.com.br/dp/%';
```

Expect `B0H78BB9TY | Pokémon TCG: 30th Elite Trainer Box - Inglês |
https://www.amazon.com.br/dp/B0H78BB9TY | NULL | 0`, as long as the item is still sold
out. A non-NULL price while `in_stock = 0` means trap 2 is live.
Also confirm `SELECT * FROM scan_errors ORDER BY id DESC LIMIT 5;` has no Amazon `parse`
row. If one exists, open its `error_message` before changing any selector.

## Suggested PR

Branch `feat/issue-18-amazon-parser` off `main`, PR title
`Add Amazon Brasil availability watch parser (closes #18)`.
