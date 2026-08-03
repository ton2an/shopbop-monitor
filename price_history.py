#!/usr/bin/env python3
"""
Shopify product price-history tracker.

Works with any Shopify store (e.g. bernerkuhl.com) — not Shopbop. Every run it
fetches each watched product's public `<product-url>.json` endpoint, appends
the cheapest in-stock variant's price to state/price_history.csv, and sends a
ntfy push (same NTFY_TOPIC as monitor.py) when the price changed since the
previous recorded point.

Run it on a schedule (cron) and the CSV becomes the price-over-time record;
`--graph` turns it into a self-contained HTML line chart.

Config:
  shopify_items.txt   one product URL per line (the /products/<handle> page).
  .env                NTFY_TOPIC=...   (optional; alerts skipped if unset)

Usage:
  python3 price_history.py                # one pass: record prices, alert on change
  python3 price_history.py --check        # print current prices, don't save/alert
  python3 price_history.py --graph        # write charts/<handle>.html per product
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from curl_cffi import requests as cf

ROOT        = Path(__file__).parent
ITEMS_PATH  = ROOT / "shopify_items.txt"
CSV_PATH    = ROOT / "state" / "price_history.csv"
CHARTS_DIR  = ROOT / "charts"
ENV_PATH    = ROOT / ".env"

REQUEST_GAP = 1.0   # seconds between product fetches (be polite)
FIELDS      = ["ts", "store", "handle", "title", "price",
               "compare_at", "currency", "available"]

SESSION = cf.Session(impersonate="chrome124")


def load_env() -> None:
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def load_urls() -> list[str]:
    if not ITEMS_PATH.exists():
        return []
    urls = []
    for raw in ITEMS_PATH.read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            urls.append(line.split()[0].rstrip("/"))
    return urls


def fetch_product(url: str) -> dict | None:
    """Fetch a Shopify product's public JSON. Returns a snapshot dict or None."""
    parsed = urllib.parse.urlparse(url)
    store = parsed.netloc
    handle = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    try:
        r = SESSION.get(url + ".json",
                        headers={"accept": "application/json"}, timeout=30)
        if r.status_code != 200:
            log(f"  {store}/{handle}: HTTP {r.status_code}")
            return None
        product = r.json()["product"]
    except Exception as e:
        log(f"  {store}/{handle}: fetch error {e!r}")
        return None

    variants = product.get("variants", [])
    if not variants:
        return None
    # Cheapest available variant; fall back to cheapest overall if sold out.
    def price(v: dict) -> float:
        try:
            return float(v.get("price") or "inf")
        except ValueError:
            return float("inf")
    avail = [v for v in variants if v.get("available", True)]
    best = min(avail or variants, key=price)
    compare = best.get("compare_at_price") or ""
    # Currency isn't in the product payload; Shopify serves the store's default.
    return {
        "store":      store,
        "handle":     handle,
        "title":      product.get("title", handle),
        "price":      best.get("price", ""),
        "compare_at": compare,
        "available":  bool(avail),
        "n_sizes":    len(avail),
        "n_variants": len(variants),
    }


def last_prices() -> dict[tuple[str, str], str]:
    """Most recent recorded price per (store, handle)."""
    out: dict[tuple[str, str], str] = {}
    if CSV_PATH.exists():
        with CSV_PATH.open() as f:
            for row in csv.DictReader(f):
                out[(row["store"], row["handle"])] = row["price"]
    return out


def append_row(snap: dict) -> None:
    CSV_PATH.parent.mkdir(exist_ok=True)
    new = not CSV_PATH.exists()
    with CSV_PATH.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow({
            "ts":         datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "store":      snap["store"],
            "handle":     snap["handle"],
            "title":      snap["title"],
            "price":      snap["price"],
            "compare_at": snap["compare_at"],
            "currency":   "",
            "available":  int(snap["available"]),
        })


def send_ntfy(topic: str, title: str, body: str, url: str = "") -> None:
    headers = {"Title": title.encode("utf-8"), "Tags": "chart_with_downwards_trend"}
    if url:
        headers["Click"] = url
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic}", data=body.encode("utf-8"),
            headers=headers, method="POST")
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log(f"  ntfy error: {e!r}")


# --------------------------------------------------------------------------- #
# Graph
# --------------------------------------------------------------------------- #
def write_graph(store: str, handle: str, rows: list[dict]) -> Path:
    """Render one product's history as a dependency-free HTML/SVG line chart."""
    pts = [(r["ts"], float(r["price"])) for r in rows if r["price"]]
    title = rows[-1]["title"] or handle
    w, h, pad = 800, 320, 50
    lo = min(p for _, p in pts)
    hi = max(max(p for _, p in pts),
             max((float(r["compare_at"]) for r in rows if r["compare_at"]),
                 default=0))
    if hi == lo:
        hi = lo * 1.1 or 1
    span = hi - lo

    def x(i: int) -> float:
        return pad + (w - 2 * pad) * (i / max(len(pts) - 1, 1))

    def y(p: float) -> float:
        return h - pad - (h - 2 * pad) * ((p - lo) / span)

    poly = " ".join(f"{x(i):.1f},{y(p):.1f}" for i, (_, p) in enumerate(pts))
    dots = "".join(
        f'<circle cx="{x(i):.1f}" cy="{y(p):.1f}" r="3.5" fill="#0a6849">'
        f"<title>{ts} — {p:g}</title></circle>"
        for i, (ts, p) in enumerate(pts))
    gridlines = "".join(
        f'<line x1="{pad}" y1="{y(lo + span * f):.1f}" x2="{w - pad}" '
        f'y2="{y(lo + span * f):.1f}" stroke="#ddd"/>'
        f'<text x="{pad - 8}" y="{y(lo + span * f) + 4:.1f}" text-anchor="end" '
        f'font-size="11" fill="#666">{lo + span * f:.0f}</text>'
        for f in (0, 0.25, 0.5, 0.75, 1))
    first, last = pts[0][0][:10], pts[-1][0][:10]

    html = f"""<!doctype html><meta charset="utf-8">
<title>{title} — price history</title>
<body style="font-family:system-ui;max-width:860px;margin:2rem auto">
<h2 style="margin-bottom:.2rem">{title}</h2>
<p style="color:#666;margin-top:0">{store}/{handle} · {len(pts)} data point(s) ·
latest {pts[-1][1]:g} (low {lo:g}, high {max(p for _, p in pts):g})</p>
<svg viewBox="0 0 {w} {h}" width="100%">
{gridlines}
<polyline points="{poly}" fill="none" stroke="#0a6849" stroke-width="2"/>
{dots}
<text x="{pad}" y="{h - pad + 20}" font-size="11" fill="#666">{first}</text>
<text x="{w - pad}" y="{h - pad + 20}" font-size="11" fill="#666"
 text-anchor="end">{last}</text>
</svg></body>"""
    CHARTS_DIR.mkdir(exist_ok=True)
    out = CHARTS_DIR / f"{handle}.html"
    out.write_text(html)
    return out


def graph_all() -> None:
    if not CSV_PATH.exists():
        log("No price history yet — run price_history.py a few times first.")
        return
    groups: dict[tuple[str, str], list[dict]] = {}
    with CSV_PATH.open() as f:
        for row in csv.DictReader(f):
            groups.setdefault((row["store"], row["handle"]), []).append(row)
    for (store, handle), rows in groups.items():
        out = write_graph(store, handle, rows)
        log(f"  {store}/{handle}: {len(rows)} point(s) -> {out}")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="print current prices, don't record or alert")
    ap.add_argument("--graph", action="store_true",
                    help="write charts/<handle>.html from recorded history")
    args = ap.parse_args()

    if args.graph:
        graph_all()
        return

    load_env()
    topic = os.environ.get("NTFY_TOPIC", "")
    urls = load_urls()
    if not urls:
        log("Nothing to track: add Shopify product URLs to shopify_items.txt "
            "(one per line).")
        sys.exit(1)

    prev = {} if args.check else last_prices()
    for url in urls:
        snap = fetch_product(url)
        if snap is None:
            continue
        cut = ""
        if snap["compare_at"]:
            try:
                pct = 100 * (1 - float(snap["price"]) / float(snap["compare_at"]))
                cut = f" ({snap['compare_at']} -{pct:.0f}%)"
            except (ValueError, ZeroDivisionError):
                pass
        stock = f"{snap['n_sizes']}/{snap['n_variants']} sizes in stock" \
                if snap["available"] else "SOLD OUT"
        log(f"  {snap['title']}: {snap['price']}{cut} · {stock}")

        if args.check:
            time.sleep(REQUEST_GAP)
            continue

        old = prev.get((snap["store"], snap["handle"]))
        append_row(snap)
        if old is not None and old != snap["price"] and topic:
            arrow = "⬇️" if float(snap["price"]) < float(old) else "⬆️"
            send_ntfy(topic, f"{arrow} {snap['title']}: {old} → {snap['price']}",
                      f"{snap['store']}\n{stock}", url=url)
            log(f"    price change {old} -> {snap['price']} (push sent)")
        time.sleep(REQUEST_GAP)


if __name__ == "__main__":
    main()
