# ROTATION — NSE Sector Rotation Tracker

A self-updating sector-rotation dashboard, modeled on a Leading/Turning-Up/
Turning-Down/Lagging quadrant view (RRG-style), running entirely for free on
GitHub (Actions + Pages). No server, no API keys, no ongoing cost.

## What it does

- Every trading day, ~16:05 IST (shortly after NSE's 15:30 close), a GitHub
  Actions job downloads NSE's official **index bhavcopy** (a public CSV NSE
  publishes daily with every index's closing value), appends it to
  `data/history.json`, and recomputes each sector's position.
- Each sector gets an **RS-Ratio** (relative strength vs NIFTY 500) and
  **RS-Momentum** (is that relative strength accelerating or decelerating),
  which is the same math behind classic Relative Rotation Graphs. Those two
  numbers place every sector into one of four quadrants — Leading, Turning
  Down, Turning Up, Lagging — exactly like the reference dashboard.
- `index.html` is a static page that reads the resulting `data/rotation.json`
  and renders the quadrant boxes, the sortable ladder table, and a scatter
  "Map" view. GitHub Pages serves this file for free.

## One important honest caveat

The reference image tracks ~37 hand-curated micro-sectors (Cables & Wires,
Jewellery, Auto Ancillary...) — that's a proprietary classification from a
paid data vendor, not something NSE publishes. This build instead uses NSE's
own ~20-24 official sectoral/thematic indices (Auto, Bank, IT, Pharma,
Realty, Metal, FMCG, PSU/Private Bank, Oil & Gas, Defence, etc. — see the
`SECTORS` list in `fetch_rotation_data.py`), which is the real, freely
available public dataset closest to that view. You can edit that list any
time if NSE adds more indices, or if you later get access to a stock-level
sector mapping you'd like to aggregate yourself.

## Setup (10 minutes, one time)

1. **Create a new GitHub repository** (public or private both work), and
   upload every file in this folder to it, preserving the folder structure
   (the `.github/workflows/update-rotation.yml` path matters).

2. **Turn on write permissions for the workflow:**
   Repo → Settings → Actions → General → "Workflow permissions" →
   select **Read and write permissions** → Save.
   (This lets the daily job commit the updated data files back to the repo.)

3. **Turn on GitHub Pages:**
   Repo → Settings → Pages → Source: **Deploy from a branch** →
   Branch: `main`, folder: `/ (root)` → Save.
   GitHub will give you a URL like `https://<you>.github.io/<repo>/`.

4. **Run the workflow once manually** to populate history immediately
   instead of waiting for tomorrow's scheduled run:
   Repo → Actions → "Update sector rotation data" → **Run workflow**.
   This backfills ~3 months of history in one go and writes
   `data/rotation.json`.

5. Open your Pages URL. That's it — from here it updates itself every
   trading day automatically.

## Files

| File | Purpose |
|---|---|
| `fetch_rotation_data.py` | Downloads NSE data, maintains history, computes rotation scores |
| `requirements.txt` | Python deps (`requests`, `pandas`) |
| `.github/workflows/update-rotation.yml` | The daily schedule (cron) that runs the script and commits results |
| `data/history.json` | Rolling daily closes per index (auto-created/maintained) |
| `data/rotation.json` | Latest computed rotation snapshot (auto-created/maintained) |
| `index.html` | The dashboard UI |

## Tuning

All the knobs are at the top of `fetch_rotation_data.py`:
- `SECTORS` — which NSE indices to track
- `RS_RATIO_WINDOW` / `RS_MOMENTUM_WINDOW` — smoothing windows for the
  rotation math (defaults: 14 and 5 trading days)
- `STRENGTH_LOOKBACK` — window for the "Strength %" column (default: 20
  trading days ≈ 1 month)
- `RANK_LOOKBACK` — window for the "4-Wk Rank" change indicator

## If NSE ever changes its bhavcopy URL/format

NSE occasionally tweaks its archive paths. If the daily Action starts
failing, check `https://archives.nseindia.com/content/indices/` for the
current filename pattern and update `BHAVCOPY_URL` in
`fetch_rotation_data.py` accordingly.
