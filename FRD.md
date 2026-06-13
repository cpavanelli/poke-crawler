# Functional Requirements Document (FRD)

# Pokémon Card Price Watcher

Version: 1.0
Status: Approved for Development

---

# 1. Purpose

The Pokémon Card Price Watcher is a lightweight monitoring application that periodically scans configured Pokémon card marketplace pages, tracks the lowest available listing prices for selected card conditions, stores historical pricing data, and sends Discord notifications when a new all-time-low price is detected.

It also monitors **sealed products** (e.g. Elite Trainer Boxes), which are tracked as a single lowest price with no per-condition breakdown (see §5).

Initial implementation targets LigaPokemon.

The architecture must be extensible to support additional marketplaces in the future, such as MYP Cards.

---

# 2. Goals

- Monitor multiple card URLs.
- Support monitoring multiple conditions per card.
- Store historical pricing information.
- Maintain an all-time-low baseline per card condition.
- Notify Discord when a new all-time-low price is detected.
- Operate reliably on a Raspberry Pi.
- Use JSON configuration.
- Use SQLite without an ORM.
- Support future marketplace integrations.

---

# 3. Configuration

## Card Configuration

```json
[
  {
    "name": "Mega Gengar",
    "conditions": ["NM"],
    "url": "https://www.ligapokemon.com.br/?view=cards/card&card=Mega+Gengar+ex%20(284/217)&show=1&ed=ASC&num=284"
  },
  {
    "name": "Mega Charizard X",
    "conditions": ["NM", "SP"],
    "url": "https://www.ligapokemon.com.br/?view=cards/card&card=Mega+Charizard+X+ex%20(125/094)&show=1&ed=PFL&num=125"
  }
]
```

### Sealed Products

A sealed product is configured by **omitting the `conditions` array**. Its absence is the marker for sealed mode; no new field is introduced. A sealed product is tracked as a single lowest price (§5), so it has no conditions to list.

```json
[
  {
    "name": "ETB - Megaevolution Series - Ascended Heroes",
    "url": "https://www.ligapokemon.com.br/?view=prod/view&pcode=135115&prod=..."
  }
]
```

Validation:

- If `conditions` is present, it must be a non-empty array of valid card condition acronyms (card mode).
- If `conditions` is absent (or empty), the entry is a sealed product (sealed mode).
- Card identity is still `SHA256(url)` (§9), unaffected by mode.

Unknown JSON properties must be ignored for forward compatibility.

## Environment Configuration

```env
DISCORD_WEBHOOK_URL=

CARDS_CONFIG_PATH=cards.json
DATABASE_PATH=watcher.db

CHECK_INTERVAL_MINUTES=15
REQUEST_DELAY_SECONDS=30
SPRITE_REQUEST_DELAY_SECONDS=2

HTTP_TIMEOUT_SECONDS=20

USER_AGENT=PokemonCardWatcher/1.0

SEND_INITIAL_BASELINE_NOTIFICATION=false

LOG_MAX_BYTES=1048576
LOG_BACKUP_COUNT=5
```

---

# 4. Scheduling

## Recheck Timer

The entire card list is scanned every CHECK_INTERVAL_MINUTES.

## Request Delay Timer

A delay of REQUEST_DELAY_SECONDS is applied between cards.

Example:

Card A
→ Wait 30 seconds
Card B
→ Wait 30 seconds
Card C

## Intra-Card Sprite Delay

When a card page contains obfuscated prices (`precoCss`), a second request is needed to fetch the digit sprite. A short delay of SPRITE_REQUEST_DELAY_SECONDS is applied between the page request and the sprite request.

Example:

Fetch page HTML
→ precoCss detected → Wait 2 seconds → Fetch sprite
→ no precoCss → no extra request

## Sprite Memory Policy

The digit sprite is held in memory only for the duration of a single card parse and never written to disk. It is loaded via `io.BytesIO` directly from the HTTP response bytes and discarded after decoding. This avoids unnecessary SD card write cycles on Raspberry Pi.

---

# 5. Price Rules

## Lowest Price Definition

Lowest price means:

- Listing price only.
- Shipping is ignored.
- Shipping is never stored.
- Shipping is never included in comparisons.
- Shipping is never included in notifications.

## Condition Tracking

Conditions are tracked independently.

Example:

NM = R$1.250,00
SP = R$950,00

## Sealed Products

A sealed product (configured by omitting `conditions`, §3) is tracked as a **single lowest price**, not per condition. However, "ignore conditions" does not mean "ignore quality entirely": a sealed-product page mixes genuinely sealed/new boxes with the occasional used or defective unit, and the cheapest listing is frequently a damaged one. Surfacing that as the product's price would be misleading.

Therefore the lowest sealed price is the minimum listing price over only the **factory-sealed** listings:

- **Included** quality acronym: `L` (Lacrado / sealed) only.
- **Excluded:** every other acronym — `A` (Aberto), `N` (Novo), `NEA` (Novo com embalagem aberta), `NSA` (Novo sem embalagem), `U` (Usado), `D` (Com defeito / avaria). Only an unambiguously factory-sealed box counts; "new", "opened", "no packaging", used, and damaged are all excluded.

These acronyms come from the sealed-product `dataQuality` map (§10), which is a different set from the card conditions.

The result is stored, compared, and notified under a single synthetic condition label, `SEALED`, so the existing `(card_id, condition)` storage schema (§8) and Discord format (§7) are reused unchanged. Shipping is still ignored (listing price only). If a sealed page yields no included listing (all used/defective, or all sprite decodes failed), there is no result for that product on that scan — log and continue, exactly like "no matching condition" (§12).

---

# 6. Scan Workflow

For each configured card:

1. Load configuration.
2. Fetch page.
3. Parse listings.
4. Filter by configured conditions.
5. Determine lowest price per condition.
6. Store scan history.
7. Compare against baseline. 
8. Send notification if a new all-time-low exists.
9. Update baseline.
10. Wait request delay.

Step 2 uses the shared HTTP fetcher (§13, §17). Step 3 is the parser's
`parse_listings(html)` (every listing, all conditions); steps 4–5 are the
marketplace-agnostic `lowest_prices(listings, conditions)` reduction (§11).

For a **sealed product**, steps 4–5 instead use the sealed reduction (§5, §11):
filter to the included sealed/new acronyms and take a single lowest price under
the `SEALED` label. Steps 6–9 are identical in both modes.

---

# 7. Notifications

## New All-Time-Low

Notification is sent only when:

Current Price < Stored Baseline

Example:

Stored baseline: R$500

Current scan: R$490

Result:

- Send notification
- Update baseline

## Discord Format

```text
[card name] - [condition] - [price found] - Previous lowest: [latest lowest price] - [url]
```

Example:

```text
Mega Charizard X - NM - R$1.250,00 - Previous lowest: R$1.350,00 - https://...
```

## Initial Baseline

When SEND_INITIAL_BASELINE_NOTIFICATION=true:

```text
Mega Gengar - NM - R$500,00 - Initial baseline - https://...
```

## Sprite Decode Alert

When a `precoCss` listing cannot be decoded (see §10), a Discord alert is sent so the breakage is noticed:

```text
⚠️ Sprite decode failed - [card name] - [url] - listing skipped
```

This is informational only; it does not affect baselines or scan history.

---

# 8. Database

Technology:

- SQLite
- Python sqlite3

No ORM.

## Durability Settings (Raspberry Pi)

To minimise SD card writes and survive unexpected power loss, the SQLite connection is opened with:

- `journal_mode = WAL` — fewer fsyncs and safer crash recovery than the default rollback journal.
- `synchronous = NORMAL` — durable under application crashes, with far fewer disk syncs than `FULL`.

These pragmas are applied on every connection open.

## price_baselines

```sql
CREATE TABLE price_baselines (
    card_id TEXT NOT NULL,
    card_name TEXT NOT NULL,
    url TEXT NOT NULL,
    condition TEXT NOT NULL,
    lowest_price REAL NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(card_id, condition)
);
```

## scan_results

```sql
CREATE TABLE scan_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id TEXT NOT NULL,
    card_name TEXT NOT NULL,
    url TEXT NOT NULL,
    condition TEXT NOT NULL,
    lowest_price REAL NOT NULL,
    scanned_at TEXT NOT NULL
);
```

## scan_errors

```sql
CREATE TABLE scan_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id TEXT,
    url TEXT NOT NULL,
    error_type TEXT NOT NULL,
    error_message TEXT,
    occurred_at TEXT NOT NULL
);
```

---

# 9. Card Identification

Internal identifier:

```text
card_id = SHA256(url)
```

Names are display-only metadata.

---

# 10. LigaPokemon HTML Parsing

## Page Fetch

A standard HTTP GET with a browser-like `User-Agent` header returns a valid 200 response. No JavaScript execution or headless browser is needed.

## Data Location

All listing data is embedded in the raw HTML as inline JavaScript variables, not as pre-rendered HTML. The `#marketplace-stores` div is empty on arrival and populated client-side by `mpcard.init()`.

The relevant variables injected into the page are:

| Variable | Content |
|---|---|
| `cards_stock` | Array of individual listings (price, condition, store ref) |
| `cards_stores` | Object keyed by store ID with store name and location |
| `dataQuality` | Array mapping condition IDs to condition labels |

### Sealed-product pages (`view=prod/view`)

Sealed products use the `view=prod/view&pcode=...` page layout instead of `view=cards/card`. The data shape is identical, but the listing and store arrays are named **`prod_stock`** and **`prod_stores`** (same fields, including `precoFinal` / `precoCss` / `qualid` / `lj_id`). `dataQuality` is still present and is still parsed dynamically from the page.

The parser must read `prod_stock` / `prod_stores` when present and fall back to `cards_stock` / `cards_stores` otherwise. No other parser logic changes: `precoFinal`, the `precoCss` sprite-decode path (below), and the dynamic `dataQuality` map all work unchanged.

The sealed-product `dataQuality` uses a **different acronym set** from cards (the parser maps it dynamically, so this is not hardcoded):

| qualid | acron | label |
|---|---|---|
| 1 | A | Aberto |
| 2 | L | Lacrado |
| 3 | N | Novo |
| 4 | NEA | Novo com embalagem aberta |
| 5 | NSA | Novo sem embalagem |
| 6 | U | Usado |
| 7 | D | Com defeito / avaria |

Which of these count toward the lowest sealed price is defined in §5 (included: `L` only).

## Extracting Listings

Parse `cards_stock` from the raw HTML using a regex or string search for `var cards_stock = `. Each entry contains:

| Field | Description |
|---|---|
| `precoFinal` | Listing price as a decimal string (e.g. `"1700.00"`) |
| `precoCss` | Present instead of `precoFinal` when price is obfuscated (see below) |
| `qualid` | Condition ID — resolve via `dataQuality` |
| `lj_id` | Store ID — resolve via `cards_stores` |

## Condition Mapping

Parse `dataQuality` from the same page. Map `qualid` to the `acron` field:

| qualid | acron | label |
|---|---|---|
| 1 | M | Nova |
| 2 | NM | Praticamente Nova |
| 3 | SP | Usada Levemente |
| 4 | MP | Usada Moderadamente |
| 5 | HP | Muito Usada |
| 6 | D | Danificada |

## Obfuscated Prices (`precoCss`)

Some listings (identified by `lj_tipo=15`) do not include `precoFinal`. Instead they carry a `precoCss` field containing a semicolon-separated list of CSS class groups, one per digit. The price is rendered visually via a CSS sprite.

This is an active anti-scraping measure. Both the CSS class names and the sprite image are **randomised on every page load** — they cannot be cached or reused across requests.

Decoding algorithm (must be performed against a single page load):

1. Fetch the page and hold the full HTML response in memory.
2. Extract `precoCss` from `cards_stock` in that response.
3. Parse all inline `<style>` blocks from that same response. Build a map of `class → background-position`.
4. Extract the digit sprite URL from the `background-image` rule referencing `/imgnum/` in the same inline CSS.
5. Download that sprite (same session/headers).
6. For each semicolon-separated group in `precoCss`:
   - If the group is `V`, it is the decimal separator (`,`).
   - Otherwise, find the class in the group that exists in the position map, read its `(x, y)` background-position, and crop an 8×21px region from the sprite at that offset to identify the digit.
7. Concatenate the resolved digits around the separator to form the final price string.

Listings without `precoCss` (the majority) require only step 2 — read `precoFinal` directly.

## Sprite Decode Failure

The sprite decoder is the most fragile part of the system: both the CSS class names and the sprite image are randomised per page load, and the site changes this anti-scrape mechanism without notice. When a `precoCss` listing cannot be decoded (missing style map entry, sprite download failure, unrecognised digit crop):

1. Skip that individual listing — do not let it abort the card or the cycle.
2. Continue evaluating the remaining listings for the card (a decodable listing may still yield a valid lowest price).
3. Record the failure in `scan_errors` with `error_type = sprite_decode`.
4. Send a Discord alert so the decoder breakage is visible and can be fixed promptly (see §7).

A sprite decode failure is therefore a per-listing skip, not a per-card parser failure.

---

# 11. Marketplace Architecture

## Parser Interface

A marketplace parser is responsible only for **extraction** — turning a page's
raw HTML into the full set of individual listings. It does not know about
configured conditions, baselines, or the lowest-price reduction.

```python
can_handle(url) -> bool
parse_listings(html) -> list[Listing]
```

`parse_listings` returns every listing on the page (all conditions, unfiltered),
each as a `Listing(condition, price)` where `price` is the listing price only
(§5). For LigaPokemon this includes decoding obfuscated `precoCss` listings
(§10); a sprite-decode failure skips the affected listing and is reported at
most once per page.

Example `parse_listings` output:

```json
[
  {"condition": "NM", "price": 843.00},
  {"condition": "NM", "price": 934.15},
  {"condition": "SP", "price": 1200.00}
]
```

## Lowest-Price Reduction

Reducing the listings to the lowest price per configured condition is **not**
marketplace-specific, so it lives outside the parser hierarchy as a pure
function:

```python
lowest_prices(listings, conditions) -> list[PriceResult]
```

The scanner composes the two — `parse_listings(html)` then
`lowest_prices(listings, card.conditions)`. A debugging/inspection tool
(`tools/list_prices.py`) calls `parse_listings` on its own to print every
listing for a URL.

For sealed products the reduction differs but is still marketplace-agnostic and
parser-independent — the parser still returns every listing with its raw acronym
(§10). A sibling pure function reduces those to one sealed price:

```python
lowest_sealed_price(listings) -> PriceResult | None
```

It keeps only the included sealed/new acronyms (§5), takes the minimum listing
price, and returns a single `PriceResult(condition="SEALED", ...)`, or `None`
when no included listing exists. The scanner branches on the card's mode (§3):
card mode calls `lowest_prices`, sealed mode calls `lowest_sealed_price`. The
parser does **not** branch — page-type detection (`prod_stock` vs `cards_stock`)
lives in extraction (§10), and sealed-vs-card filtering lives in the reduction.

Example `lowest_prices` output (conditions = `["NM", "SP"]`):

```json
[
  {"condition": "NM", "lowest_price": 843.00},
  {"condition": "SP", "lowest_price": 1200.00}
]
```

## Initial Parsers

Version 1:

- LigaPokemonParser

Future:

- MypCardsParser

---

# 12. Error Handling

Supported scenarios:

- Timeout
- Network failure
- Parser failure
- Sprite decode failure
- Invalid configuration
- Discord failure
- No matching condition
- HTTP 403
- HTTP 429

Behavior:

| Error | Action |
|---------|---------|
| Timeout | Log and continue |
| Parser failure | Log and continue |
| Sprite decode failure | Skip listing, log to scan_errors, send Discord alert, continue |
| Invalid config | Abort startup |
| Discord failure | Log and continue |
| 403 | Stop current cycle |
| 429 | Stop current cycle |

---

# 13. Retry Policy

Maximum attempts:

2

Flow:

Attempt 1
→ Failure
→ Wait 5 seconds
→ Attempt 2
→ Failure
→ Log error

---

# 14. Concurrency

- One request at a time.
- No parallel execution.
- Sequential processing only.

---

# 15. Lock File

File:

```text
watcher.lock
```

The lock file contains the PID of the process that created it.

Behavior on startup:

- If the lock does not exist, create it (write current PID) and continue.
- If the lock exists, read the stored PID:
  - If a process with that PID is still alive, another run is in progress — exit.
  - If no process with that PID is alive, the lock is stale (left behind by a crash, reboot, or power loss). Overwrite it with the current PID and continue.
- Remove the lock on normal shutdown.

This prevents a killed run from permanently wedging all future cron invocations, since the lock would otherwise never be removed.

---

# 16. Logging

Log:

- Startup
- Shutdown
- Successful scans
- Baseline creation
- New all-time-lows
- Notification results
- Errors
- Retries

## Log Rotation

Logs are written via a size-capped rotating file handler so they can never fill the SD card:

- `LOG_MAX_BYTES` — maximum size of a single log file before rotation (default 1 MiB).
- `LOG_BACKUP_COUNT` — number of rotated files to retain (default 5).

Older files beyond the backup count are deleted automatically.

## Timestamps

All stored and logged timestamps use the host's local timezone (see §18). The deployment assumes NTP keeps the system clock accurate.

---

# 17. Anti-Abuse Rules

- One request at a time.
- Respect delays.
- Use User-Agent.
- Apply intra-card sprite delay when a second request is required.
- No proxy rotation.
- No CAPTCHA bypass.
- No automated login.
- Stop cycle after 403.
- Stop cycle after 429.

---

# 18. Deployment

Target:

- Raspberry Pi

Execution model:

- Cron
- Single-run application

Example:

```cron
*/15 * * * * cd /home/pi/card-watcher && /home/pi/card-watcher/.venv/bin/python app.py
```

## Host Requirements

The Raspberry Pi host is assumed to provide:

- **NTP time sync enabled** (`timedatectl` / `systemd-timesyncd`). Timestamps are stored in local time, so an accurate clock and a correctly configured local timezone are required.
- **Local timezone configured** for the Pi (e.g. via `raspi-config` or `timedatectl set-timezone`).
- **cron started on boot** (default on Raspberry Pi OS) so scans resume automatically after a reboot or power loss. No long-lived daemon is used; each cron invocation is a single-run process guarded by the lock file (§15).

---

# 19. Project Structure

```text
card-watcher/
│
├── cards.json
├── .env
├── watcher.db
│
├── app.py
│
├── parsers/
│   ├── base.py
│   ├── ligapokemon_parser.py
│   ├── sprite_decoder.py
│   └── mypcards.py
│
├── services/
│   ├── scanner.py
│   ├── notifier.py
│   ├── storage.py
│   ├── fetcher.py
│   ├── pricing.py
│   └── config.py
│
├── models/
│   ├── card.py
│   ├── listing.py
│   └── price_result.py
│
├── tools/
│   └── list_prices.py
│
└── logs/
```

---

# 20. Technology Stack

- Python 3.12+
- httpx
- BeautifulSoup4
- sqlite3
- python-dotenv
- Discord Webhooks
- pytest

---

# 21. Future Enhancements

- MYP Cards support
- Telegram notifications
- Price charts
- Daily summaries
- Health checks
- Web dashboard
- Docker deployment
- Additional marketplaces
- Target price alerts
- Enable/disable flags per card
