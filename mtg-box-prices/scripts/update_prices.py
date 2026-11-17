#!/usr/bin/env python3
"""Pull TCGplayer market price history for the newest Magic sets' booster displays.

For each of the newest N sets it finds the "Play Booster Display" and the
"Collector Booster Display", then saves:

  data/prices.json        everything the dashboard reads
  data/daily_market.csv   one row per product per day, so history keeps
                          building past TCGplayer's one-year window

Sources:
  Scryfall      set list and release dates
  TCGCSV        TCGplayer group and product IDs, plus today's market/low/mid/high
  TCGplayer     weekly and daily market price history (the same data as the
                "Market Price History" chart on each product page)

Standard library only. Run:  python3 scripts/update_prices.py
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
PRICES_JSON = DATA_DIR / "prices.json"
DAILY_CSV = DATA_DIR / "daily_market.csv"
OVERRIDES_JSON = ROOT / "config" / "overrides.json"

SCRYFALL_SETS = "https://api.scryfall.com/sets"
TCGCSV = "https://tcgcsv.com/tcgplayer/1"  # category 1 = Magic: The Gathering
HISTORY_URL = "https://infinite-api.tcgplayer.com/price/history/{pid}/detailed?range={rng}"
PRODUCT_URL = "https://www.tcgplayer.com/product/{pid}"

# TCGplayer history ranges. "annual" gives weekly buckets (the 1Y view);
# "quarter" gives finer buckets for the 1M and 3M views.
HISTORY_RANGES = ("annual", "quarter")

SET_TYPES = {"expansion", "core", "masters", "draft_innovation"}
KINDS = {"play": "Play Booster Display", "collector": "Collector Booster Display"}
OTHER_LANGUAGES = re.compile(
    r"\b(japanese|chinese|korean|german|french|italian|spanish|portuguese|russian)\b", re.I
)

USER_AGENT = os.environ.get(
    "PRICE_BOT_UA", "Mozilla/5.0 (compatible; mtg-box-prices/1.0; personal price dashboard)"
)
REQUEST_PAUSE = float(os.environ.get("PRICE_BOT_PAUSE", "1.0"))


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def get(url: str, *, as_json: bool = True, tries: int = 3):
    last_err = None
    for attempt in range(tries):
        req = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/plain, */*"}
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode("utf-8")
            return json.loads(body) if as_json else body
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as err:
            last_err = err
            code = getattr(err, "code", None)
            if code in (400, 401, 403, 404):
                break
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"{url}: {last_err}")


def num(value):
    if value in (None, ""):
        return None
    try:
        return round(float(str(value).replace("$", "").replace(",", "")), 2)
    except ValueError:
        return None


# ---------------------------------------------------------------- set discovery


def newest_sets(include_preorders: bool, today: dt.date) -> list[dict]:
    data = get(SCRYFALL_SETS)
    if data.get("object") != "list":
        raise RuntimeError("Scryfall /sets did not return a list")
    sets = [
        s
        for s in data["data"]
        if s.get("set_type") in SET_TYPES and not s.get("digital") and s.get("released_at")
    ]
    if not include_preorders:
        sets = [s for s in sets if dt.date.fromisoformat(s["released_at"]) <= today]
    sets.sort(key=lambda s: s["released_at"], reverse=True)
    return sets


def tcgcsv_groups() -> list[dict]:
    data = get(f"{TCGCSV}/groups")
    return data.get("results", [])


def match_group(scry_set: dict, groups: list[dict], overrides: dict) -> dict | None:
    code = scry_set["code"].upper()
    if code in overrides.get("group_ids", {}):
        gid = overrides["group_ids"][code]
        return next((g for g in groups if g["groupId"] == gid), {"groupId": gid, "name": scry_set["name"]})
    by_abbr = [g for g in groups if str(g.get("abbreviation", "")).upper() == code]
    if by_abbr:
        return by_abbr[0]
    name = scry_set["name"].lower()
    by_name = [g for g in groups if str(g.get("name", "")).lower() == name]
    return by_name[0] if by_name else None


def pick_display(products: list[dict], kind: str) -> dict | None:
    want = KINDS[kind].lower()
    found = [
        p
        for p in products
        if want in p.get("name", "").lower()
        and "case" not in p.get("name", "").lower()
        and not OTHER_LANGUAGES.search(p.get("name", ""))
    ]
    found.sort(key=lambda p: len(p["name"]))  # shortest name is the plain display
    return found[0] if found else None


# ---------------------------------------------------------------- price history


def parse_history(raw: dict) -> list[dict]:
    rows = raw.get("result") or raw.get("results") or []
    if not rows:
        return []

    def rank(r):
        return (
            str(r.get("condition", "")).lower() == "unopened",
            str(r.get("language", "english")).lower() == "english",
            str(r.get("variant", "normal")).lower() == "normal",
            len(r.get("buckets") or []),
        )

    best = max(rows, key=rank)
    points = []
    for b in best.get("buckets") or []:
        date = str(b.get("bucketStartDate") or b.get("date") or "")[:10]
        if not date:
            continue
        points.append(
            {
                "d": date,
                "m": num(b.get("marketPrice")),
                "q": int(num(b.get("quantitySold")) or 0),
                "lo": num(b.get("lowSalePrice")),
                "hi": num(b.get("highSalePrice")),
            }
        )
    points.sort(key=lambda p: p["d"])
    # TCGplayer dates each bucket by the day after it ends: the bucket its
    # site shows as "11/9 to 11/15" arrives dated 11/16. Shift each date back
    # by the bucket length so "d" is the first day the bucket covers.
    dates = [dt.date.fromisoformat(p["d"]) for p in points]
    for i, p in enumerate(points):
        if len(dates) < 2:
            break
        gap = (dates[i] - dates[i - 1]) if i else (dates[1] - dates[0])
        p["d"] = (dates[i] - gap).isoformat()
        if not p["q"]:  # no sales that period: TCGplayer sends 0 for low/high
            p["lo"] = p["hi"] = None
    # Periods before the product was listed come back with a $0 market price.
    for p in points:
        if not p["m"]:
            p["m"] = None
    return points


def fetch_history(pid: int) -> dict[str, list[dict]]:
    out = {}
    for rng in HISTORY_RANGES:
        raw = get(HISTORY_URL.format(pid=pid, rng=rng))
        out[rng] = parse_history(raw)
        time.sleep(REQUEST_PAUSE)
    return out


# ---------------------------------------------------------------- main


def load_previous() -> dict:
    try:
        return json.loads(PRICES_JSON.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def previous_product(prev: dict, pid: int) -> dict | None:
    for s in prev.get("sets", []):
        for p in (s.get("products") or {}).values():
            if p and p.get("product_id") == pid:
                return p
    return None


def append_daily(rows: list[dict]) -> None:
    fields = ["date", "set_code", "type", "product_id", "market", "low", "mid", "high"]
    existing = set()
    if DAILY_CSV.exists():
        with DAILY_CSV.open() as f:
            existing = {(r["date"], r["product_id"]) for r in csv.DictReader(f)}
    new = [r for r in rows if (r["date"], str(r["product_id"])) not in existing]
    if not new:
        return
    write_header = not DAILY_CSV.exists()
    with DAILY_CSV.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if write_header:
            w.writeheader()
        for r in new:
            w.writerow({k: ("" if r.get(k) is None else r[k]) for k in fields})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sets", type=int, default=int(os.environ.get("SET_COUNT", "10")))
    ap.add_argument("--include-preorders", action="store_true")
    ap.add_argument("--debug", action="store_true", help="print one raw history response")
    args = ap.parse_args()

    DATA_DIR.mkdir(exist_ok=True)
    today = dt.date.today()
    overrides = json.loads(OVERRIDES_JSON.read_text()) if OVERRIDES_JSON.exists() else {}
    skip = {c.upper() for c in overrides.get("skip_sets", [])}
    prev = load_previous()

    log("Reading Scryfall sets and TCGCSV groups")
    candidates = newest_sets(args.include_preorders, today)
    groups = tcgcsv_groups()

    out_sets, daily_rows, problems = [], [], []
    for s in candidates:
        if len(out_sets) >= args.sets:
            break
        code = s["code"].upper()
        if code in skip:
            continue
        group = match_group(s, groups, overrides)
        if not group:
            log(f"  {code}: no TCGplayer group found, skipping")
            continue
        gid = group["groupId"]
        products = get(f"{TCGCSV}/{gid}/products").get("results", [])
        prices = {
            p["productId"]: p
            for p in get(f"{TCGCSV}/{gid}/prices").get("results", [])
            if p.get("subTypeName", "Normal") == "Normal"
        }
        picked = {k: pick_display(products, k) for k in KINDS}
        for k, pid in (overrides.get("product_ids", {}).get(code) or {}).items():
            picked[k] = next((p for p in products if p["productId"] == pid), {"productId": pid, "name": f"{s['name']} {KINDS[k]}"})
        if not any(picked.values()):
            log(f"  {code}: no booster displays listed, skipping")
            continue

        log(f"  {code} {s['name']}")
        entry = {
            "code": code,
            "name": s["name"],
            "released_at": s["released_at"],
            "set_type": s["set_type"],
            "icon_svg_uri": s.get("icon_svg_uri"),
            "group_id": gid,
            "products": {},
        }
        for kind, prod in picked.items():
            if not prod:
                entry["products"][kind] = None
                continue
            pid = prod["productId"]
            price = prices.get(pid, {})
            current = {k: num(price.get(f"{k}Price")) for k in ("market", "low", "mid", "high")}
            item = {
                "product_id": pid,
                "name": prod.get("name"),
                "url": PRODUCT_URL.format(pid=pid),
                "image": prod.get("imageUrl"),
                "current": current,
                "history": {},
                "history_updated": None,
                "history_error": None,
            }
            try:
                hist = fetch_history(pid)
                if args.debug:
                    log(json.dumps(get(HISTORY_URL.format(pid=pid, rng="month")), indent=2)[:3000])
                    args.debug = False
                if not hist.get("annual"):
                    raise RuntimeError("history response had no buckets")
                item["history"] = hist
                item["history_updated"] = today.isoformat()
            except Exception as err:  # keep the last good history
                item["history_error"] = str(err)
                old = previous_product(prev, pid)
                if old:
                    item["history"] = old.get("history", {})
                    item["history_updated"] = old.get("history_updated")
                problems.append(f"{code} {kind}: {err}")
                log(f"    {kind}: history failed ({err})")
            entry["products"][kind] = item
            daily_rows.append(
                {"date": today.isoformat(), "set_code": code, "type": kind, "product_id": pid, **current}
            )
        out_sets.append(entry)

    try:
        tcgcsv_updated = get("https://tcgcsv.com/last-updated.txt", as_json=False).strip()
    except RuntimeError:
        tcgcsv_updated = None

    if not out_sets:
        log("No sets found; leaving existing data untouched")
        return 1

    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "tcgcsv_updated": tcgcsv_updated,
        "sets": out_sets,
        "problems": problems,
    }
    PRICES_JSON.write_text(json.dumps(payload, indent=1) + "\n")
    append_daily(daily_rows)
    log(f"Wrote {len(out_sets)} sets to {PRICES_JSON.relative_to(ROOT)}")
    if problems:
        log("Problems:\n  " + "\n  ".join(problems))
    return 0


if __name__ == "__main__":
    sys.exit(main())
