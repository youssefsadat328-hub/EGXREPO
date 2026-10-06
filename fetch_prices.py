"""
fetch_prices.py - daily prices and volume from Yahoo Finance for every stock
in the saved StockAnalysis list page (data/html/list.html).

Usage:
    python fetch_prices.py                 # all stocks; skips files saved in the last 20 hours
    python fetch_prices.py COMI SWDY       # only these
    python fetch_prices.py --refresh       # ignore what is already saved

Output:
    data/raw/prices/<SYMBOL>.csv           Open, High, Low, Close, Volume (not adjusted)
    data/raw/prices_status.csv             one line per stock: did Yahoo have it?

Yahoo uses the .CA suffix for the Egyptian Exchange (COMI -> COMI.CA).
Yahoo does not carry every small EGX stock; the ones it lacks simply get no
price-based scores later. This is normal, not a bug.
"""
import argparse
import pathlib
import time

import pandas as pd
import yfinance as yf

from sa_parse import parse_list

ROOT = pathlib.Path(__file__).parent
LIST_FILE = ROOT / "data" / "html" / "list.html"
PRICE_DIR = ROOT / "data" / "raw" / "prices"
STATUS_FILE = ROOT / "data" / "raw" / "prices_status.csv"

SUFFIX = ".CA"
FRESH_HOURS = 20
PAUSE = 0.6           # seconds between Yahoo requests
MAX_CONSECUTIVE_ERRORS = 6


def is_fresh(path):
    return path.exists() and (time.time() - path.stat().st_mtime) / 3600 < FRESH_HOURS


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="*")
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    if args.symbols:
        symbols = [s.upper() for s in args.symbols]
    else:
        if not LIST_FILE.exists():
            raise SystemExit("no saved list page - run:  python sa_download.py list")
        symbols = [r["symbol"] for r in parse_list(LIST_FILE.read_text(encoding="utf-8"))]

    PRICE_DIR.mkdir(parents=True, exist_ok=True)
    old = {}
    if STATUS_FILE.exists():
        old = pd.read_csv(STATUS_FILE).set_index("symbol").to_dict("index")

    status = dict(old)
    consecutive_errors = 0
    got = skipped = empty = errors = 0
    for i, sym in enumerate(symbols, 1):
        path = PRICE_DIR / f"{sym}.csv"
        if not args.refresh and is_fresh(path):
            skipped += 1
            continue
        ticker = sym + SUFFIX
        try:
            px = yf.Ticker(ticker).history(period="5y", auto_adjust=False)
        except Exception as e:  # network, rate limit, etc.
            errors += 1
            consecutive_errors += 1
            status[sym] = {"yahoo_ticker": ticker, "rows": 0, "last_date": "", "status": f"error: {str(e)[:60]}"}
            print(f"[{i}/{len(symbols)}] {sym:8} error: {str(e)[:70]}")
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                print("\nToo many errors in a row - Yahoo is probably rate-limiting you. "
                      "Stopping; run again in an hour. What is saved so far is kept.")
                break
            time.sleep(PAUSE * 3)
            continue
        consecutive_errors = 0
        if px is None or px.empty:
            empty += 1
            status[sym] = {"yahoo_ticker": ticker, "rows": 0, "last_date": "", "status": "no data on Yahoo"}
            print(f"[{i}/{len(symbols)}] {sym:8} no data on Yahoo")
        else:
            px.index = px.index.tz_localize(None)
            px[["Open", "High", "Low", "Close", "Volume"]].to_csv(path)
            got += 1
            status[sym] = {"yahoo_ticker": ticker, "rows": len(px), "last_date": str(px.index[-1].date()), "status": "ok"}
            print(f"[{i}/{len(symbols)}] {sym:8} {len(px)} days, last {px.index[-1].date()}")
        time.sleep(PAUSE)

    out = pd.DataFrame.from_dict(status, orient="index")
    out.index.name = "symbol"
    out.reset_index().to_csv(STATUS_FILE, index=False)
    print(f"\nsaved {got}, no data on Yahoo {empty}, errors {errors}, already fresh {skipped}")
    print(f"status file: {STATUS_FILE}")


if __name__ == "__main__":
    main()
