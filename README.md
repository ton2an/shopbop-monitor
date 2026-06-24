<h1 align="center">🛍️ shopbop-monitor</h1>

<p align="center">
  <em>Get a phone push the moment a brand drops something new on Shopbop — or when an item you want comes back in stock.</em>
</p>

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/python-3.10%2B-blue.svg">
  <img alt="License" src="https://img.shields.io/badge/license-MIT-green.svg">
  <img alt="Notifications" src="https://img.shields.io/badge/push-ntfy.sh-brightgreen.svg">
  <img alt="Dependencies" src="https://img.shields.io/badge/deps-curl__cffi-orange.svg">
</p>

---

A tiny, no-frills watcher for [Shopbop](https://www.shopbop.com). Point it at the
brands and items you care about; it polls Shopbop's public JSON API on a schedule
and sends you a [ntfy](https://ntfy.sh) push when something changes.

It alerts on two things, since the previous run:

- 🆕 **New product** — a brand you follow lists something it didn't have before.
- 🔄 **Back in stock** — a product (or a specific color) flips out-of-stock → in-stock.

No account, no API key, no scraping of HTML — it uses the same public
`api.shopbop.com` endpoint the website's own search calls, sorted newest-first.

## ✨ Features

- **Two watch modes** — follow whole **brands**, or specific **items** by description.
- **Fuzzy item matching** — don't know the exact product name? Describe it
  (`Maison Margiela | tabi ballet flats`) and it finds the match. Preview with `--check`.
- **Designers only** — keeps *exact* designer matches, so resale sellers
  (e.g. *Shopbop Archive*) and diffusion lines (e.g. *MM6*) are filtered out.
- **Men's & women's** — per-watch `MENS` / `WOMENS` department.
- **No first-run flood** — brand watches seed silently; item watches send one
  verification push so you can confirm the match is right.
- **Color-level restocks** — notices when a single color comes back, not just the whole product.
- Tiny footprint: one Python file, one dependency, JSON state on disk.

## 📲 What an alert looks like

```
🆕 Lemaire: Croissant Leather Bag
   Lemaire · Croissant Leather Bag
   $1,490.00
   https://www.shopbop.com/...

🔄 Back in stock — Maison Margiela: Tabi Ballerina Flats
   Maison Margiela · Tabi Ballerina Flats
   $1,550.00 · back in stock
   https://www.shopbop.com/...
```

## 🚀 Quick start

```bash
git clone https://github.com/ton2an/shopbop-monitor.git
cd shopbop-monitor

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env            # set your NTFY_TOPIC
cp brands.example.txt brands.txt
cp items.example.txt  items.txt # optional

# Subscribe to your topic in the ntfy app (iOS/Android) or https://ntfy.sh/<topic>
.venv/bin/python monitor.py --test-ntfy   # confirm pushes arrive
.venv/bin/python monitor.py --check       # preview your item matches
.venv/bin/python monitor.py               # first run seeds; later runs alert
```

> ⚠️ Your ntfy topic acts like a password — anyone who knows it can read your
> notifications. Keep it secret and add a random suffix. `.env`, `brands.txt`,
> and `items.txt` are gitignored so your config never lands in the repo.

## ⚙️ Configuration

### `brands.txt` — whole-brand watches

```
Lemaire
Acne Studios
Our Legacy | MENS          # optional dept; default is WOMENS
```

### `items.txt` — targeted item watches

```
Maison Margiela | tabi ballet flats
Acne Studios | 1996 sprayed long sleeve | MENS
```

Format: `Brand | words you remember | [MENS|WOMENS]`. Matching is fuzzy — each
word you type counts if it appears in the product name (with typo tolerance),
and words you omit don't hurt. An item watch that matches nothing today will
still alert the instant a matching product appears.

### `.env`

```
NTFY_TOPIC=your-shopbop-topic-abc123
```

## 🖥️ Commands

| Command | What it does |
| --- | --- |
| `monitor.py` | One pass: detect changes and send pushes. |
| `monitor.py --check` | Preview what your item queries match (scores + stock). No state change. |
| `monitor.py --dry-run` | Compute and print alerts without sending or saving state. |
| `monitor.py --seed` | Baseline state for all watches without alerting (run after editing configs). |
| `monitor.py --test-ntfy` | Send a single test push and exit. |

## ⏰ Running on a schedule

Hourly via cron (invoking the venv's Python directly):

```cron
30 * * * * cd /path/to/shopbop-monitor && .venv/bin/python3 -u monitor.py >> logs/monitor.log 2>&1
```

Or use the bundled `run.sh`, which activates the venv (if present) and logs for you:

```cron
30 * * * * /path/to/shopbop-monitor/run.sh
```

State lives in `state/state.json`; output in `logs/monitor.log`. If a fetch
fails (network/HTTP error), that brand is skipped for the run and its state is
preserved, so a blip never turns into a flood of false "new product" alerts.

## 🔧 Tuning

Constants at the top of `monitor.py`:

| Name | Default | Meaning |
| --- | --- | --- |
| `MATCH_MIN` | `0.6` | Fuzzy-match threshold for item watches (0–1). |
| `MAX_PRODUCTS` | `600` | Cap on products fetched per brand per run. |
| `MAX_ALERTS` | `30` | Cap on pushes per run; extras are summarized into one. |
| `REQUEST_GAP` | `0.6` | Politeness delay (seconds) between API calls. |

## 📄 License

MIT — see [LICENSE](LICENSE).

> Not affiliated with or endorsed by Shopbop. Uses a public endpoint for
> personal, non-commercial monitoring; please be polite with request volume.
