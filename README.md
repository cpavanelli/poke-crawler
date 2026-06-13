# poke-crawler

**Pokémon Card Price Watcher** — a lightweight, single-run app that scans configured
[LigaPokemon](https://www.ligapokemon.com.br/) card and sealed-product pages, tracks the
lowest listing price per condition, stores price history plus an all-time-low baseline in
SQLite, and posts a Discord webhook when a new all-time-low is found.

It is designed to run unattended on a Raspberry Pi via cron: one process per invocation,
no daemon, SD-card-friendly. See [`FRD.md`](FRD.md) for the full specification — it is the
source of truth.

## Requirements

- Python 3.12+
- A Discord incoming webhook URL
- A host with an accurate clock (NTP) and correct local timezone — timestamps are stored
  in local time (FRD §16, §18)

## Install

```bash
git clone <repo-url> poke-crawler
cd poke-crawler

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

## Configure

Two files, both read relative to the working directory:

**1. `.env`** — secrets and tuning. Copy the template and fill it in:

```bash
cp .env.example .env
```

| Variable | Default | Notes |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | *(required)* | Discord incoming webhook. Missing/empty aborts startup. |
| `CARDS_CONFIG_PATH` | `cards.json` | Path to the card list. |
| `DATABASE_PATH` | `watcher.db` | SQLite file (created on first run). |
| `REQUEST_DELAY_SECONDS` | `30` | Polite delay between cards (FRD §4, §17). |
| `SPRITE_REQUEST_DELAY_SECONDS` | `2` | Delay before a sprite fetch on obfuscated listings. |
| `HTTP_TIMEOUT_SECONDS` | `20` | Per-request timeout. |
| `USER_AGENT` | `PokemonCardWatcher/1.0` | Sent on every request. |
| `SEND_INITIAL_BASELINE_NOTIFICATION` | `false` | Notify when first establishing a baseline. |
| `LOG_MAX_BYTES` | `1048576` | Rotating log size cap (FRD §16). |
| `LOG_BACKUP_COUNT` | `5` | Rotated log files to keep. |

> **There is no scan-interval setting.** The recheck cadence is the cron schedule on the
> host (see *Deploy*), not an in-app interval. The delays above only pace requests *within*
> a single scan.

**2. `cards.json`** — the cards and sealed products to watch. Card identity is `SHA256(url)`;
names are display-only.

```json
[
  {
    "name": "Mega Gengar",
    "conditions": ["NM", "SP"],
    "url": "https://www.ligapokemon.com.br/?view=cards/card&card=...&num=284"
  },
  {
    "name": "ETB - Ascended Heroes",
    "url": "https://www.ligapokemon.com.br/?view=prod/view&pcode=135115&prod=..."
  }
]
```

- **Card mode:** include a non-empty `conditions` array of valid acronyms
  (`M`, `NM`, `SP`, `MP`, `HP`, `D`). Each condition is tracked independently.
- **Sealed mode:** omit `conditions` (or leave it empty). The product is tracked as a single
  `SEALED` lowest price over factory-sealed (`L`) listings only (FRD §5).
- Unknown JSON keys are ignored for forward compatibility.

## Run

One scan cycle, guarded by a PID lock file (`watcher.lock`) so overlapping runs can't collide:

```bash
python app.py
```

The detailed run log is written to `logs/watcher.log` (rotating). Warnings and errors also go
to stderr. Configuration errors abort with a non-zero exit code; transient/per-item failures
are logged and the run continues (FRD §12).

Inspect every listing for a single URL without touching the database:

```bash
python tools/list_prices.py "<ligapokemon-url>"
```

## Test

```bash
pytest
```

## Deploy (Raspberry Pi, cron)

The app is a single-run process; cron provides the recheck cadence. Each tick runs one scan
and exits, so a crash never wedges future runs (the lock file reclaims stale PIDs — FRD §15).

**1. Host setup** (FRD §18):

```bash
sudo timedatectl set-timezone America/Sao_Paulo   # your local zone
timedatectl                                        # confirm NTP active + correct TZ
systemctl is-enabled cron                          # expect: enabled (so scans resume on boot)
```

**2. Add the cron entry** with `crontab -e` (adjust paths to your install):

```cron
*/30 * * * * cd /home/pi/poke-crawler && /home/pi/poke-crawler/.venv/bin/python app.py >> /home/pi/poke-crawler/logs/cron.log 2>&1
```

- `*/30` runs at :00 and :30. Use a divisor of 60 (`*/15`, `*/30`) for evenly spaced ticks.
- The `cd` is **required**: cron runs from `$HOME` with a minimal environment, and `.env`,
  `cards.json`, `watcher.db`, and `watcher.lock` are all resolved relative to the working
  directory. Without it the app would create a second database in the wrong place.
- Use the absolute path to the venv's `python` — cron does not activate the venv.
- `>> logs/cron.log 2>&1` captures failures that happen *before* logging starts (bad `.env`,
  missing venv, import error) and keeps cron from filling the local mail spool. Normal runs
  leave it nearly empty.

**3. Verify the cron path end-to-end:**

```bash
grep CRON /var/log/syslog | tail                   # cron fired the job
tail logs/cron.log                                 # no startup errors
sqlite3 watcher.db "select card_name, condition, scanned_at \
  from scan_results order by id desc limit 4;"     # fresh rows at the tick boundary
```

## How it works (brief)

- **No headless browser.** LigaPokemon embeds listing data as inline JS (`cards_stock` /
  `prod_stock`, `dataQuality`); the parser reads it from the raw HTML (FRD §10).
- **Obfuscated prices** (`precoCss`, `lj_tipo=15`) are decoded via a per-page CSS digit
  sprite, held in memory only and never written to disk (FRD §4). A decode failure skips just
  that listing and sends a Discord alert.
- **Polite by design:** one request at a time, sequential, with delays; stops the cycle on
  HTTP 403/429. No proxy rotation, CAPTCHA bypass, or automated login (FRD §14, §17).
- **SD-friendly storage:** SQLite with `journal_mode=WAL` and `synchronous=NORMAL`, size-capped
  rotating logs (FRD §8, §16).

See [`FRD.md`](FRD.md) for full detail and [`CLAUDE.md`](CLAUDE.md) for the hard constraints.
