# Daily Market Update

Web app that generates your weekday **Daily Market Update** after the Indian cash market close: pulls market tables, drafts formal narrative, lets you edit in-browser, then exports PDF.

## What it automates

| Section | Source (Render-friendly) |
|---|---|
| Market Snapshot | Yahoo Finance (Sensex, Nifty 50, Midcap 100, Smallcap 100, India VIX) |
| Global Markets | Yahoo Finance (S&P 500, Nasdaq, FTSE 100, Nikkei 225, Hang Seng) |
| Sectors Trending Today | NSE sectoral indices via Yahoo (replaces Groww screenshot with a clean table) |
| Nifty 50 gainers / losers | NSE public API |
| Advances / Declines | NSE public API |
| Commodity Market Watch | Moneycontrol MCX when available, else global futures fallback |
| News & Updates / Key Takeaways / Stocks in Focus | Headlines from Moneycontrol, CNBC TV18, NDTV Profit, Economic Times, Business Standard, Mint + Claude narrative |

Language rules are enforced in generation and export: formal tone, no em dashes.

## Local run

```bash
cd DailyMarketUpdate
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env
# REQUIRED for strong Key Takeaways / News / Stocks in Focus:
# set ANTHROPIC_API_KEY in .env

uvicorn app.main:app --reload --port 8000
```

Open http://127.0.0.1:8000

### WeasyPrint on Windows

PDF export uses WeasyPrint. On Linux/Render this is straightforward. On Windows you may need GTK libraries; if PDF fails locally, still use the editor/preview and export from the Render deployment.

## Deploy on Render

1. Push this repo to GitHub.
2. Create a **Web Service** on Render from that repo.
3. Use:
   - **Runtime:** Python 3
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
4. Add environment variables:
   - `ANTHROPIC_API_KEY` (recommended)
   - `ANTHROPIC_MODEL` (optional, default `claude-sonnet-4-20250514`)
5. If using `render.yaml`, connect the Blueprint; it also installs WeasyPrint system libs.

Or from the included Blueprint:

```bash
# after pushing to GitHub, in Render: New > Blueprint > select repo
```

## Daily workflow

1. Open the site after ~15:35 IST (Mon-Fri).
2. Click **Generate draft for today’s date** (button shows the IST date).
3. Review source status chips (retry or fill blanks if a source failed).
4. Edit bullets/tables in the left pane; preview updates live.
5. Click **Export PDF**.

## Insight workflow (Key Takeaways + News & Updates)

1. Pull market tables first.
2. Ingest a wider news set (12 feeds across your fixed sources), score by impact and overlap with today's movers/sectors/commodities, then enrich top stories by reading article text.
3. Ask Claude to write **insights**, not table restatements:
   - Key Takeaways: 5 analyst bullets (verdict, breadth, domestic drivers, macro overlay, watchpoint)
   - News & Updates: 6-8 high-impact bullets with optional theme labels, **no source prefixes**
4. You edit in the UI, then export PDF.

`ANTHROPIC_API_KEY` is required for the strongest writing quality.
