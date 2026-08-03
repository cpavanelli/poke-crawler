# Plan: Issue #17 — `FourseParser` catalog parser (fourse.com.br / WooCommerce) · FRD §11, §21

Add a second **catalog parser** so the New-Product (Catalog) Watch shipped in #15 can
also watch **Fourse** (`fourse.com.br`), a **WooCommerce** store. First target: the
30th-anniversary pre-order block page
`https://fourse.com.br/block/celebrating-30-years-of-pokemon/`.

**This is a pure addition.** `models/`, `services/catalog.py`, `services/storage.py`,
`services/notifier.py`, and the `cards.json` schema are all untouched — the
`new_product` entry type, `Product`, `detect_changes`, `collection_products`, and the
batched Discord message already do everything needed. The only shared file that
changes is one entry in `DEFAULT_CATALOG_PARSERS`.

---

## Verified against the live pages (2026-08-02) — read this before writing the parser

Fetched with a browser User-Agent → **HTTP 200**, ~380 KB, **fully server-rendered**.
Platform is **WordPress + WooCommerce** with the **Ecomus** theme (`X-Powered-By:
PHP/8.3.31`, `wp-content/plugins/elementor`, `platform: hostinger`) — *not* Nuvemshop.
Prices are plain text; there is no sprite/CSS obfuscation.

The real product grid is `#ecomus-shop-content > ul.products > li.product.type-product`.

| Field | Source | Verified value |
|---|---|---|
| `product_id` | `post-<ID>` token in the `li` class list (fallback: `data-product_id` on the add-to-cart button) | `17960` |
| `name` | `h2.woocommerce-loop-product__title a` text (HTML entities, e.g. `&#8211;` → `–`) | `(PT-BR) Pokémon – Box Coleção Ilustração – Parceiro Inicial – Série 2` |
| `url` | `a.woocommerce-loop-product__link[href]` — already absolute | `https://fourse.com.br/item/pt-br-pokemon-box-colecao-ilustracao-parceiro-inicial-serie-2/` |
| `price` | `span.price ins .woocommerce-Price-amount bdi`, else `span.price > .woocommerce-Price-amount bdi` | `R$ 99,99` |
| `in_stock` | `instock` / `outofstock` token in the `li` class list | `instock` |

**There is no `application/ld+json` anywhere on the site** (zero occurrences on every
page sampled). Unlike Nuvemshop, the WooCommerce class token is the *only* structured
stock signal — but it is first-class WooCommerce output, not a theme artifact, so it
is reliable.

A full sweep for alternative data sources came back empty: no `og:`/`product:` meta,
no `dataLayer`/`gtag`/`ecommerce` payload (Meta Pixel fires `PageView` only, no
`content_ids`), no `wc-block` or Elementor product widgets, no
cross-sell/upsell/related/recently-viewed, and no product template in the quick-view
modal (fetched over AJAX). The target page holds exactly **6 distinct `post-<ID>`
values and 6 distinct `/item/` slugs**. The DOM grid is the only viable source.

### Trap 1 — the header search modal renders 5 phantom products *twice*

Both listings appear on **every page of the site** and have nothing to do with the
page's content.

- **`ul.products` → `li.product.type-product`** ("Nossas recomendações") — full
  WooCommerce loop markup, byte-for-byte indistinguishable from the real grid.
  On `/product-category/pokemon/etbs/`: **11** `li.product` in the document, **6** in
  the grid. On the `/block/` target page: **6** in the document, **1** in the grid.
  A naive `soup.select("li.product")` reports 5 bogus products on the first scan.
- **`ul.search-products-suggest-list` → `li.em-flex`** — a *second*, structurally
  unrelated listing of the same 5 products, using `.suggest-list__link[href]`,
  `.suggest-list__title`, `.suggest-list__price`. It has **no product ID and no stock
  signal**, so it is useless as a source, but it matches any loose fallback such as
  `a[href*="/item/"]` or `.woocommerce-Price-amount`.

**Rule: scope every selector to the `#ecomus-shop-content` subtree. No selector may be
rooted at the document.** Scoping kills both. A document-rooted *fallback* added later
would silently reintroduce the bug through the suggest list.

### Trap 2 — sale prices use `<del>` / `<ins>`

```html
<span class="price">
  <del aria-hidden="true">…<bdi>R$&nbsp;399,99</bdi></del>
  <span class="screen-reader-text">O preço original era: R$ 399,99.</span>
  <ins aria-hidden="true">…<bdi>R$&nbsp;349,99</bdi></ins>
  <span class="screen-reader-text">O preço atual é: R$ 349,99.</span>
  <span class="em-price-unit"><span class="divider">/</span> UN</span>
</span>
```

Taking the first `bdi` yields the **old** price. Prefer `<ins>` when present.
(4 of 16 products on `/product-category/pokemon/page/2/` carry a `sale` class.)

### Trap 3 — three `.price` elements per product

Besides the real `span.price`, each product summary contains
`.fswp_installments_price > p.price` (the 12x installment, e.g. `R$ 25,00`) and
`.fswp_in_cash_price > p.price` (the Pix price). A loose `.price` selector yields the
installment. Also `span.price` ends with `<span class="em-price-unit">/ UN</span>`, so
`get_text()` on the span returns `"R$ 99,99/ UN"` — the amount must be read from the
inner `.woocommerce-Price-amount bdi`.

### Price format

Brazilian text, **not** integer cents like Nuvemshop: `R$&nbsp;1.234,56`. Needs a small
parser — strip the currency symbol and NBSP (`\xa0`), drop `.` thousands separators,
`,` → `.`.

---

## Decisions (locked — do not silently re-decide)

1. **New parser, host allowlist.** `parsers/fourse.py`, `SUPPORTED_HOSTS =
   frozenset({"fourse.com.br"})`, mirroring `NuvemshopParser` exactly. Named for the
   store, not the platform: the selectors depend on the Ecomus theme wrapper
   `#ecomus-shop-content`, so claiming generic WooCommerce support would be a lie.
2. **Grid scoping is mandatory** (trap 1). Only `li.product` inside
   `#ecomus-shop-content` is a product.
3. **Page 1 only** — one request per watch, FRD §21 unchanged. Fourse category pages
   *do* paginate (`/product-category/pokemon/` = 21 products over 2 pages with
   `nav.woocommerce-pagination`), so a watch pointed at a >15-product category URL
   would silently miss page 2+. Accepted known limitation, not in scope.
4. **Identity = the WooCommerce post ID** from the `post-<ID>` class token, fallback
   `data-product_id` on the add-to-cart button. Slug and name are display-only.
   **Never fall back to a bare `data-id`** — the page carries `data-id="215"` on the
   Mailchimp newsletter form (`mc4wp-form-215`), which is not a product.
5. **Degradation matches `NuvemshopParser`:** unreadable price ⇒ `price=None`, product
   kept (price is display-only); missing name or link ⇒ warn and skip; missing or
   ambiguous stock token ⇒ treat as sold out (the state that never alerts on its own)
   and warn.
6. **No changes to shared behavior.** Shisui watch, price watcher, DB schema, and
   Discord format untouched.
7. **Disappearance is not a sell-out.** No `outofstock` product was found anywhere
   (3 pages, 37 products, all `instock`) — Fourse appears to hide sold-out items, so a
   sell-out shows up as *disappearance*. Existing semantics stand: rows are never
   deleted, a vanished product stays recorded as in-stock, a reappearance fires
   neither NEW nor RESTOCK. Practical consequence: **NEW alerts will work; RESTOCK
   likely never fires on this store.** Revisit only if that proves wrong in practice.
8. **Pre-order flag is not surfaced.** Pre-order items carry `product_cat-pre-order`
   and a "Pré-venda" button, but WooCommerce still marks them `instock`, so the
   existing NEW/RESTOCK rules cover them. No `Product` field, no schema column, no
   notifier change.

## Scope boundary

Out of scope: pagination, a generic WooCommerce parser, disappearance-as-sell-out,
the pre-order flag, and any change to `models/`, `services/catalog.py`,
`services/storage.py`, or `services/notifier.py`.

---

## Step 1 — Capture fixtures (`tests/fixtures/fourse/`)

Follow the `tests/fixtures/shisui/` convention: real HTML committed to the repo,
captured with a browser UA, hand-trimmed, read once at module import with
`encoding="utf-8"`.

```python
import httpx

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
for name, url in [
    ("block_30_years", "https://fourse.com.br/block/celebrating-30-years-of-pokemon/"),
    ("category_multi", "https://fourse.com.br/product-category/pokemon/page/2/"),
]:
    html = httpx.get(url, headers={"User-Agent": UA}, follow_redirects=True).text
    (Path("tests/fixtures/fourse") / f"{name}.html").write_text(html, encoding="utf-8")
```

- `block_30_years.html` — the target page. **Keep both phantom listings** from the
  header search modal: the 5 `li.product` "Nossas recomendações" blocks *and* the
  `ul.search-products-suggest-list` block. They are the regression surface for trap 1
  and must survive trimming.
- `category_multi.html` — must include at least one `sale` product (`<del>`/`<ins>`,
  trap 2) and the `em-price-unit` suffix (trap 3).

Trim aggressively elsewhere: inline `<style>`/`<script>`, SVG sprite defs, and the
Elementor footer are the bulk of the bytes and none of them matter.

## Step 2 — `FourseParser` (`parsers/fourse.py`) · FRD §21

Mirror the shape of `parsers/nuvemshop.py`: module docstring documenting the traps,
module-level `logger`, `SUPPORTED_HOSTS`, the class, then private `Tag`-based helpers.

```python
SUPPORTED_HOSTS = frozenset({"fourse.com.br"})

_GRID_SELECTOR = "#ecomus-shop-content ul.products > li.product"
_IN_STOCK_CLASS = "instock"
_OUT_OF_STOCK_CLASS = "outofstock"
_POST_ID_PREFIX = "post-"


class FourseParser(CatalogParser):
    def can_handle(self, url: str) -> bool:
        # identical to NuvemshopParser.can_handle — host allowlist incl. subdomains
    def parse_catalog(self, html: str) -> list[Product]:
        soup = BeautifulSoup(html, "html.parser")
        products = []
        for block in soup.select(_GRID_SELECTOR):
            product = _parse_block(block)
            if product is not None:
                products.append(product)
        return products
```

Products come back in page order. A page with no grid returns `[]` — **not** an
exception; the scanner records a parse error only for genuine exceptions.

There is no `_page_base_url` equivalent: Fourse renders absolute `href`s. Keep
`urljoin` against the canonical URL as a defensive fallback for a relative `href`,
matching `_parse_url` in `nuvemshop.py`.

Helpers:

- `_parse_block(block: Tag) -> Product | None` — same rule chain as
  `nuvemshop._parse_block`: id (warn + skip), name (warn + skip), url (warn + skip),
  stock (warn + treat as sold out), then `Product(...)` with
  `price=_parse_price(block)`.
  Note the id case differs from `nuvemshop._parse_block`, which skips a missing id
  *silently* because there the block is the quick-shop modal template — genuinely not
  a product. Here selection is already scoped to the grid, so every block is a real
  product and an unreadable id drops one; it must warn like every other skip.
- `_parse_id(block)` — scan `block.get("class")` for a token starting with `post-`
  whose remainder is all digits; fall back to
  `block.select_one("[data-product_id]")["data-product_id"]`. Note `class` comes back
  from BeautifulSoup as a **list**, not a string.
- `_parse_name(block)` — `h2.woocommerce-loop-product__title a`, `get_text(strip=True)`.
  BeautifulSoup already decodes `&#8211;` to `–`; do not unescape again.
- `_parse_url(block)` — `a.woocommerce-loop-product__link[href]`; `//`-relative ⇒
  prefix `https:`; already absolute ⇒ as-is; otherwise `urljoin` against the canonical.
- `_parse_price(block)` — `span.price` **scoped to this block**, preferring
  `ins .woocommerce-Price-amount bdi` and falling back to
  `.woocommerce-Price-amount bdi`; select `span.price` specifically so the
  `p.price.fswp_calc` installment/Pix nodes cannot match (trap 3). Then
  `_parse_brl(text)`. Unreadable ⇒ warn + `None`.
- `_parse_brl(raw: str) -> float | None` — strip `R$`, `\xa0`, and whitespace; remove
  `.`; replace `,` with `.`; `float()` guarded by `ValueError` ⇒ `None`.
- `_parse_stock(block)` — read the `li` class list: `outofstock` ⇒ `False`,
  `instock` ⇒ `True`, neither ⇒ `None` (caller warns and treats as sold out). Check
  `outofstock` **first**: a naive substring test for `instock` matches inside
  `outofstock`. Use exact token comparison against the class list, not substring
  matching on the joined string.

## Step 3 — Tests (`tests/test_fourse_parser.py`)

Mirror `tests/test_nuvemshop_parser.py`: module-level fixture reads, `_block(...)` /
`_page(...)` synthetic builders for the degradation cases, real fixtures for the traps.
`_page()` must wrap blocks in `<div id="ecomus-shop-content"><ul class="products">…`
so the scoping rule is exercised by every synthetic test too.

Fixture tests:

- `test_can_handle` — parametrized: `fourse.com.br` and subdomains ⇒ `True`;
  `shisuistore.com.br`, `ligapokemon.com.br`, `notfourse.com.br`, `"not-a-url"` ⇒
  `False`. Also assert `NuvemshopParser().can_handle("https://fourse.com.br/…")` is
  `False`, so the two parsers cannot both claim a URL.
- `test_block_fixture_yields_only_the_grid_product` — **exactly 1** product, with an
  in-test comment that the document holds 6 `li.product` and 6 `/item/` links. Assert
  `product_id == "17960"`, the full name with `–` (en dash, not `&#8211;`), the
  absolute `/item/...` URL, `price == 99.99`, `in_stock is True`.
- `test_phantom_search_modal_listings_are_never_parsed` — document the traps the way
  `test_in_stock_products_parse_despite_the_hidden_esgotado_label` does: assert
  `"search-products-suggest-list" in BLOCK_HTML` and
  `BLOCK_HTML.count('<li class="product ') == 6`, then assert the parse returns 1
  product. This test fails loudly if fixture trimming ever drops the phantoms.
- `test_category_fixture_returns_the_grid_in_page_order` — expected `product_id` list
  equals the grid's, and its length is strictly less than the document's `li.product`
  count.
- `test_sale_price_uses_the_ins_price` — the on-sale fixture product parses to the
  `<ins>` value (e.g. `349.99`), never the `<del>` value (`399.99`).

Synthetic tests:

- Installment (`R$ 25,00`) and Pix nodes are never picked; `em-price-unit` (`/ UN`)
  does not leak into the parsed amount.
- `_parse_brl` handles thousands separators — `R$&nbsp;1.234,56` ⇒ `1234.56`.
- `outofstock` ⇒ `in_stock is False`; **`outofstock` is not misread as `instock`**;
  neither token ⇒ `in_stock is False` + warning.
- Unparseable price ⇒ `price is None` and the product is still returned.
- Missing name ⇒ skipped; missing link ⇒ skipped.
- Missing `post-<ID>` class ⇒ falls back to `data-product_id`; neither ⇒ skipped.
- A page with no `#ecomus-shop-content` returns `[]`.

## Step 4 — Register the parser (`services/scanner.py`)

Two lines, at `services/scanner.py:14` and `:36`:

```python
from parsers.fourse import FourseParser
...
DEFAULT_CATALOG_PARSERS: tuple[CatalogParserFactory, ...] = (NuvemshopParser, FourseParser)
```

`_select_catalog_parser` already returns the first factory whose `can_handle` matches,
so nothing else changes.

Extend `tests/test_scanner.py` (catalog cases live at ~lines 803–1180):

- A Fourse watch URL dispatches to `FourseParser` when both catalog parsers are
  registered, and a Shisui watch in the same run still dispatches to `NuvemshopParser`.
- A catalog URL matching neither still produces the `no parser for url` parse error.

## Step 5 — Config sample + docs

**`cards.json`** — append:

```json
{
  "name": "Fourse — 30 Anos de Pokémon",
  "type": "new_product",
  "url": "https://fourse.com.br/block/celebrating-30-years-of-pokemon/"
}
```

`watch_id = SHA256(url)`, so editing this URL later silently re-seeds a new watch.
Write the file as UTF-8.

**`FRD.md`**
- §11 "Initial Parsers" (line 592): add
  `- FourseParser (catalog watch — Fourse, WooCommerce)`.
- §21 (after the Nuvemshop field table at lines 830–838): add the Fourse field table
  from the top of this plan, plus a sentence that Fourse exposes no `ld+json` and that
  stock comes from the WooCommerce `instock`/`outofstock` class token.
- **Drive-by fix:** §21 line 838 still specifies Nuvemshop `in_stock` as "absence of
  the `noStock` / 'Esgotado' out-of-stock markers" — exactly the rule trap 2 of #15
  proved wrong and which `parsers/nuvemshop.py` deliberately does not implement (it
  reads `ld+json` `offers.availability`). Correct the row to match the shipped code.

**`README.md`** (~lines 57–92) — add Fourse to the supported catalog-watch stores
alongside Shisui.

---

## Hard-constraint checklist (CLAUDE.md / FRD)

- [ ] One request per watch — page 1 only, no pagination, no parallelism.
- [ ] `REQUEST_DELAY_SECONDS` between entries (scanner-level, unchanged).
- [ ] HTTP 403/429 stops the cycle (scanner-level, unchanged).
- [ ] The parser writes nothing to disk and performs no I/O — pure `html -> list[Product]`.
- [ ] Raw `sqlite3` only; no schema change at all in this issue.
- [ ] Parser implements `can_handle` + `parse_catalog` and nothing else — no
      new/restock, storage, or notification logic (FRD §11).
- [ ] Log-and-continue for per-product failures; no exception escapes `parse_catalog`
      for a malformed block.
- [ ] Shipping is never parsed, stored, or compared.

## Acceptance (from issue #17)

- [ ] `can_handle` true for `fourse.com.br` + subdomains, false for `shisuistore.com.br`
      and `ligapokemon.com.br`; `NuvemshopParser.can_handle("…fourse.com.br…")` false.
- [ ] `parse_catalog(block_30_years.html)` returns exactly 1 product (document has 6
      `li.product` / 6 `/item/` links) with the verified id, name, url, price, stock.
- [ ] `parse_catalog(category_multi.html)` returns exactly the grid's products in page
      order, no phantoms.
- [ ] A sale product parses to the `<ins>` price, never `<del>`.
- [ ] `em-price-unit` does not corrupt the amount; installment and Pix prices are never
      picked.
- [ ] `outofstock` ⇒ `in_stock=False`; no stock token ⇒ `in_stock=False` + warning.
- [ ] Unparseable price ⇒ `price=None`, product kept; no name / no link ⇒ skipped + warning.
- [ ] No `#ecomus-shop-content` ⇒ `[]`, not an exception.
- [ ] No product sourced from `ul.search-products-suggest-list`; no document-rooted selector.
- [ ] End-to-end: the Fourse watch seeds silently on first scan, a later scan with a new
      product produces exactly one consolidated Discord message, and the Shisui watch in
      the same run is unaffected.

## Verification

```
pytest                      # full suite green, including the new parser module
python app.py               # one real cycle
```

After the first `python app.py`, confirm the watch seeded silently (no Discord
message) and that rows landed:

```sql
SELECT watch_id, product_id, product_name, price, in_stock
FROM collection_products
WHERE watch_id = (SELECT watch_id FROM catalog_watches ORDER BY first_scanned_at DESC LIMIT 1);
```

Expect **one row** for the `/block/` page — `17960`, price `99.99`, `in_stock = 1`.
Five rows would mean the phantom-listing trap is live. Run `python app.py` a second
time and confirm no Discord message and a refreshed `last_seen_at`.

## Suggested PR

Branch `feat/issue-17-fourse-parser` off `main`, PR title
`Add Fourse (WooCommerce) catalog parser (closes #17)`.
