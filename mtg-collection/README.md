# MTG Sealed Collection

Tracks the value of a sealed Magic collection in Notion. You log purchases in the Notion app; a GitHub Action prices them from TCGplayer.

## Where things live

- **Notion page "Sealed Collection"**: the Collection, Price History and Weekly Totals databases, plus charts. All collection data stays here.
- **`sync_notion.py`**: the price job. Standard library Python, no data files.
- **`.github/workflows/mtg-collection.yml`**: runs the job every 3 hours.

## What the job does

1. **New items**: a Collection row with a TCGplayer link and "History loaded" unchecked gets its product ID, set, type, image, current market price, and weekly prices from its purchase date onward (up to a year back).
2. **Weekly price**: the first run each week (weeks start Monday, UTC) records every owned item's market price in Price History and updates Market price and Prior week price.
3. **Weekly Totals**: rebuilt from Price History on every run, so edits to Paid, Status or deleted rows show up.

Sold items stop getting weekly prices after they are marked Sold.

The repository and its Action logs are public, so the job prints counts and errors only, never product names, prices or quantities.

## One-time setup

1. **Make a Notion connection.** Go to https://www.notion.so/profile/integrations, click **New integration**, name it (for example "Collection prices"), choose your workspace, keep the type **Internal**, and save. Copy the **Internal Integration Secret**.
2. **Give it the page.** In Notion, open **Sealed Collection**, open the **•••** menu, choose **Connections**, and add the connection you just made. The three databases inside get access with it.
3. **Give GitHub the secret.** In this repository go to **Settings → Secrets and variables → Actions → New repository secret**. Name it `NOTION_TOKEN` and paste the secret.
4. **Run it once.** Go to **Actions → MTG collection prices → Run workflow**.

## Adding items

In Notion, add a row to **Collection** with Product, TCGplayer link, Qty (blank means 1), Paid (the all-in total for the row) and Purchase date. The rest fills in on the next run, within 3 hours. To fill it in now, run the workflow from the Actions tab.

If the job can't price a row it says why in **Sync status**.
