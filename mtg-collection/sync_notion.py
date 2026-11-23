#!/usr/bin/env python3
"""Keep the Notion "Sealed Collection" databases priced from TCGplayer.

Each run does up to three things:

1. New items: any Collection row with a TCGplayer link whose "History loaded"
   box is unchecked gets its product details, today's market price and its
   weekly price history since the purchase date (up to a year back).
2. Weekly check: once per week (the first run on or after Monday, UTC) every
   owned item gets this week's market price, saved to Price History.
3. Weekly Totals: rebuilt from Price History whenever 1 or 2 changed anything.

The repository and its Action logs are public, so this script never prints
product names, prices or quantities. Logs show counts and errors only.

Needs NOTION_TOKEN (a Notion internal integration token with access to the
Sealed Collection page). Standard library only.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict

# Notion data sources (not secret: useless without the token).
COLLECTION = os.environ.get("COLLECTION_DS", "4211c394-4e9f-812e-b9bc-000bd8152853")
HISTORY = os.environ.get("HISTORY_DS", "4211c394-4e9f-8144-8f81-000bf6f160e8")
TOTALS = os.environ.get("TOTALS_DS", "4211c394-4e9f-81e5-ba7d-000bc8ae476b")

NOTION = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"
TCG_HISTORY = "https://infinite-api.tcgplayer.com/price/history/{pid}/detailed?range=annual"
TCG_LIVE = "https://mpapi.tcgplayer.com/v2/product/{pid}/pricepoints"
TCG_DETAILS = "https://mpapi.tcgplayer.com/v2/product/{pid}/details"
TCG_IMAGE = "https://tcgplayer-cdn.tcgplayer.com/product/{pid}_200w.jpg"
USER_AGENT = "Mozilla/5.0 (compatible; mtg-collection/1.0; personal collection tracker)"

TYPE_RULES = [  # first match wins, checked against the product name
    ("case", "Booster Box Case"),
    ("collector booster display", "Collector Booster Display"),
    ("play booster display", "Play Booster Display"),
    ("set booster display", "Set Booster Display"),
    ("draft booster display", "Draft Booster Display"),
    ("bundle", "Bundle"),
    ("commander deck", "Commander Deck"),
]

TOKEN = os.environ.get("NOTION_TOKEN", "")


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- HTTP


def http(method: str, url: str, body=None, headers=None, tries: int = 4):
    data = json.dumps(body).encode() if body is not None else None
    last = None
    for attempt in range(tries):
        req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else None
        except urllib.error.HTTPError as err:
            last = err
            if err.code == 429 or err.code >= 500:
                wait = float(err.headers.get("Retry-After") or 2 * (attempt + 1))
                time.sleep(wait)
                continue
            detail = err.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"{method} {url.split('?')[0]} -> {err.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as err:
            last = err
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"{method} {url.split('?')[0]} failed: {last}")


def notion(method: str, path: str, body=None):
    time.sleep(0.34)  # stay under Notion's ~3 requests per second
    return http(method, NOTION + path, body, {
        "Authorization": f"Bearer {TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    })


def tcg(url: str):
    time.sleep(0.5)
    return http("GET", url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}, tries=2)


def query_all(ds: str, body: dict | None = None) -> list[dict]:
    rows, cursor = [], None
    while True:
        payload = dict(body or {}, page_size=100)
        if cursor:
            payload["start_cursor"] = cursor
        res = notion("POST", f"/data_sources/{ds}/query", payload)
        rows.extend(res.get("results", []))
        if not res.get("has_more"):
            return rows
        cursor = res.get("next_cursor")


# ---------------------------------------------------------------- Notion values


def val(page: dict, name: str):
    p = page["properties"].get(name)
    if not p:
        return None
    t = p["type"]
    v = p.get(t)
    if t in ("title", "rich_text"):
        return "".join(x.get("plain_text", "") for x in v) or None
    if t == "select":
        return v["name"] if v else None
    if t == "date":
        return v["start"][:10] if v and v.get("start") else None
    if t == "relation":
        return [r["id"] for r in v]
    if t == "formula":
        return v.get(v.get("type"))
    return v  # number, url, checkbox


def p_title(s):
    return {"title": [{"text": {"content": s}}]}


def p_text(s):
    return {"rich_text": [{"text": {"content": s[:1900]}}] if s else []}


def p_num(n):
    return {"number": None if n is None else round(float(n), 2)}


def p_date(d):
    return {"date": {"start": d} if d else None}


def p_select(s):
    return {"select": {"name": s} if s else None}


# ---------------------------------------------------------------- TCGplayer


def num(v):
    try:
        return round(float(str(v).replace("$", "").replace(",", "")), 2) if v not in (None, "") else None
    except ValueError:
        return None


def monday(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=d.weekday())


def product_id(link: str | None) -> int | None:
    m = re.search(r"tcgplayer\.com/product/(\d+)", link or "")
    return int(m.group(1)) if m else None


def live_price(pid: int):
    try:
        raw = tcg(TCG_LIVE.format(pid=pid))
    except RuntimeError:
        return None
    rows = raw if isinstance(raw, list) else raw.get("results") or raw.get("result") or [raw]
    for r in rows:
        if isinstance(r, dict) and str(r.get("printingType", "Normal")).lower() == "normal":
            return num(r.get("marketPrice"))
    return None


def details(pid: int) -> dict:
    try:
        raw = tcg(TCG_DETAILS.format(pid=pid))
    except RuntimeError:
        return {}
    if isinstance(raw, dict) and isinstance(raw.get("results"), list) and raw["results"]:
        raw = raw["results"][0]
    return raw if isinstance(raw, dict) else {}


def weekly_history(pid: int) -> list[tuple[dt.date, float]]:
    """(week start, market price) pairs, oldest first.

    TCGplayer dates each bucket by the day after it ends, so each date is
    moved back one bucket length to the Monday the week starts on.
    """
    raw = tcg(TCG_HISTORY.format(pid=pid))
    rows = raw.get("result") or []
    if not rows:
        return []
    best = max(rows, key=lambda r: (str(r.get("condition", "")).lower() == "unopened",
                                    str(r.get("language", "english")).lower() == "english",
                                    len(r.get("buckets") or [])))
    pts = sorted(
        (dt.date.fromisoformat(str(b.get("bucketStartDate"))[:10]), num(b.get("marketPrice")))
        for b in best.get("buckets") or [] if b.get("bucketStartDate")
    )
    out = []
    for i, (d, m) in enumerate(pts):
        gap = (d - pts[i - 1][0]) if i else (pts[1][0] - d if len(pts) > 1 else dt.timedelta(days=7))
        if m:
            out.append((monday(d - gap), m))
    return out


def product_type(name: str) -> str:
    low = name.lower()
    return next((t for key, t in TYPE_RULES if key in low), "Other")


# ---------------------------------------------------------------- jobs


def history_row(item_id: str, week: dt.date, price: float, qty: float, source: str) -> dict:
    return {
        "parent": {"type": "data_source_id", "data_source_id": HISTORY},
        "properties": {
            "Entry": p_title(week.strftime("%b %-d, %Y")),
            "Week of": p_date(week.isoformat()),
            "Item": {"relation": [{"id": item_id}]},
            "Market price": p_num(price),
            "Qty": p_num(qty),
            "Value": p_num(price * qty),
            "Source": p_select(source),
        },
    }


def qty_of(item: dict) -> float:
    q = val(item, "Qty")
    return float(q) if q else 1.0


def owned_until(item: dict) -> dt.date | None:
    """Last day the item counts as owned, or None if still owned."""
    if val(item, "Status") != "Sold":
        return None
    sold = val(item, "Sold date")
    return dt.date.fromisoformat(sold) if sold else dt.date.min


def set_up_item(item: dict, this_week: dt.date, today: dt.date) -> None:
    """Fill in details and past prices for a new item."""
    link = val(item, "TCGplayer link")
    pid = product_id(link) or (int(val(item, "Product ID")) if val(item, "Product ID") else None)
    if not pid:
        notion("PATCH", f"/pages/{item['id']}", {"properties": {
            "Sync status": p_text("Add the TCGplayer product link (it looks like tcgplayer.com/product/123456/...).")}})
        return

    info = details(pid)
    name = info.get("productName") or val(item, "Product") or ""
    try:
        hist = weekly_history(pid)
    except RuntimeError as err:
        log(f"  history unavailable for one item: {err}")
        hist = []
    price = live_price(pid) or (hist[-1][1] if hist else None)

    bought = val(item, "Purchase date")
    first_week = monday(dt.date.fromisoformat(bought)) if bought else this_week
    last_day = owned_until(item)
    have = {val(r, "Week of") for r in query_all(HISTORY, {
        "filter": {"property": "Item", "relation": {"contains": item["id"]}}})}
    qty = qty_of(item)
    added = 0
    for week, m in hist:
        if week < first_week or week >= this_week or week.isoformat() in have:
            continue
        if last_day is not None and week > last_day:
            continue
        notion("POST", "/pages", history_row(item["id"], week, m, qty, "Backfill"))
        added += 1

    done = [m for w, m in hist if w < this_week]  # completed weeks only
    prior = done[-1] if done else None
    props = {
        "Product ID": p_num(pid),
        "Market price": p_num(price),
        "Prior week price": p_num(prior),
        "Price checked": p_date(today.isoformat()),
        "History loaded": {"checkbox": True},
        "Sync status": p_text("" if price is not None else "TCGplayer had no market price for this product yet."),
    }
    if info.get("setName") and not val(item, "Set"):
        props["Set"] = p_text(info["setName"])
    if not val(item, "Type"):
        props["Type"] = p_select(product_type(name))
    if not item["properties"].get("Image", {}).get("files"):
        props["Image"] = {"files": [{"name": f"{pid}.jpg", "type": "external", "external": {"url": TCG_IMAGE.format(pid=pid)}}]}
    notion("PATCH", f"/pages/{item['id']}", {"properties": props})
    log(f"  set up one item: {added} past week(s) loaded")


def weekly_check(items: list[dict], this_week: dt.date, today: dt.date) -> int:
    """Record this week's price for every owned, set-up item. Returns rows written."""
    recorded = {
        rid
        for r in query_all(HISTORY, {"filter": {"property": "Week of", "date": {"equals": this_week.isoformat()}}})
        for rid in (val(r, "Item") or [])
    }
    cache: dict[int, float | None] = {}
    written = 0
    for item in items:
        pid = val(item, "Product ID")
        if not pid or not val(item, "History loaded") or val(item, "Status") == "Sold":
            continue
        if item["id"] in recorded:
            continue
        pid = int(pid)
        if pid not in cache:
            cache[pid] = live_price(pid)
            if cache[pid] is None:
                try:
                    h = weekly_history(pid)
                    cache[pid] = h[-1][1] if h else None
                except RuntimeError:
                    pass
        price = cache[pid]
        if price is None:
            notion("PATCH", f"/pages/{item['id']}", {"properties": {
                "Sync status": p_text(f"No TCGplayer price on {today.isoformat()}; will try again next run.")}})
            continue
        notion("POST", "/pages", history_row(item["id"], this_week, price, qty_of(item), "Weekly"))
        props = {
            "Market price": p_num(price),
            "Price checked": p_date(today.isoformat()),
            "Sync status": p_text(""),
        }
        checked = val(item, "Price checked")
        if checked and dt.date.fromisoformat(checked) < this_week:
            props["Prior week price"] = p_num(val(item, "Market price"))  # last week's price
        notion("PATCH", f"/pages/{item['id']}", {"properties": props})
        written += 1
    return written


def rebuild_totals(items: list[dict]) -> int:
    paid = {i["id"]: (val(i, "Paid") or 0) for i in items}
    weeks: dict[str, dict] = defaultdict(lambda: {"value": 0.0, "paid": 0.0, "qty": 0.0})
    for r in query_all(HISTORY):
        week, rel = val(r, "Week of"), val(r, "Item") or []
        if not week or not rel or rel[0] not in paid:
            continue  # row for a deleted item
        w = weeks[week]
        w["value"] += val(r, "Value") or 0
        w["paid"] += paid[rel[0]]
        w["qty"] += val(r, "Qty") or 1

    existing = {}
    for r in query_all(TOTALS):
        wk = val(r, "Week of")
        if wk in existing or wk not in weeks:
            notion("PATCH", f"/pages/{r['id']}", {"in_trash": True})  # duplicate or no longer backed by data
        else:
            existing[wk] = r

    changed = 0
    for wk in sorted(weeks):
        w = weeks[wk]
        props = {
            "Week": p_title(wk),
            "Week of": p_date(wk),
            "Total value": p_num(w["value"]),
            "Total paid": p_num(w["paid"]),
            "Items": p_num(w["qty"]),
        }
        old = existing.get(wk)
        if old is None:
            notion("POST", "/pages", {"parent": {"type": "data_source_id", "data_source_id": TOTALS}, "properties": props})
            changed += 1
        elif (val(old, "Total value"), val(old, "Total paid"), val(old, "Items")) != (
            round(w["value"], 2), round(w["paid"], 2), round(w["qty"], 2)):
            notion("PATCH", f"/pages/{old['id']}", {"properties": props})
            changed += 1
    return changed


def main() -> int:
    if not TOKEN:
        print("::warning::NOTION_TOKEN is not set, so nothing was synced. See mtg-collection/README.md.")
        return 0
    today = dt.datetime.now(dt.timezone.utc).date()
    this_week = monday(today)

    items = query_all(COLLECTION)
    log(f"{len(items)} collection rows")

    new = [i for i in items if not val(i, "History loaded") and (val(i, "TCGplayer link") or val(i, "Product ID"))]
    missing_link = [i for i in items if not val(i, "History loaded") and not val(i, "TCGplayer link")
                    and not val(i, "Product ID") and not val(i, "Sync status")]
    for item in new + missing_link:
        try:
            set_up_item(item, this_week, today)
        except RuntimeError as err:
            log(f"  could not set up one item: {err}")
    if new:
        log(f"Set up {len(new)} new item(s)")
        items = query_all(COLLECTION)  # pick up the details just written

    n = weekly_check(items, this_week, today)
    if n:
        log(f"Week of {this_week}: {n} price(s) recorded")

    # Rebuilt every run so edits to Paid, Status or deleted rows show up.
    log(f"Weekly Totals: {rebuild_totals(items)} row(s) written")
    log("Done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
