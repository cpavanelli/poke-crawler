# Plan: Issue #15 — New-Product (Catalog) Watch · FRD §1, §3, §8, §11, §12, §19, §21

Add a **second monitor type** beside the price watcher: a **catalog watch** that
fetches a marketplace collection page, extracts the full set of products on it,
and posts **one consolidated Discord message** when a product is newly listed or
transitions sold-out → in-stock. First target: the Shisui Store pre-sale page
(`https://www.shisuistore.com.br/pre-venda/`), which runs on Nuvemshop /
Tiendanube.

Source of truth: `FRD.md` §3 (mandatory `type`), §8 (`collection_products`), §11
(catalog parser capability + `detect_changes`), §12 (invalid `type` aborts
startup), §19 (layout), §21 (the whole feature). **The FRD edits and the
`cards.json` migration to `"type": "price"` are already in the working tree** —
do not re-write them, just implement against them.

**Depends on (all merged):** #1 config/models, #2 storage, #5 fetcher, #6
notifier, #7 scanner, #10 parser split, #14 sealed products.

Match the conventions already established across the repo (read
`parsers/ligapokemon_parser.py`, `services/config.py`, `services/pricing.py`,
`services/storage.py`, `services/scanner.py`, `models/card.py` before writing):
`from __future__ import annotations`; FRD section refs in docstrings; typed
signatures; keyword-only args for multi-field calls; dependency injection so
tests run **offline**; raw `sqlite3` only; no new production deps
(`BeautifulSoup4` is already a dependency and is the right tool here).

---

## Verified against the live pages (2026-07-31) — read this before writing the parser

Both pages were fetched with a browser User-Agent: **HTTP 200, fully
server-rendered**, no JS/headless needed. Two traps were found that the issue
text does not capture — the parser must handle both.

**Trap 1 — a phantom `js-item-product` block.** `div.js-item-product` matches
**one extra element per page**: the quick-shop modal template, which has an
**empty `data-product-id`**, no name, no link, no price. On `/pre-venda/`:
2 matches, 1 real product. On `/produtos/`: 51 matches, **50 real products**
(all 50 have `.js-item-name`, `a.item-link[href]`,
`.js-price-display[data-product-price]`, and their own per-item ld+json).
⇒ **Skip any block whose `data-product-id` is missing or empty.**

**Trap 2 — the "Esgotado" label is *always rendered*.** It is hidden with an
inline style when the product is in stock:

```html
<div class="js-stock-label ... label" data-label="Esgotado"
     data-store="product-item-label-stock" style="display:none;">Esgotado</div>
```

On `/produtos/` **every one of the 50 in-stock products still contains the text
`Esgotado`**. A "does the block contain `Esgotado` / `noStock`" check — the rule
sketched in the issue — would classify **every product as sold out**, and then
every product would fire a false `RESTOCK` the first time the check was fixed.
⇒ **Do not use the label as the stock signal.**

**The reliable stock signal** is each item's own `ld+json` block (contrary to the
issue note, there is one *per product*, nested inside the `js-item-product`
div):

```json
"offers": { "@type": "Offer", "price": "99.99",
            "availability": "https://schema.org/OutOfStock" }
```

Verified: `/pre-venda/` item → `OutOfStock`; all 50 `/produtos/` items →
`InStock`. A secondary signal exists on the quick-shop container
(`data-variants` JSON, `"available": true/false`, `"stock": N`) and is a sound
fallback.

**Verified field map (use these, in this order):**

| Field | Source | Verified value (pre-venda item) |
|---|---|---|
| `product_id` | `data-product-id` on the block | `"357460215"` |
| `name` | `.js-item-name` text, stripped | `(PRÉ-VENDA) Box Coleção Ilustração Parceiro Inicial Série 3` |
| `url` | `a.item-link[href]` (absolute in the HTML) | `https://shisuistore.com.br/produtos/pre-venda-box-colecao-ilustracao-parceiro-inicial-serie-3/` |
| `price` | `.js-price-display[data-product-price]` — **integer cents** | `9999` → `99.99` (`/produtos/`: `499999` → `4999.99`) |
| `in_stock` | item ld+json `offers.availability` ends with `InStock` | `False` |

Take the price from `data-product-price` (cents), **not** from the `R$99,99`
text — no locale parsing, no ambiguity. Files are UTF-8; read fixtures with
`encoding="utf-8"`.

**Pagination:** neither page emitted a `?page=N` link (`/produtos/` rendered 50
items with no pagination control). Page 1 only is both the decision (§21) and
sufficient in practice.

---

## Decisions (locked — do not silently re-decide)

1. **`type` is mandatory** in `cards.json`; `"price"` | `"new_product"`; missing,
   empty, non-string, or unknown ⇒ `ConfigError` (aborts startup, §12). Unknown
   JSON *properties* are still ignored.
2. **Identity.** Watch identity = `SHA256(url)` (the existing `card_id_for`, §9).
   Product identity = `product_id` (store numeric ID) as **TEXT**; name, URL and
   price are display-only.
3. **Alerts:** NEW (unseen `product_id`, *even if sold out*) and RESTOCK (known
   product, stored `in_stock = 0`, now in stock). Nothing else alerts.
4. **First scan of a watch seeds silently** (record all, notify nothing). Seeding
   is keyed on a `catalog_watches` row, **not** on "no product rows stored":
   the pre-sale page is empty between drops, and inferring the seed from an
   empty product set would swallow the first product listed after an empty seed.
   *(Found by running the live page on 2026-07-31, when it had zero products.)*
5. **One consolidated webhook call per watch per scan**, only when there is at
   least one change.
6. **Rows are never deleted.** Disappear → reappear is not a new product.
7. **Stock must come from a structured signal** (ld+json availability, then
   `data-variants`), never from the visible/hidden "Esgotado" label — see Trap 2.
   A product whose stock state cannot be determined is **recorded as sold out**
   with a warning: sold-out is the state that never alerts on its own, so the
   product stays tracked instead of being dropped. (Decided with the user; the
   cost is that a signal that breaks and later recovers can read as a RESTOCK.)
8. **Notify before persisting** the new state, mirroring the price path
   (`notify_all_time_low` runs before `upsert_baseline`). A dropped webhook is
   swallowed (§12) in both modes; consistency beats a bespoke rollback.

---

## Scope boundary

**In scope:** `models/card.py`, `models/product.py` (new), `services/config.py`,
`parsers/base.py`, `parsers/nuvemshop.py` (new), `services/catalog.py` (new),
`services/storage.py`, `services/notifier.py`, `services/scanner.py`, `cards.json`,
`README.md`, plus tests and two HTML fixtures.

**Out of scope:** pagination; other Nuvemshop stores (host allowlist grows by one
line); price history / baselines for catalog products; `tools/list_prices.py`
(takes a URL argument, never reads `cards.json` — unaffected).

**Invariant:** price-mode behaviour is byte-for-byte unchanged. Every existing
test must stay green **without modification** — which is why the new `Card` field
gets a `"price"` default (see step 1).

---

## Step 1 — Mandatory `type` (`models/card.py`, `services/config.py`) · FRD §3, §12

**`models/card.py`.** Add one field plus module constants; keep the model
otherwise untouched so `card_id` (`SHA256(url)`, §9) is shared by both modes:

```python
TYPE_PRICE = "price"
TYPE_NEW_PRODUCT = "new_product"
VALID_TYPES = frozenset({TYPE_PRICE, TYPE_NEW_PRODUCT})

@dataclass(slots=True, frozen=True)
class Card:
    name: str
    conditions: tuple[str, ...]
    url: str
    is_sealed: bool = False
    entry_type: str = TYPE_PRICE      # FRD §3; "new_product" ⇒ catalog watch

    @property
    def is_catalog(self) -> bool:
        return self.entry_type == TYPE_NEW_PRODUCT
```

Field name `entry_type` (not `type`) to avoid shadowing the builtin at call
sites. The default keeps every existing `Card(...)` construction in tests and
`tools/` valid — **the JSON requirement is enforced in config, not the model.**

**`services/config.py` — `_parse_card`.** After name/url validation, read `type`
**before** the conditions logic:

```python
entry_type = entry.get("type")
if not isinstance(entry_type, str) or entry_type.strip().lower() not in VALID_TYPES:
    raise ConfigError(
        f"Card {name!r} must have a 'type' of {'price'!r} or {'new_product'!r}; got {entry_type!r}"
    )
```

- `new_product` ⇒ return `Card(name, conditions=(), url, is_sealed=False,
  entry_type=TYPE_NEW_PRODUCT)` immediately; `conditions`, if present, is ignored
  (§3) — do **not** validate it.
- `price` ⇒ the existing card/sealed branch, unchanged, with
  `entry_type=TYPE_PRICE`.

Update the `load_cards` / `_parse_card` docstrings to state the `type` rule.

**Tests (`tests/test_config.py`):**
- `"type": "price"` + `conditions` ⇒ card mode as today; `"type": "price"`
  without `conditions` ⇒ `is_sealed`; both `is_catalog is False`.
- `"type": "new_product"` ⇒ `is_catalog`, `conditions == ()`, no ConditionError
  even with a bogus `conditions` value present.
- Missing `type`, `""`, `"Price "`→ accepted (trim+lower), `"prices"`, `123`,
  `null` ⇒ `ConfigError`. Assert the error names the offending entry.
- Existing fixtures in the test suite must be updated to carry `"type"`, and
  `cards.json` (already migrated in the working tree) must load clean.

---

## Step 2 — `Product` model + catalog parser contract (`models/product.py`, `parsers/base.py`) · FRD §11

```python
# models/product.py
@dataclass(slots=True, frozen=True)
class Product:
    """One product on a watched collection page (FRD §21)."""
    product_id: str          # store id, identity (FRD §21)
    name: str                # display-only
    url: str                 # display-only
    price: float | None      # display-only; never feeds baselines (FRD §5)
    in_stock: bool
```

`parsers/base.py` gains a **sibling ABC** — do not add an abstract method to
`MarketplaceParser` (that would break `LigaPokemonParser`) and do not fold
catalog logic into the price contract (§11):

```python
class CatalogParser(ABC):
    @abstractmethod
    def can_handle(self, url: str) -> bool: ...
    @abstractmethod
    def parse_catalog(self, html: str) -> list[Product]:
        """Every product on page 1 of a collection page (FRD §21)."""
```

---

## Step 3 — `NuvemshopParser` (`parsers/nuvemshop.py`) · FRD §21

`can_handle`: hostname allowlist, same shape as `LigaPokemonParser.can_handle`
(`urlsplit(url).hostname`, exact or `.`-suffix match) against
`SUPPORTED_HOSTS = frozenset({"shisuistore.com.br"})`. Nuvemshop is multi-tenant
on custom domains, so host-sniffing the platform is impossible — document in the
docstring that a new Nuvemshop store is one entry in this constant.

`parse_catalog(html)` — BeautifulSoup, mode-agnostic, no I/O, no notification or
diff logic:

1. `soup.select("div.js-item-product")`.
2. **Skip blocks with a missing/blank `data-product-id`** (Trap 1). Skip
   silently — it is a template, not an error.
3. `name` ← `.js-item-name` text (`get_text(strip=True)`); missing/blank ⇒ skip
   the block with a `logger.warning`.
4. `url` ← `a.item-link[href]`, falling back to the first
   `a[href*="/produtos/"]`; `urljoin` it against a module-level base or leave as
   is when already absolute (verified absolute today; `urljoin` is the cheap
   guard). Missing ⇒ skip with a warning.
5. `price` ← `int(el["data-product-price"]) / 100` from
   `.js-price-display[data-product-price]`; unparseable/absent ⇒ `None` (price is
   display-only; never a reason to drop a product).
6. `in_stock` ← `_parse_stock(block)`:
   - primary: `json.loads` of the block's own
     `script[type="application/ld+json"]` → `offers.availability`; endswith
     `"InStock"` ⇒ `True`, endswith `"OutOfStock"` ⇒ `False`;
   - fallback: `json.loads` of `data-variants` on the quick-shop container ⇒
     `any(v.get("available") for v in variants)`;
   - neither available/parseable ⇒ **`False` (sold out)** with a
     `logger.warning` (decision 7).
7. Return the products in page order (deterministic message ordering).

Malformed *page-level* input (e.g. no `js-item-product` at all) returns `[]` —
that is not an exception; the scanner treats an empty catalog as "no changes".
Raise `ValueError` only if the HTML is not parseable at all.

**Fixtures** (`tests/fixtures/shisui/`), captured with a browser UA and trimmed
by hand to keep them small — **keep the modal `js-item-product` block and keep
an in-stock item's hidden `Esgotado` label**, they are the regression surface:

- `pre_venda.html` — the `/pre-venda/` page: 1 real product + the modal block.
- `produtos_multi.html` — a trimmed `/produtos/` page: 2–3 in-stock products
  (each still carrying the hidden `Esgotado` label) + the sold-out pre-venda
  product + the modal block.

```bash
python - <<'PY'
import httpx
for url, out in [("https://www.shisuistore.com.br/pre-venda/", "pre_venda.html"),
                 ("https://www.shisuistore.com.br/produtos/", "produtos_multi.html")]:
    r = httpx.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30, follow_redirects=True)
    r.raise_for_status()
    open(f"tests/fixtures/shisui/{out}", "w", encoding="utf-8").write(r.text)
PY
```

**Tests (`tests/test_nuvemshop_parser.py`):**
- `can_handle`: `https://www.shisuistore.com.br/pre-venda/` and the bare host ⇒
  `True`; `ligapokemon.com.br` and an unrelated host ⇒ `False`.
- `pre_venda.html` ⇒ exactly **one** `Product`:
  `product_id == "357460215"`, name as verified above, url ending
  `/produtos/pre-venda-box-colecao-ilustracao-parceiro-inicial-serie-3/`,
  `price == 99.99`, `in_stock is False`. (The count of 1 is the Trap-1
  regression.)
- `produtos_multi.html` ⇒ every in-stock product parses `in_stock is True`
  **even though its HTML contains the string `Esgotado`** — assert
  `"Esgotado" in html` in the same test so the trap is documented in code.
- A block missing `data-product-id` / name / link is skipped; a block with an
  unreadable price still yields a `Product` with `price is None`; a block with
  no stock signal at all yields `in_stock is False`.
- Empty/product-less HTML ⇒ `[]`.

---

## Step 4 — Diff reduction (`services/catalog.py`) · FRD §11, §21

Pure, marketplace-agnostic, no I/O — the sibling of `services/pricing.py`:

```python
CHANGE_NEW = "new"
CHANGE_RESTOCK = "restock"

@dataclass(slots=True, frozen=True)
class Change:
    kind: str          # CHANGE_NEW | CHANGE_RESTOCK
    product: Product

def detect_changes(
    current: Iterable[Product], known: Mapping[str, bool]
) -> list[Change]:
    """New and restocked products (FRD §21).

    `known` maps product_id -> the in_stock state stored for it. A product_id
    absent from `known` is NEW (even when sold out); a known product stored as
    sold-out and now in stock is RESTOCK. Nothing else is reported.
    """
```

`known` is deliberately `Mapping[str, bool]`, not storage rows — the reduction
stays independent of the DB. Output preserves `current` order.

**Tests (`tests/test_catalog.py`):** unseen id ⇒ NEW; unseen **and sold out** ⇒
NEW; known `False` → now `True` ⇒ RESTOCK; known `True` → now `False` ⇒ nothing;
unchanged ⇒ nothing; known id absent from `current` (disappeared) ⇒ nothing;
`known == {}` with several products ⇒ all NEW (the scanner, not this function,
suppresses the seed); mixed input ⇒ both kinds, in page order.

---

## Step 5 — Storage (`services/storage.py`) · FRD §8

Add `catalog_watches` and `collection_products` to the existing `init_db`
`executescript` block (`CREATE TABLE IF NOT EXISTS`), exactly as specified in
FRD §8 — `collection_products` keyed `(watch_id, product_id)` with
`in_stock INTEGER NOT NULL` and nullable `price`; `catalog_watches` keyed on
`watch_id` and carrying the seed/scan timestamps (decision 4).
Nothing about the three price tables changes; `connect()` already applies
`journal_mode=WAL` + `synchronous=NORMAL` to every connection.

Two helpers, in the module's existing style:

```python
def is_watch_seeded(conn, watch_id: str) -> bool:
    """Whether the watch has completed a scan before (FRD §21)."""

def mark_watch_scanned(conn, watch_id: str, *, now: str) -> None:
    """Record a completed scan, preserving first_scanned_at."""

def get_known_stock(conn, watch_id: str) -> dict[str, bool]:
    """product_id -> stored in_stock for one watch (FRD §21)."""

def upsert_collection_products(conn, watch_id: str, products: Sequence[Product], *, now: str) -> None:
    """Insert new products and refresh known ones. Never deletes (FRD §21)."""
```

The upsert is one `executemany` + one `commit()`:

```sql
INSERT INTO collection_products (
    watch_id, product_id, product_name, product_url, price, in_stock,
    first_seen_at, last_seen_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(watch_id, product_id) DO UPDATE SET
    product_name  = excluded.product_name,
    product_url   = excluded.product_url,
    price         = excluded.price,
    in_stock      = excluded.in_stock,
    last_seen_at  = excluded.last_seen_at
```

`first_seen_at` is **not** in the `DO UPDATE SET` list — that is what preserves
it across re-appearances. Store `int(product.in_stock)`; read back
`bool(row["in_stock"])`. No `DELETE` statement anywhere in this feature.

**Tests (`tests/test_storage.py`, `:memory:` connection as today):**
- `init_db` creates `collection_products` and is idempotent on a second call.
- Empty watch ⇒ `get_known_stock` returns `{}`.
- Seed 2 products ⇒ 2 rows, correct `first_seen_at`/`last_seen_at`, `in_stock`
  round-trips as `bool`, `price=None` stored as `NULL`.
- Re-upsert at a later `now` with a changed name/price/stock ⇒ still 2 rows,
  `first_seen_at` **unchanged**, `last_seen_at`/`price`/`in_stock` refreshed.
- A product omitted from the second upsert ⇒ its row survives untouched;
  re-including it later keeps the original `first_seen_at`.
- Two different `watch_id`s with the same `product_id` ⇒ independent rows.

---

## Step 6 — Notifier (`services/notifier.py`) · FRD §7, §21

One formatter + one send method, matching the existing pure-format / thin-method
split:

```python
STOCK_IN = "Em estoque"
STOCK_OUT = "Esgotado"
MAX_CHANGE_LINES = 20

def format_catalog_updates(*, watch_name: str, changes: Sequence[Change]) -> str: ...

class DiscordNotifier:
    def notify_catalog_updates(self, *, watch_name: str, changes: Sequence[Change]) -> bool: ...
```

Output (FRD §21):

```text
🆕 Shisui Pré-venda — 2 update(s)
• NEW: (PRÉ-VENDA) Box Coleção ... - R$99,99 - Esgotado - https://shisuistore.com.br/produtos/...
• RESTOCK: Booster Box Winterspell - R$899,99 - Em estoque - https://shisuistore.com.br/produtos/...
```

- Header count is `len(changes)`; the literal is `update(s)` regardless of count.
- Labels are `NEW:` / `RESTOCK:`; prices via the existing `format_brl`;
  `price is None` renders as `—`.
- **Discord caps a message at 2000 characters.** Emit at most
  `MAX_CHANGE_LINES` lines and append `• … and N more` when truncated; the
  header still reports the true total.
- Never called with an empty `changes` — assert/return early and send nothing.

**Tests (`tests/test_notifier.py`, existing spy client):** one NEW + one RESTOCK
⇒ **exactly one** `_send`/POST whose content matches the format above, both lines
present, header says `2 update(s)`; a `None` price renders `—`; 25 changes ⇒ 20
lines + `… and 5 more` and the payload stays under 2000 chars; shipping never
appears anywhere in the message (§5).

---

## Step 7 — Scanner wiring (`services/scanner.py`) · FRD §6, §21

Dispatch in `run()` on `entry.is_catalog`; keep `scan_card` untouched in name,
signature and behaviour, and add `scan_catalog(card)` beside it.

```python
DEFAULT_CATALOG_PARSERS: tuple[Callable[[], CatalogParser], ...] = (NuvemshopParser,)
```

Catalog parsers take no sprite fetcher and no sprite-error handler — keep the
factory type separate from `ParserFactory` rather than widening it.

`Scanner.__init__` gains `catalog_parsers: Sequence[...] | None = None`
(defaulting to `DEFAULT_CATALOG_PARSERS`) so tests can inject.

`run()`:

```python
outcome = self.scan_catalog(card) if card.is_catalog else self.scan_card(card)
```

Everything around it is already generic: `CycleStop` handling + `scan_errors`
row, `wait_between_cards()` between entries (§4/§17 — **do not** add a second
delay), the failure counters.

`scan_catalog(card)` — same skeleton as `scan_card`, same error taxonomy (§12):

1. select a catalog parser; none ⇒ `scan_errors(error_type="parse", "no parser
   for url")` and return an error outcome (reuse the existing `_select_parser`
   log/record shape; factor the shared bit if it is clean, otherwise a small
   sibling method is fine — no sprite callback here).
2. `html = self._fetcher.get_page(card.url)` — **one request, page 1 only, no
   `?page=N` follow-up** (§21). `CycleStop` propagates; `FetchError` ⇒
   `error_type="fetch"`, log and continue.
3. `products = parser.parse_catalog(html)`; exceptions ⇒ `error_type="parse"`,
   log and continue.
4. `known = storage.get_known_stock(conn, watch_id)` where `watch_id =
   card.card_id`.
5. **Seed:** `if not known:` → `upsert_collection_products(...)`,
   `logger.info("Seeded %s products for %s", ...)`, return an outcome with zero
   changes and **no notification**.
6. `changes = detect_changes(products, known)`.
7. `if changes:` → `self._notifier.notify_catalog_updates(watch_name=card.name,
   changes=changes)` (one call), then `upsert_collection_products(...)`
   (decision 8). No changes ⇒ log at debug/info, still upsert (it refreshes
   `last_seen_at`/price), send nothing.

Outcome/summary plumbing: add `catalog_changes: int = 0` to `CardOutcome` and
`catalog_updates: int = 0` to `ScanSummary` (defaults keep every existing
construction and assertion valid). Catalog entries have no `PriceResult`s, so
`results=()`, `new_lows=()`, `initial_baselines=()`.

**Tests (`tests/test_scanner.py`, existing offline harness — real parser, real
`sqlite3` `:memory:`, `httpx.MockTransport`, Discord spy):**
- First scan of a catalog watch over `pre_venda.html` ⇒ rows seeded, **zero**
  webhook calls, no `scan_errors`.
- Second scan of the same HTML ⇒ no webhook, `last_seen_at` refreshed.
- Second scan serving `produtos_multi.html` (new ids + the seeded product now
  in stock) ⇒ **exactly one** webhook call containing both a `NEW:` and a
  `RESTOCK:` line; rows updated; original `first_seen_at` preserved.
- In-stock → sold-out on a later scan ⇒ no webhook, `in_stock` row flips to 0.
- 403/429 on the catalog URL ⇒ `CycleStop`, cycle stops, `http_403` row written,
  later entries not scanned (reuse the existing assertion style).
- Fetch failure / parser exception ⇒ `scan_errors` row, no webhook, **cycle
  continues** to the next entry.
- Mixed list (a price card, a sealed product, a catalog watch) ⇒ all three
  processed in order, exactly one `get_page` per entry,
  `wait_between_cards` called `len(entries) - 1` times, and the existing
  price-mode assertions unchanged.

---

## Step 8 — Config sample + docs

- **`cards.json`:** append the live watch (the `"type": "price"` migration is
  already committed to the working tree):

```json
{
  "name": "Shisui Pré-venda",
  "type": "new_product",
  "url": "https://www.shisuistore.com.br/pre-venda/"
}
```

  Note in the PR body that `watch_id = SHA256(url)`, so editing this URL later
  (e.g. dropping `www.`) creates a **new** watch that silently re-seeds.
- **`README.md` §"2. `cards.json`"**: document `type` as required, list both
  values, add the `new_product` example, and state what a catalog watch alerts on
  (new listing / restock, batched, page 1 only). The existing card/sealed bullets
  become sub-bullets of `type: "price"`.

---

## Hard-constraint checklist (CLAUDE.md / FRD)

- ✅ Raw `sqlite3` only; `collection_products` created via `init_db`, WAL +
  `synchronous=NORMAL` from `connect()`.
- ✅ One request at a time, sequential; exactly one `get_page` per watch, page 1
  only; `REQUEST_DELAY_SECONDS` between entries via the existing
  `wait_between_cards()` — no new delay, no parallelism, no async.
- ✅ Nothing written to disk (no sprites, no HTML caching) — SD-friendly.
- ✅ Shipping never stored, compared or notified; catalog `price` is
  display-only and never feeds a baseline or `scan_results`.
- ✅ 403/429 stop the cycle; no proxy rotation, no login, no CAPTCHA handling.
- ✅ Identity is `SHA256(url)`; names display-only.
- ✅ Log-and-continue for per-entry failures; only invalid `type` aborts startup.
- ✅ No new dependency (`BeautifulSoup4`, `httpx`, `sqlite3`, `pytest` already
  present).

---

## Acceptance (from issue #15)

- ✅ `type` mandatory; migrated entries behave exactly as before (step 1).
- ✅ `new_product` entry loads as a catalog watch without `conditions` (step 1).
- ✅ `parse_catalog` returns one `Product` per real `js-item-product` with the
  verified id/name/url/price/stock; the sample parses as sold out (step 3).
- ✅ First scan seeds silently (step 7).
- ✅ Unseen `product_id` ⇒ NEW even when sold out (steps 4, 7).
- ✅ Sold-out → in-stock ⇒ RESTOCK; in-stock → sold-out ⇒ nothing (step 4).
- ✅ Multiple changes ⇒ a single consolidated message; no changes ⇒ no call
  (steps 6, 7).
- ✅ Rows never deleted; reappearing product is not re-reported (step 5).
- ✅ Page 1 only, one request per watch; price tables and behaviour unchanged
  (step 7 + invariant).

---

## Suggested PR

Single PR, `closes #15`, one commit per step (1–7, docs folded into 8 or the
final commit). `pytest` green with **no network and no real sleeps** — fixtures
only. Existing tests change **only** where a `cards.json`-shaped fixture needs
the new mandatory `"type"` key. PR body should reference the FRD sections already
updated in the working tree (§1, §3, §8, §11, §12, §19, §21) and call out the two
verified parser traps (phantom modal block, always-rendered `Esgotado` label).
