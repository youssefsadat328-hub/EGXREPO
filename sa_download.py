"""
sa_download.py - polite, cache-first downloader for StockAnalysis pages.

What it does, and what it deliberately does NOT do:
  * reads robots.txt first and refuses any URL it disallows
  * identifies itself honestly (no pretending to be a browser)
  * waits several seconds between requests
  * keeps every page it downloads, so each page is requested at most once
    per refresh period
  * STOPS at the first 403 / 429 / 5xx or at a page that does not look like
    the real thing. It never retries around a block.

Usage (run from your project folder):
    python sa_download.py list                    # the list of all EGX stocks (1 request)
    python sa_download.py fin COMI SWDY           # financials pages for specific stocks
    python sa_download.py fin --all --max 20      # next 20 stocks not yet saved
    python sa_download.py fin --all               # everything still missing
    python sa_download.py fin --all --refresh     # re-download even if saved recently
    python sa_download.py import "saved page.html" ...   # add pages you saved by hand (no request)

Pages are saved under data/html/ . Do NOT commit that folder to GitHub
(see .gitignore). Source of the data: StockAnalysis.com - credit it on your page.
"""
import argparse
import os
import pathlib
import random
import re
import shutil
import sys
import time
import urllib.robotparser

import requests

from sa_parse import parse_list

# ------------------------------------------------------------- settings
# EDIT THIS: put your public GitHub project URL (or any contact that is not a
# private email address) so the site owner can see who is asking, e.g.
# "https://github.com/yourname/egx-scorer"
CONTACT = os.environ.get("SA_CONTACT", "https://github.com/youssefsadat328-hub/EGXREPO")

USER_AGENT = f"egx-scorer/0.1 (personal learning project; {CONTACT})"
BASE = os.environ.get("SA_BASE", "https://stockanalysis.com").rstrip("/")
LIST_PATH = "/list/egyptian-stock-exchange/"
FIN_PATH = "/quote/egx/{symbol}/financials/"
# A few companies are listed mainly abroad; StockAnalysis keeps their financials under the main listing's page.
FIN_PATH_OVERRIDES = {"ORAS": "/quote/adx/ORAS/financials/"}

MIN_DELAY = 2.0          # never go faster than this, whatever --delay says
DEFAULT_DELAY = 4.0      # seconds between requests (a little random jitter is added)
LIST_MAX_AGE_DAYS = 1
FIN_MAX_AGE_DAYS = 30    # financials change only when companies report
TIMEOUT = 30

ROOT = pathlib.Path(__file__).parent
HTML_DIR = ROOT / "data" / "html"
FIN_DIR = HTML_DIR / "fin"
LIST_FILE = HTML_DIR / "list.html"
MISSING_FILE = HTML_DIR / "missing.txt"
ERROR_LOG = HTML_DIR / "errors.log"


class StopDownloading(Exception):
    """Raised when the site says no (or something looks wrong). We stop, we do not work around it."""


# ------------------------------------------------------------- helpers
def age_days(path):
    return (time.time() - path.stat().st_mtime) / 86400


def is_fresh(path, max_days):
    return path.exists() and age_days(path) <= max_days


def log_error(msg):
    HTML_DIR.mkdir(parents=True, exist_ok=True)
    with open(ERROR_LOG, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")


def load_robots(session):
    """Fetch and parse robots.txt. If we cannot read it, we do not download."""
    url = BASE + "/robots.txt"
    try:
        r = session.get(url, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise StopDownloading(f"could not read robots.txt ({e}); not downloading anything")
    if r.status_code != 200:
        raise StopDownloading(f"robots.txt returned HTTP {r.status_code}; not downloading anything")
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(r.text.splitlines())
    return rp


def get_page(session, rp, url):
    """Return (status, html). Status is 'ok', 'missing' (404) or 'error'.
    Raises StopDownloading if the site pushes back."""
    if not rp.can_fetch(USER_AGENT, url):
        raise StopDownloading(f"robots.txt disallows {url}")
    try:
        r = session.get(url, timeout=TIMEOUT)
    except requests.RequestException as e:
        log_error(f"{url}  network error: {e}")
        return "error", None
    if r.status_code in (403, 429) or r.status_code >= 500:
        retry = r.headers.get("Retry-After")
        extra = f" (Retry-After: {retry})" if retry else ""
        raise StopDownloading(f"HTTP {r.status_code} from {url}{extra} - the site is pushing back, so stopping. "
                              "Wait a while (hours, not minutes) before running again, and use a larger --delay.")
    if r.status_code == 404:
        return "missing", None
    if r.status_code != 200:
        log_error(f"{url}  unexpected HTTP {r.status_code}")
        return "error", None
    r.encoding = "utf-8"
    html = r.text
    if "<table" not in html:
        raise StopDownloading(f"{url} came back without a data table (blocked, challenge page or layout change?). "
                              "Stopping; not trying to get around it.")
    return "ok", html


def new_session():
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    return s


def pause(delay, first):
    if not first:
        time.sleep(delay + random.uniform(0, 1.5))


# ------------------------------------------------------------- commands
def cmd_list(args):
    HTML_DIR.mkdir(parents=True, exist_ok=True)
    if is_fresh(LIST_FILE, LIST_MAX_AGE_DAYS) and not args.refresh:
        print(f"list already saved {age_days(LIST_FILE) * 24:.1f} h ago - nothing to do (use --refresh to force)")
        return
    session = new_session()
    rp = load_robots(session)
    status, html = get_page(session, rp, BASE + LIST_PATH)
    if status != "ok":
        raise StopDownloading(f"could not get the list page ({status})")
    n = len(parse_list(html))  # sanity: must parse
    LIST_FILE.write_text(html, encoding="utf-8")
    print(f"saved list page: {n} stocks -> {LIST_FILE}")


def cmd_fin(args):
    FIN_DIR.mkdir(parents=True, exist_ok=True)
    if args.all:
        if not LIST_FILE.exists():
            sys.exit("no saved list page yet - run:  python sa_download.py list")
        symbols = [r["symbol"] for r in parse_list(LIST_FILE.read_text(encoding="utf-8"))]
    else:
        symbols = [s.upper() for s in args.symbols]
    if not symbols:
        sys.exit("give some symbols (COMI SWDY ...) or use --all")
    bad = [s for s in symbols if not re.fullmatch(r"[A-Z0-9\-]+", s)]
    if bad:
        sys.exit(f"unexpected characters in symbols: {bad}")

    known_missing = set(MISSING_FILE.read_text().split()) if MISSING_FILE.exists() else set()
    todo = []
    for s in symbols:
        f = FIN_DIR / f"{s}.html"
        if not args.refresh and (is_fresh(f, FIN_MAX_AGE_DAYS) or (s in known_missing and s not in FIN_PATH_OVERRIDES)):
            continue
        todo.append(s)
    skipped = len(symbols) - len(todo)
    if args.max:
        todo = todo[:args.max]
    if not todo:
        print(f"nothing to download ({skipped} already saved or known to have no page)")
        return

    delay = max(args.delay, MIN_DELAY)
    print(f"{len(todo)} to download, {skipped} skipped. About {len(todo) * (delay + 0.75) / 60:.0f} min at {delay:.0f}s per page.")
    session = new_session()
    rp = load_robots(session)

    done = missing = failed = 0
    try:
        for i, s in enumerate(todo, 1):
            pause(delay, first=(i == 1))
            status, html = get_page(session, rp, BASE + FIN_PATH_OVERRIDES.get(s, FIN_PATH.format(symbol=s)))
            if status == "ok":
                (FIN_DIR / f"{s}.html").write_text(html, encoding="utf-8")
                done += 1
                print(f"[{i}/{len(todo)}] {s:8} saved ({len(html) // 1024} KB)")
            elif status == "missing":
                missing += 1
                with open(MISSING_FILE, "a") as m:
                    m.write(s + "\n")
                print(f"[{i}/{len(todo)}] {s:8} no financials page (404) - noted, will not ask again")
            else:
                failed += 1
                print(f"[{i}/{len(todo)}] {s:8} failed - see {ERROR_LOG}")
    except KeyboardInterrupt:
        print("\ninterrupted - everything saved so far is kept")
    finally:
        print(f"\nsaved {done}, no page {missing}, failed {failed}, skipped {skipped}")


def cmd_import(args):
    """Copy pages you saved from your browser into the cache. Makes no requests."""
    FIN_DIR.mkdir(parents=True, exist_ok=True)
    for p in args.files:
        path = pathlib.Path(p)
        html = path.read_text(encoding="utf-8", errors="replace")
        m = re.search(r'rel="canonical" href="https://stockanalysis\.com([^"]+)"', html)
        if not m:
            print(f"{path.name}: no canonical address found - skipped")
            continue
        addr = m.group(1)
        sym = re.fullmatch(r"/quote/egx/([A-Za-z0-9\-]+)/financials/", addr)
        if addr == LIST_PATH:
            shutil.copyfile(path, LIST_FILE)
            print(f"{path.name}: imported as the list page")
        elif sym:
            shutil.copyfile(path, FIN_DIR / f"{sym.group(1).upper()}.html")
            print(f"{path.name}: imported as financials for {sym.group(1).upper()}")
        else:
            print(f"{path.name}: {addr} is not a page this tool uses - skipped")


def main():
    ap = argparse.ArgumentParser(description="Polite, cache-first downloader for StockAnalysis pages")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("list", help="download the list of all EGX stocks")
    a.add_argument("--refresh", action="store_true")
    a.set_defaults(fn=cmd_list)

    b = sub.add_parser("fin", help="download financials pages")
    b.add_argument("symbols", nargs="*")
    b.add_argument("--all", action="store_true", help="every symbol in the saved list page")
    b.add_argument("--max", type=int, default=0, help="download at most this many this run")
    b.add_argument("--delay", type=float, default=DEFAULT_DELAY, help=f"seconds between requests (minimum {MIN_DELAY:.0f})")
    b.add_argument("--refresh", action="store_true", help="re-download even if saved recently")
    b.set_defaults(fn=cmd_fin)

    c = sub.add_parser("import", help="add pages you saved by hand from your browser")
    c.add_argument("files", nargs="+")
    c.set_defaults(fn=cmd_import)

    args = ap.parse_args()
    if args.cmd in ("list", "fin") and "CHANGE-ME" in CONTACT and BASE.startswith("https://stockanalysis.com"):
        sys.exit("Before downloading, edit CONTACT near the top of sa_download.py and put your GitHub project URL there.")
    try:
        args.fn(args)
    except StopDownloading as e:
        print(f"\nSTOPPED: {e}")
        sys.exit(2)


if __name__ == "__main__":
    main()
