"""
update.py - refresh everything in the right order, then rebuild the scores.

    python update.py                  # normal refresh (prices every time, financials only when 30+ days old)
    python update.py --recheck-missing   # also retry stocks that had no financials page (new listings)
    python update.py --skip-sa        # skip the slow StockAnalysis steps (prices from Yahoo + rescore only)
    python update.py --push           # at the end: git add docs, commit, push (publishes the new scores)

Steps
  1. list page            (python sa_download.py list)            saved for 1 day
  2. financials           (python sa_download.py fin --all)       saved for 30 days, so this is fast most days
  3. Yahoo prices         (python fetch_prices.py <symbols>)      every stock EXCEPT the ones whose prices come
                                                                   from StockAnalysis (Yahoo has the wrong
                                                                   security for some of them, e.g. ORAS)
  4. StockAnalysis prices (python sa_prices.py ...)               new stocks that have no price file, and
                                                                   StockAnalysis-sourced files older than 3 days
  5. scores               (python score_all.py)                   writes docs/data/scores.json and scores.js

Needs $env:SA_CONTACT set (PowerShell) for steps 1, 2 and 4, like the other scripts.
Safe to stop and rerun: every step keeps what it already saved.
"""
import argparse
import csv
import datetime as dt
import os
import pathlib
import subprocess
import sys

import pandas as pd

from sa_parse import parse_list

ROOT = pathlib.Path(__file__).parent
PY = sys.executable
LIST_FILE = ROOT / "data" / "html" / "list.html"
MISSING = ROOT / "data" / "html" / "missing.txt"
PRICE_DIR = ROOT / "data" / "raw" / "prices"
SA_STATUS = ROOT / "data" / "raw" / "sa_prices_status.csv"
SA_STALE_DAYS = 3


def run(*cmd, must=True):
    print("\n>>>", " ".join(cmd))
    code = subprocess.call([PY, *cmd], cwd=ROOT)
    if code != 0 and must:
        sys.exit(f"\nstopped: {' '.join(cmd)} exited with code {code}. Fix that, then run update.py again.")
    return code


def sa_sourced():
    """Symbols whose price file was saved from StockAnalysis (latest log line has rows)."""
    if not SA_STATUS.exists():
        return set()
    out = {}
    with open(SA_STATUS, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            out[r["symbol"]] = int(r["rows"] or 0) > 0
    return {s for s, ok in out.items() if ok and (PRICE_DIR / f"{s}.csv").exists()}


def last_date(sym):
    try:
        return pd.read_csv(PRICE_DIR / f"{sym}.csv", usecols=[0]).iloc[-1, 0][:10]
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(description="Refresh data and rebuild the scores.")
    ap.add_argument("--recheck-missing", action="store_true", help="retry stocks that had no financials page")
    ap.add_argument("--skip-sa", action="store_true", help="skip StockAnalysis downloads (slow ones)")
    ap.add_argument("--push", action="store_true", help="commit and push docs/ when done")
    args = ap.parse_args()

    if not args.skip_sa and not os.environ.get("SA_CONTACT"):
        sys.exit("Set your project URL first (PowerShell):\n"
                 "   $env:SA_CONTACT = 'https://github.com/yourname/egx-scorer'\n"
                 "or run with --skip-sa to refresh Yahoo prices and rescore only.")

    started = dt.datetime.now()
    if not args.skip_sa:
        run("sa_download.py", "list")
        if args.recheck_missing and MISSING.exists():
            bak = MISSING.with_suffix(".bak")
            bak.write_text(MISSING.read_text())
            MISSING.unlink()
            print(f"(old list of missing financials kept as {bak.name}; it is rebuilt during the run)")
        run("sa_download.py", "fin", "--all")

    if not LIST_FILE.exists():
        sys.exit("no saved list page - run:  python sa_download.py list")
    symbols = [r["symbol"] for r in parse_list(LIST_FILE.read_text(encoding="utf-8"))]
    from_sa = sa_sourced()
    yahoo = [s for s in symbols if s not in from_sa]
    run("fetch_prices.py", *yahoo, must=False)         # a few Yahoo hiccups should not stop the update

    if not args.skip_sa:
        today = dt.date.today()
        stale = [s for s in sorted(from_sa)
                 if (d := last_date(s)) is None or (today - dt.date.fromisoformat(d)).days > SA_STALE_DAYS]
        if stale:
            print(f"\n{len(stale)} StockAnalysis-sourced price file(s) older than {SA_STALE_DAYS} days: {', '.join(stale)}")
            run("sa_prices.py", *stale, "--refresh", must=False)
        run("sa_prices.py", must=False)                # stocks with no price file at all

    run("score_all.py")

    mins = (dt.datetime.now() - started).total_seconds() / 60
    print(f"\nupdate finished in {mins:.0f} minutes. Open docs/index.html to check it.")
    if args.push:
        for cmd in (["git", "add", "docs"],
                    ["git", "commit", "-m", f"Update scores {dt.date.today().isoformat()}"],
                    ["git", "push"]):
            print(">>>", " ".join(cmd))
            if subprocess.call(cmd, cwd=ROOT) != 0:
                sys.exit("git step failed (if 'nothing to commit', the scores did not change).")
        print("pushed. The site refreshes in a minute or two.")
    else:
        print("To publish:  git add docs ; git commit -m \"Update scores\" ; git push   (or rerun with --push)")


if __name__ == "__main__":
    main()
