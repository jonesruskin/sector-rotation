"""
NSE Sector Rotation - daily data fetcher & calculator
------------------------------------------------------
Downloads NSE's official end-of-day "Index Bhavcopy" (a public CSV with the
closing value of every published NSE index for a given trading day), keeps a
rolling history file (data/history.json), and computes a Relative-Rotation
(RRG-style) score for each sector versus a benchmark (NIFTY 500).

Output: data/rotation.json  (consumed by index.html)

Designed to be run once per trading day, ~15-20 minutes after market close,
by the GitHub Actions workflow in .github/workflows/update-rotation.yml.
On its very first run it will automatically "backfill" the last ~90 calendar
days so the dashboard has enough history to compute meaningful quadrants
right away instead of waiting weeks.
"""

import json
import io
import time
import datetime as dt
from pathlib import Path

import requests
import pandas as pd

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

DATA_DIR = Path(__file__).parent / "data"
HISTORY_FILE = DATA_DIR / "history.json"
OUTPUT_FILE = DATA_DIR / "rotation.json"

BENCHMARK = "NIFTY 500"

# Official NSE sectoral / thematic indices we track (must match the "Index
# Name" text exactly as NSE publishes it in the bhavcopy, upper-cased below).
# Feel free to trim/extend this list.
# Each entry can be a single name or a tuple of acceptable spelling variants
# (NSE's bhavcopy wording doesn't always exactly match its website's display
# name, e.g. "OIL & GAS" vs "OIL AND GAS") — the fetcher normalizes and
# matches against any variant given.
SECTORS = [
    # --- Official Sectoral Indices ---
    "NIFTY AUTO",
    "NIFTY BANK",
    "NIFTY CAPITAL GOODS",
    "NIFTY CEMENT",
    "NIFTY CHEMICALS",
    ("NIFTY COMMERCIAL & TRANSPORT SERVICES", "NIFTY COMMERCIAL AND TRANSPORT SERVICES"),
    "NIFTY CONSTRUCTION",
    "NIFTY CONSUMER DURABLES",
    "NIFTY CONSUMER SERVICES",
    ("NIFTY FINANCIAL SERVICES", "NIFTY FIN SERVICE"),
    "NIFTY FMCG",
    ("NIFTY HEALTHCARE INDEX", "NIFTY HEALTHCARE"),
    "NIFTY HOSPITALS",
    "NIFTY HOUSING FINANCE",
    "NIFTY INSURANCE",
    "NIFTY IT",
    "NIFTY MEDIA",
    "NIFTY METAL",
    "NIFTY NBFC",
    ("NIFTY OIL & GAS", "NIFTY OIL AND GAS", "NIFTY OIL AND GAS INDEX"),
    "NIFTY PHARMA",
    "NIFTY POWER",
    "NIFTY PRIVATE BANK",
    "NIFTY PSU BANK",
    "NIFTY REALTY",
    ("NIFTY REITS & REALTY", "NIFTY REITS AND REALTY"),
    "NIFTY RETAIL",
    "NIFTY TELECOMMUNICATIONS",
    "NIFTY MIDSMALL HEALTHCARE",
    ("NIFTY MIDSMALL FINANCIAL SERVICES", "NIFTY MIDSMALL FINANCIAL SERVICE"),
    ("NIFTY MIDSMALL IT & TELECOM", "NIFTY MIDSMALL IT AND TELECOM"),

    # --- Thematic indices with real sector-rotation value ---
    "NIFTY CAPITAL MARKETS",
    "NIFTY COMMODITIES",
    "NIFTY CPSE",
    ("NIFTY EV & NEW AGE AUTOMOTIVE", "NIFTY EV AND NEW AGE AUTOMOTIVE"),
    "NIFTY ENERGY",
    "NIFTY INDIA DEFENCE",
    "NIFTY INDIA DIGITAL",
    "NIFTY INDIA INTERNET",
    "NIFTY INDIA MANUFACTURING",
    "NIFTY INDIA RAILWAYS PSU",
    "NIFTY INDIA TOURISM",
    "NIFTY INFRASTRUCTURE",
    "NIFTY MNC",
    "NIFTY PSE",
    "NIFTY SERVICES SECTOR",
    ("NIFTY SMALL FINANCE BANKS & MICROFINANCE INSTITUTIONS", "NIFTY SMALL FINANCE BANKS AND MICROFINANCE INSTITUTIONS"),
    ("NIFTY TRANSPORTATION & LOGISTICS", "NIFTY TRANSPORTATION AND LOGISTICS"),
]

RS_RATIO_WINDOW = 14      # trading days for the RS-Ratio smoothing
RS_MOMENTUM_WINDOW = 5    # trading days for the RS-Momentum smoothing
STRENGTH_LOOKBACK = 20    # ~1 month, for the "Strength %" column
RANK_LOOKBACK = 20        # ~4 weeks, for the "4-Wk Rank" delta
BACKFILL_CALENDAR_DAYS = 100   # how far back to reach on a cold start
MAX_HISTORY_DAYS_KEPT = 200    # trim history file so it doesn't grow forever

def _canonical_label(entry) -> str:
    """The name we display/store for a SECTORS entry (first variant if a tuple)."""
    return entry[0] if isinstance(entry, tuple) else entry


def _variants(entry) -> list:
    return list(entry) if isinstance(entry, tuple) else [entry]


def normalize_name(name: str) -> str:
    """Strip punctuation/whitespace differences so 'NIFTY OIL & GAS' and
    'Nifty Oil and Gas Index' compare equal."""
    n = name.upper()
    n = n.replace("&", " AND ")
    n = "".join(ch if ch.isalnum() else " " for ch in n)
    n = " ".join(n.split())
    n = n.replace(" INDEX", "")  # trailing "INDEX" suffix is inconsistent across NSE sources
    return n.strip()


# Build a lookup: normalized-variant -> canonical display name, for every
# sector we track (plus the benchmark).
_SECTOR_LOOKUP = {}
for _entry in SECTORS:
    _label = _canonical_label(_entry)
    for _v in _variants(_entry):
        _SECTOR_LOOKUP[normalize_name(_v)] = _label
_BENCHMARK_NORM = normalize_name(BENCHMARK)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

BHAVCOPY_URL = "https://archives.nseindia.com/content/indices/ind_close_all_{ddmmyyyy}.csv"


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def fetch_index_closes_for_date(date: dt.date) -> dict | None:
    """Download NSE's index bhavcopy for one calendar date. Returns a dict of
    {INDEX NAME: closing value} or None if that date has no file (weekend /
    holiday / not yet published)."""
    url = BHAVCOPY_URL.format(ddmmyyyy=date.strftime("%d%m%Y"))
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
    except requests.RequestException:
        return None
    if resp.status_code != 200 or not resp.content or b"Index Name" not in resp.content[:2000]:
        return None
    try:
        df = pd.read_csv(io.BytesIO(resp.content))
    except Exception:
        return None
    df.columns = [c.strip() for c in df.columns]
    name_col = next((c for c in df.columns if "Index Name" in c), None)
    close_col = next((c for c in df.columns if "Closing Index Value" in c), None)
    if not name_col or not close_col:
        return None

    day = {}
    for _, row in df.iterrows():
        raw_name = str(row[name_col]).strip()
        norm = normalize_name(raw_name)
        if norm == _BENCHMARK_NORM:
            canonical = BENCHMARK
        elif norm in _SECTOR_LOOKUP:
            canonical = _SECTOR_LOOKUP[norm]
        else:
            continue
        try:
            day[canonical] = float(row[close_col])
        except (ValueError, TypeError):
            continue
    return day or None


def load_history() -> dict:
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text())
    return {}


def save_history(history: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # keep file bounded
    if len(history) > MAX_HISTORY_DAYS_KEPT:
        for old_date in sorted(history.keys())[: len(history) - MAX_HISTORY_DAYS_KEPT]:
            del history[old_date]
    HISTORY_FILE.write_text(json.dumps(history, indent=2, sort_keys=True))


def update_history() -> dict:
    """Fetch any missing trading days (backfilling on a cold start, or just
    today's close on a normal daily run) and merge them into history.json."""
    history = load_history()
    today = dt.date.today()

    if not history:
        start = today - dt.timedelta(days=BACKFILL_CALENDAR_DAYS)
        print(f"No existing history — backfilling from {start} to {today}...")
    else:
        last_known = max(dt.date.fromisoformat(d) for d in history.keys())
        start = last_known + dt.timedelta(days=1)
        print(f"Existing history found up to {last_known}. Fetching {start}..{today}.")

    d = start
    fetched = 0
    while d <= today:
        # Skip weekends outright to save requests.
        if d.weekday() < 5:
            iso = d.isoformat()
            if iso not in history:
                day_data = fetch_index_closes_for_date(d)
                if day_data and BENCHMARK.upper() in day_data:
                    history[iso] = day_data
                    fetched += 1
                time.sleep(0.3)  # be polite to NSE's archive server
        d += dt.timedelta(days=1)

    print(f"Fetched {fetched} new trading day(s). Total days in history: {len(history)}.")
    save_history(history)
    return history


# --------------------------------------------------------------------------
# RRG-style rotation calculation
# --------------------------------------------------------------------------

def build_frame(history: dict) -> pd.DataFrame:
    dates = sorted(history.keys())
    rows = []
    for d in dates:
        row = {"date": d}
        row.update(history[d])
        rows.append(row)
    df = pd.DataFrame(rows).set_index("date")
    df.index = pd.to_datetime(df.index)
    df = df.sort_index()
    return df


ACRONYMS = {"NIFTY", "IT", "FMCG", "PSU", "NBFC", "MNC", "CPSE", "PSE", "EV", "REITS"}


def display_name(canonical: str) -> str:
    words = canonical.split(" ")
    out = []
    for w in words:
        core = w.strip("&")
        if core in ACRONYMS:
            out.append(w)  # keep as-is (already uppercase)
        else:
            out.append(w.capitalize())
    return " ".join(out)


def compute_rotation(df: pd.DataFrame) -> dict:
    bench_col = BENCHMARK.upper()
    if bench_col not in df.columns:
        raise RuntimeError("Benchmark data missing from history — cannot compute rotation.")

    canonical_names = [_canonical_label(s) for s in SECTORS]
    available_sectors = [s for s in canonical_names if s in df.columns]
    results = []
    rank_history = {}  # sector -> list of (date, rotation_score) for rank-change lookback

    scored_frame = pd.DataFrame(index=df.index)

    for sector in available_sectors:
        pair = df[[sector, bench_col]].dropna()
        if len(pair) < RS_RATIO_WINDOW + RS_MOMENTUM_WINDOW + 2:
            continue  # not enough history yet for this sector

        rs = pair[sector] / pair[bench_col]
        rs_ratio = 100 * rs / rs.rolling(RS_RATIO_WINDOW).mean()
        rs_momentum = 100 * rs_ratio / rs_ratio.rolling(RS_MOMENTUM_WINDOW).mean()
        rotation_score = (rs_ratio - 100) + (rs_momentum - 100)

        scored_frame[sector] = rotation_score

        if rotation_score.dropna().empty:
            continue

        latest_ratio = rs_ratio.iloc[-1]
        latest_mom = rs_momentum.iloc[-1]
        latest_score = rotation_score.iloc[-1]

        prev_mom = rs_momentum.iloc[-2] if len(rs_momentum) > 1 else latest_mom
        momentum_delta = latest_mom - prev_mom

        strength_pct = None
        if len(pair) > STRENGTH_LOOKBACK:
            past_px = pair[sector].iloc[-STRENGTH_LOOKBACK - 1]
            cur_px = pair[sector].iloc[-1]
            strength_pct = (cur_px / past_px - 1) * 100

        if pd.isna(latest_ratio) or pd.isna(latest_mom):
            continue

        if latest_ratio >= 100 and latest_mom >= 100:
            quadrant = "Leading"
        elif latest_ratio >= 100 and latest_mom < 100:
            quadrant = "Turning Down"
        elif latest_ratio < 100 and latest_mom < 100:
            quadrant = "Lagging"
        else:
            quadrant = "Turning Up"

        results.append({
            "name": display_name(sector),
            "raw_name": sector,
            "quadrant": quadrant,
            "rs_ratio": round(float(latest_ratio), 2),
            "rs_momentum": round(float(latest_mom), 2),
            "rotation_score": round(float(latest_score), 3),
            "strength_pct": round(float(strength_pct), 2) if strength_pct is not None else None,
            "momentum": round(float(momentum_delta), 2),
        })

    # rank now
    results.sort(key=lambda r: r["rotation_score"], reverse=True)
    for i, r in enumerate(results, start=1):
        r["rank"] = i

    # rank ~4 weeks ago, for the rank-change indicator
    if len(scored_frame) > RANK_LOOKBACK:
        past_scores = scored_frame.iloc[-RANK_LOOKBACK - 1].dropna().sort_values(ascending=False)
        past_ranks = {name: i + 1 for i, name in enumerate(past_scores.index)}
        for r in results:
            old_rank = past_ranks.get(r["raw_name"])
            r["rank_4wk_change"] = (old_rank - r["rank"]) if old_rank else None
    else:
        for r in results:
            r["rank_4wk_change"] = None

    for r in results:
        del r["raw_name"]

    return {
        "updated": df.index[-1].date().isoformat(),
        "benchmark": BENCHMARK,
        "trading_days_of_history": len(df),
        "sectors": results,
    }


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    history = update_history()
    df = build_frame(history)
    rotation = compute_rotation(df)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(rotation, indent=2))
    print(f"Wrote {OUTPUT_FILE} with {len(rotation['sectors'])} sectors "
          f"(as of {rotation['updated']}, {rotation['trading_days_of_history']} trading days of history).")


if __name__ == "__main__":
    main()
