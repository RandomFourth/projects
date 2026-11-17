# MTG Booster Box Prices

A dashboard of TCGplayer market price history for the 10 newest Magic sets, showing the Play Booster Display and Collector Booster Display for each.

## How it works

1. `scripts/update_prices.py` runs every 6 hours in GitHub Actions (03:41, 09:41, 15:41 and 21:41 UTC).
   - Scryfall gives the newest sets (expansion, core, masters and draft innovation; no digital sets).
   - TCGCSV gives each set's TCGplayer group, the two display product IDs, and today's market/low/mid/high price.
   - TCGplayer's price history endpoint (`infinite-api.tcgplayer.com/price/history/<id>/detailed`) gives the weekly market price, items sold and sale range. This is the same data as the "Market Price History" chart on a product page.
2. It writes `data/prices.json` (read by the page) and appends today's prices to `data/daily_market.csv`, so history keeps growing past TCGplayer's one-year window.
3. The workflow commits the data and publishes `index.html` plus `data/` to GitHub Pages.

The page reloads `data/prices.json` every 15 minutes, so an open tab picks up each update on its own.

## Put it on your iPhone

Once the site is published (see Setup), open its address in Safari, tap the Share button, then "Add to Home Screen". It gets its own icon and opens full screen like an app. It checks for new prices whenever you open it.

## Refresh button

The page can't pull prices itself, so the Refresh prices button asks GitHub to run the workflow now, waits for it to publish (about 2 minutes), and reloads. The first tap asks for a GitHub fine-grained token with access to only this repository and the Actions permission set to Read and write. The token is stored only in that browser. Set it up from the home screen app if you use one, since it keeps its own storage separate from Safari.

Without a token, "Or run it on GitHub" in the same dialog opens the workflow page, where Run workflow does the same thing.

## Setup

1. In the repository settings, open Pages and set Source to "GitHub Actions".
2. Make sure `.github/workflows/mtg-box-prices.yml` is on the default branch (scheduled runs only fire there).
3. Open Actions, pick "MTG box prices", and click "Run workflow" for the first pull.

## Run locally

```bash
python3 mtg-box-prices/scripts/update_prices.py
```

```bash
python3 -m http.server -d mtg-box-prices 8000
```

Then open http://localhost:8000.

## Fixing a wrong match

Edit `config/overrides.json`:

- `group_ids`: set code to TCGCSV group ID, when the set name or code does not match.
- `product_ids`: set code to `{"play": id, "collector": id}`, using the number from the TCGplayer product URL.
- `skip_sets`: set codes to leave off the dashboard.

## Notes

- The TCGplayer history endpoint is undocumented and could change or start refusing requests. If a product's history fails, the last good history is kept and the problem is listed at the top of the page.
