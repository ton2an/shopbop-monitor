#!/usr/bin/env python3
"""
Shopbop brand & item monitor.

Watches Shopbop via their public JSON API and sends a ntfy push when, since the
previous run:
  • a NEW product appears from a watched brand/item, or
  • a product/color flips OUT-OF-STOCK -> IN-STOCK (back in stock).

Two kinds of watch:
  brands.txt   whole-brand watches — alert on ANY new product or restock.
  items.txt    targeted item watches — "Brand | words you remember". Uses fuzzy
               matching so you don't need Shopbop's exact product name.

State is kept per-watch in state/state.json. A brand watch's first run seeds
silently (no flood). An item watch's first run sends ONE verification push so
you can confirm the fuzzy match caught the right thing.

Config:
  brands.txt   one brand per line. Optional "| siteId" (default 1006 = women's).
  items.txt    "Brand | search words". Optional trailing "| siteId".
  .env         NTFY_TOPIC=...   (the ntfy.sh topic to publish to)

Usage:
  python3 monitor.py             # one pass, send alerts
  python3 monitor.py --check     # preview what your item queries match (no state change)
  python3 monitor.py --dry-run   # compute alerts, print, don't send / don't save
  python3 monitor.py --seed      # save state for all watches, never alert
  python3 monitor.py --test-ntfy # send a single test push and exit
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from curl_cffi import requests as cf

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
ROOT        = Path(__file__).parent
BRANDS_PATH = ROOT / "brands.txt"
ITEMS_PATH  = ROOT / "items.txt"
STATE_PATH  = ROOT / "state" / "state.json"
ENV_PATH    = ROOT / ".env"

API          = "https://api.shopbop.com/public/search"
SITE_URL     = "https://www.shopbop.com"
IMG_BASE     = "https://m.media-amazon.com/images/G/01/Shopbop/p"
SITE_ID      = "1006"          # Shopbop US
DEPTS_ALL    = ("MENS", "WOMENS")   # searched by default (covers co-ed/unisex)
PAGE_LIMIT   = 100             # products per API page
MAX_PRODUCTS = 600             # safety cap per brand per run
MAX_ALERTS   = 30              # cap pushes per run; extras are summarized
REQUEST_GAP  = 0.6             # seconds between API calls (be polite)
MATCH_MIN    = 0.6             # fuzzy match threshold for item watches

HEADERS = {"Client-Id": "Browser", "accept": "application/json"}
SESSION = cf.Session(impersonate="chrome124")

STOPWORDS = {"the", "a", "an", "and", "with", "in", "of", "for"}


def load_env() -> None:
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# Config files
# --------------------------------------------------------------------------- #
def _parse_line(line: str) -> list[str]:
    return [p.strip() for p in line.split("|")]


def _depts(parts: list[str], idx: int) -> tuple[str, ...]:
    """Department(s) for a config line. Explicit MENS/WOMENS restricts to that;
    omitted means BOTH (so co-ed/unisex items are never missed)."""
    if len(parts) > idx and parts[idx]:
        d = parts[idx].strip().upper()
        if d in ("WOMENS", "MENS"):
            return (d,)
    return DEPTS_ALL


def load_brands() -> dict[str, tuple[str, ...]]:
    """Returns {brand: (depts...)} — lines for the same brand are merged."""
    if not BRANDS_PATH.exists():
        return {}
    acc: dict[str, set[str]] = {}
    for raw in BRANDS_PATH.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = _parse_line(line)
        acc.setdefault(parts[0], set()).update(_depts(parts, 1))
    return {k: tuple(sorted(v)) for k, v in acc.items()}


def load_items() -> list[tuple[str, str, tuple[str, ...]]]:
    """Returns [(brand, query, depts)] — same (brand, query) lines merged."""
    if not ITEMS_PATH.exists():
        return []
    acc: dict[tuple[str, str], set[str]] = {}
    order: list[tuple[str, str]] = []
    for raw in ITEMS_PATH.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = _parse_line(line)
        if len(parts) < 2 or not parts[1]:
            log(f"items.txt: skipping malformed line (need 'Brand | words'): {line!r}")
            continue
        key = (parts[0], parts[1])
        if key not in acc:
            order.append(key)
        acc.setdefault(key, set()).update(_depts(parts, 2))
    return [(b, q, tuple(sorted(acc[(b, q)]))) for (b, q) in order]


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #
def _norm(s: str) -> str:
    # Fold accents (Totême -> toteme) then strip non-alphanumerics.
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _tokens(s: str) -> list[str]:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    toks = re.findall(r"[a-z0-9]+", s)
    return [t for t in toks if t not in STOPWORDS and len(t) > 1]


def match_score(query: str, name: str) -> float:
    """0..1: fraction of query words present in the product name.

    Each query word scores 1 if it appears in the name, or its best per-word
    similarity if that's >= 0.8 (typo/inflection tolerance, e.g. jean/jeans).
    Words you didn't type don't penalize — so "riley jeans" still matches
    "Riley High Rise Straight Crop Jeans".
    """
    q, n = _tokens(query), _tokens(name)
    if not q:
        return 0.0
    nset = set(n)
    total = 0.0
    for qt in q:
        if qt in nset:
            total += 1.0
        else:
            best = max((difflib.SequenceMatcher(None, qt, nt).ratio() for nt in n),
                       default=0.0)
            total += best if best >= 0.8 else 0.0
    return total / len(q)


def brand_matches(want: str, designer: str) -> bool:
    # Exact (accent/case/punctuation-insensitive) so we keep only the real
    # designer label — excludes resellers (Shopbop Archive, What Goes Around…)
    # and diffusion lines (e.g. MM6 vs Maison Margiela).
    return _norm(want) == _norm(designer)


# --------------------------------------------------------------------------- #
# Fetch
# --------------------------------------------------------------------------- #
def fetch_brand(name: str, dept: str) -> dict[str, dict]:
    """Return {productCode: {...}} for products whose designer is exactly `name`."""
    products: dict[str, dict] = {}
    offset = 0
    while offset < MAX_PRODUCTS:
        url = (f"{API}?q={urllib.parse.quote(name)}&siteId={SITE_ID}&dept={dept}"
               f"&lang=en-US&currency=USD&sort=priority"
               f"&limit={PAGE_LIMIT}&offset={offset}")
        try:
            r = SESSION.get(url, headers=HEADERS, timeout=30)
            if r.status_code != 200:
                log(f"  {name}: HTTP {r.status_code} at offset {offset}")
                break
            data = r.json()
        except Exception as e:
            log(f"  {name}: fetch error {e!r}")
            break

        batch = data.get("products", [])
        if not batch:
            break

        for entry in batch:
            p = entry.get("product", {})
            if not brand_matches(name, p.get("designerName", "")):
                continue
            code = p.get("productCode")
            if not code:
                continue
            colors = {c.get("colorCode"): bool(c.get("inStock"))
                      for c in p.get("colors", []) if c.get("colorCode")}
            img = ""
            try:
                img = IMG_BASE + p["colors"][0]["images"][0]["src"]
            except (KeyError, IndexError):
                pass
            products[code] = {
                "name":     (p.get("shortDescription") or "").strip(),
                "designer": p.get("designerName", ""),
                "url":      SITE_URL + p.get("productDetailUrl", ""),
                "img":      img,
                "price":    (p.get("retailPrice") or {}).get("price", ""),
                "inStock":  bool(p.get("inStock")),
                "colors":   colors,
            }

        total = data.get("totalResults", 0)
        offset += PAGE_LIMIT
        if offset >= total:
            break
        time.sleep(REQUEST_GAP)

    return products


def filter_items(products: dict[str, dict], query: str) -> dict[str, dict]:
    """Keep products whose name fuzzily matches the query, tagged with score."""
    out: dict[str, dict] = {}
    for code, p in products.items():
        sc = match_score(query, p["name"])
        if sc >= MATCH_MIN:
            pp = dict(p)
            pp["score"] = round(sc, 2)
            out[code] = pp
    return out


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except json.JSONDecodeError:
            log("State file corrupt; starting fresh.")
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=1, sort_keys=True))


# --------------------------------------------------------------------------- #
# Diff
# --------------------------------------------------------------------------- #
def diff(prev: dict, cur: dict) -> list[dict]:
    """Return [{kind:'new'|'restock', p, detail}] comparing prev->cur."""
    alerts: list[dict] = []
    for code, p in cur.items():
        old = prev.get(code)
        if old is None:
            alerts.append({"kind": "new", "p": p, "detail": ""})
            continue
        if p["inStock"] and not old.get("inStock", False):
            alerts.append({"kind": "restock", "p": p, "detail": "back in stock"})
            continue
        back = [c for c, ins in p["colors"].items()
                if ins and not old.get("colors", {}).get(c, False)]
        if back and p["inStock"]:
            alerts.append({"kind": "restock", "p": p,
                           "detail": f"{len(back)} color(s) back in stock"})
    return alerts


# --------------------------------------------------------------------------- #
# Notify
# --------------------------------------------------------------------------- #
def send_ntfy(topic: str, title: str, body: str, url: str = "",
              img: str = "", tags: str = "shopping_bags") -> None:
    headers = {"Title": title.encode("utf-8"), "Tags": tags, "Priority": "default"}
    if url:
        headers["Click"] = url
    if img:
        headers["Attach"] = img
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic}", data=body.encode("utf-8"),
            headers=headers, method="POST")
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log(f"  ntfy error: {e!r}")


def alert_to_push(a: dict) -> tuple[str, str]:
    p, kind = a["p"], a["kind"]
    designer = p.get("designer") or ""
    name = p.get("name") or "item"
    label = a.get("label", "")
    if kind == "new":
        title = f"🆕 {designer}: {name}"
    elif kind == "restock":
        title = f"🔄 Back in stock — {designer}: {name}"
    else:  # verify
        title = f"👀 Now watching{f' [{label}]' if label else ''}: {name}"
    body = [f"{designer} · {name}"]
    if p.get("price"):
        body.append(p["price"])
    if a.get("detail"):
        body.append(a["detail"])
    if label and kind != "verify":
        body.append(f"watch: {label}")
    body.append(p["url"])
    return title, "\n".join(body)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="compute + print alerts, don't send or save state")
    ap.add_argument("--check", action="store_true",
                    help="preview what item queries match (scores + stock), no state change")
    ap.add_argument("--seed", action="store_true",
                    help="save current state for all watches, never alert")
    ap.add_argument("--test-ntfy", action="store_true",
                    help="send one test push and exit")
    args = ap.parse_args()

    load_env()
    topic = os.environ.get("NTFY_TOPIC", "")

    if args.test_ntfy:
        if not topic:
            log("NTFY_TOPIC not set in .env"); sys.exit(1)
        send_ntfy(topic, "✅ Shopbop bot test",
                  "If you can read this, ntfy is wired up.", url=SITE_URL)
        log(f"Sent test push to topic '{topic}'.")
        return

    brand_watches = load_brands()
    item_watches  = load_items()
    if not brand_watches and not item_watches:
        log("Nothing to watch: add brands to brands.txt and/or items to items.txt.")
        sys.exit(1)

    # Fetch each distinct (brand, dept) once; reused across brand + item watches.
    to_fetch: set[tuple[str, str]] = set()
    for name, depts in brand_watches.items():
        for d in depts:
            to_fetch.add((name, d))
    for brand, _q, depts in item_watches:
        for d in depts:
            to_fetch.add((brand, d))

    log(f"Fetching {len(to_fetch)} brand×dept combo(s) for "
        f"{len(brand_watches)} brand-watch(es) + {len(item_watches)} item-watch(es)...")
    fetched: dict[tuple[str, str], dict] = {}
    for (name, dept) in sorted(to_fetch):
        prods = fetch_brand(name, dept)
        fetched[(name, dept)] = prods
        if not prods:
            log(f"  {name} [{dept}]: 0 products — not carried in this dept (skipped)")
        else:
            log(f"  {name} [{dept}]: {len(prods)} products")
        time.sleep(REQUEST_GAP)

    def merged_products(name: str, depts: tuple[str, ...]) -> dict:
        """Union a brand's products across departments, deduped by productCode
        (a co-ed item in both MENS and WOMENS collapses to one entry)."""
        out: dict[str, dict] = {}
        for d in depts:
            out.update(fetched.get((name, d), {}))
        return out

    # --check: just preview item matches and exit.
    if args.check:
        if not item_watches:
            log("No item watches in items.txt to check.")
        for brand, query, depts in item_watches:
            matches = filter_items(merged_products(brand, depts), query)
            ranked = sorted(matches.values(), key=lambda x: -x["score"])
            log(f"[{brand} | {query} | {'+'.join(depts)}] -> {len(ranked)} match(es):")
            for p in ranked[:8]:
                stock = "in stock" if p["inStock"] else "OUT of stock"
                print(f"    {p['score']:.2f}  {p['name']}  ({p['price']}, {stock})")
                print(f"          {p['url']}")
            if not ranked:
                print("    (no matches — try different/fewer words, or check spelling)")
        return

    state = load_state()
    new_state = dict(state)
    all_alerts: list[dict] = []

    # Brand watches (union of departments, deduped by productCode).
    for name, depts in brand_watches.items():
        cur = merged_products(name, depts)
        if not cur:
            continue
        key = f"brand::{name}"
        prev = state.get(key)
        if prev is None:
            log(f"  brand:{name}: first run, seeding ({len(cur)} products)")
        else:
            a = diff(prev, cur)
            for x in a:
                x["label"] = name
            all_alerts.extend(a)
            log(f"  brand:{name}: {len(a)} alert(s)")
        new_state[key] = cur

    # Item watches (union of departments, deduped by productCode).
    for brand, query, depts in item_watches:
        cur = filter_items(merged_products(brand, depts), query)
        key = f"item::{brand}::{query}"
        prev = state.get(key)
        label = f"{brand}: {query}"
        if not cur:
            log(f"  item:[{label}]: 0 matches (run --check to tune the words)")
            new_state[key] = cur
            continue
        if prev is None:
            # First run: don't seed silently — send one verification push so the
            # user can confirm the fuzzy match is right.
            log(f"  item:[{label}]: first run, {len(cur)} match(es) — sending verification")
            best = max(cur.values(), key=lambda x: x["score"])
            stock = "in stock now" if best["inStock"] else "currently out of stock"
            all_alerts.append({"kind": "verify", "p": best,
                               "detail": f"{len(cur)} match(es); top one {stock}",
                               "label": label})
        else:
            a = diff(prev, cur)
            for x in a:
                x["label"] = label
            all_alerts.extend(a)
            log(f"  item:[{label}]: {len(a)} alert(s)")
        new_state[key] = cur

    # Report / notify.
    if args.dry_run:
        log(f"DRY RUN — {len(all_alerts)} alert(s), state NOT saved:")
        for a in all_alerts[:50]:
            print("   " + alert_to_push(a)[0])
        return

    save_state(new_state)

    if args.seed:
        log("Seeded state for all watches.")
        return
    if not all_alerts:
        log("No changes since last run.")
        return

    to_send = all_alerts[:MAX_ALERTS]
    log(f"Sending {len(to_send)} push(es) (of {len(all_alerts)})...")
    for a in to_send:
        if topic:
            title, body = alert_to_push(a)
            tag = {"new": "new", "restock": "arrows_counterclockwise"}.get(a["kind"], "eyes")
            send_ntfy(topic, title, body, url=a["p"]["url"],
                      img=a["p"].get("img", ""), tags=tag)
        time.sleep(0.3)
    if len(all_alerts) > MAX_ALERTS and topic:
        send_ntfy(topic, f"…and {len(all_alerts) - MAX_ALERTS} more changes",
                  "Open Shopbop to see the rest.", url=SITE_URL)


if __name__ == "__main__":
    main()
