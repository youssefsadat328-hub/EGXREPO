"""
sa_prices.py - fill the price gaps: daily prices from StockAnalysis for the stocks
Yahoo does not have.

Why this exists: fetch_prices.py gets prices from Yahoo, but Yahoo has no data for about
30 EGX stocks. StockAnalysis shows daily prices on each stock's History page, but only
one page of the table at a time, drawn by the browser. A plain download cannot click
"Next", so this script opens the page in a real (headless) browser, picks Daily + 5 Years,
and reads the table page by page - what a person does with the mouse, once per stock.

It follows the same rules as sa_download.py:
  * reads robots.txt first and skips any URL it disallows
  * identifies itself honestly in the User-Agent (set your project URL in SA_CONTACT)
  * waits several seconds between stocks
  * saves each stock as soon as it is done, and never asks twice for a stock it already has
  * STOPS at the first 403/429/5xx, or at a page without the expected table. It never works
    around a block.

One-time setup (in your .venv):
    pip install playwright
    playwright install chromium

Usage (from your project folder):
    python sa_prices.py COMI                  # try one stock first and look at the result
    python sa_prices.py                       # every stock with no price file that HAS a financials page
    python sa_prices.py --include-no-financials   # also the ones with no financials (rarely useful)
    python sa_prices.py --max 5 --delay 6
    python sa_prices.py COMI --show           # watch the browser window while it works

Output: data/raw/prices/<SYMBOL>.csv  (Date,Open,High,Low,Close,Volume - same as the Yahoo files)
        data/raw/sa_prices_status.csv (one line per stock: rows, first/last date, note)

Do NOT publish these files: StockAnalysis's terms do not allow republishing their data in
bulk. They stay in data/raw/ (already in .gitignore). Credit StockAnalysis.com on your page.
"""
import argparse
import csv
import datetime as dt
import os
import pathlib
import re
import sys
import time
import random

from bs4 import BeautifulSoup

import sa_download as sd
from sa_parse import parse_list, parse_number

ROOT = pathlib.Path(__file__).parent
PRICE_DIR = ROOT / "data" / "raw" / "prices"
STATUS_FILE = ROOT / "data" / "raw" / "sa_prices_status.csv"
FIN_DIR = sd.FIN_DIR

HIST_PATH = "/quote/egx/{symbol}/history/"
WANT_RANGE = "5 Years"         # the longest range the free page offers (your check: up to 5 years)
MIN_ROWS_OK = 120              # fewer rows than this: saved, but flagged as short
MAX_PAGES = 40                 # safety stop: 40 pages x 100 rows is far more than 5 years
STEP_TIMEOUT_MS = 15000


# ------------------------------------------------------------ parsing (testable offline)
def parse_history_html(html):
    """Rows currently shown in the History table -> list of dicts (newest first, as on the page).
    Also returns (page, pages) from the 'Page X of N' text, or (None, None)."""
    soup = BeautifulSoup(html, "lxml")
    rows, seen = [], set()
    # Look at EVERY table row on the page, not just the first table: once you scroll, the site adds a
    # floating copy of the header row (a second table) in front of the real one.
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td"])]
        if len(cells) < 8:
            continue
        try:
            d = dt.datetime.strptime(cells[0], "%b %d, %Y").date()
        except ValueError:
            continue
        o, h, l, c = (parse_number(x) for x in cells[1:5])
        vol = parse_number(cells[7])
        if None in (o, h, l, c) or d in seen:
            continue
        seen.add(d)
        rows.append({"date": d, "open": o, "high": h, "low": l, "close": c, "volume": vol or 0})
    text = soup.get_text(" ", strip=True)
    m = re.search(r"Page\s+(\d+)\s+of\s+(\d+)", text)
    page, pages = (int(m.group(1)), int(m.group(2))) if m else (None, None)
    return rows, page, pages


def write_csv(path, rows):
    rows = sorted({r["date"]: r for r in rows}.values(), key=lambda r: r["date"])
    PRICE_DIR.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Date", "Open", "High", "Low", "Close", "Volume"])
        for r in rows:
            w.writerow([r["date"].isoformat(), r["open"], r["high"], r["low"], r["close"], int(r["volume"])])
    return rows


# ------------------------------------------------------------ browser steps
def choose_menu_option(page, button_pattern, option_text):
    """Open the dropdown whose button text matches button_pattern and click option_text."""
    btn = page.locator("button[aria-haspopup='menu']").filter(has_text=re.compile(button_pattern)).first
    btn.click(timeout=STEP_TIMEOUT_MS)
    opt = page.get_by_text(option_text, exact=True).last
    opt.click(timeout=STEP_TIMEOUT_MS)


# Free options on the site: 3 Months, 6 Months, Year to Date, 1 Year, 5 Years.
# "10 Years" and "Full History" are locked behind the paid plan, so they are never tried.
RANGE_PREFERENCE = ["5 Years", "1 Year", "6 Months", "3 Months"]
RANGE_BUTTON = re.compile(r"Months?|Years?|YTD|Max|All|^\s*\d+[MY]\s*$")


def range_button(page):
    return page.locator("button[aria-haspopup='menu']").filter(has_text=RANGE_BUTTON).first


def current_range(page):
    try:
        return range_button(page).inner_text(timeout=3000).strip()
    except Exception:
        return ""


def click_option(page, text):
    """Click a menu entry called `text`. We do not know whether the site draws it as a menuitem,
    option, button or plain text, so try each in turn. Returns True if something was clicked."""
    candidates = [
        page.get_by_role("menuitem", name=text, exact=True),
        page.get_by_role("option", name=text, exact=True),
        page.get_by_role("button", name=text, exact=True),
        page.get_by_text(text, exact=True),
    ]
    for loc in candidates:
        try:
            n = loc.count()
            for i in range(n - 1, -1, -1):          # last first: the menu is drawn after the toggle button
                el = loc.nth(i)
                if el.is_visible():
                    el.click(timeout=4000)
                    return True
        except Exception:
            continue
    return False


def dump_menu(page, symbol, tag):
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(DEBUG_DIR / f"{tag}_{symbol}.png"))
        (DEBUG_DIR / f"{tag}_{symbol}.html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass


def set_range(page, symbol="", debug=False):
    """Select Daily (usually the default) and the longest range this stock offers.
    Tries each range from the longest down, and checks the dropdown label really changed.
    Returns a note ('' if all went well)."""
    notes = []
    try:
        if page.locator("button[aria-haspopup='menu']").filter(has_text=re.compile(r"^\s*Daily\s*$")).count() == 0:
            range_button_daily = page.locator("button[aria-haspopup='menu']").filter(
                has_text=re.compile(r"Weekly|Monthly|Quarterly|Annual|Yearly")).first
            range_button_daily.click(timeout=STEP_TIMEOUT_MS)
            click_option(page, "Daily")
            page.wait_for_timeout(800)
    except Exception:
        notes.append("could not confirm Daily")

    start = current_range(page)
    chosen = None
    for pref in RANGE_PREFERENCE:
        if pref == start:
            chosen = start                       # nothing longer was accepted; this is where we are
            break
        try:
            range_button(page).click(timeout=STEP_TIMEOUT_MS)
            page.wait_for_timeout(500)
            if debug and pref == RANGE_PREFERENCE[0]:
                dump_menu(page, symbol, "menu")  # what the open menu looks like (for diagnosis)
            if click_option(page, pref):
                # a long range can take a few seconds to load, and the label may change only then
                end = time.time() + 10
                while time.time() < end:
                    now = current_range(page)
                    if (re.match(r"^\s*" + pref.split()[0] + r"\s*(years?|y)\b", now, re.I) if "Year" in pref
                            else now.lower().startswith(pref.lower())):
                        chosen = pref
                        break
                    page.wait_for_timeout(400)
                if chosen:
                    page.wait_for_timeout(1000)
                    break
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
        except Exception:
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
    final = current_range(page)
    if chosen is None or final == "" :
        notes.append(f"could not change the range (stayed at '{final or start}')")
        dump_menu(page, symbol, "menu")
    elif chosen != "5 Years":
        notes.append(f"longest range accepted was {chosen}")
        if chosen == "6 Months":
            dump_menu(page, symbol, "menu")
    return "; ".join(notes)


def wait_for_rows(page, timeout_s=25):
    """The table is redrawn after a range change; wait until it has real rows again."""
    end = time.time() + timeout_s
    while time.time() < end:
        rows, _, _ = parse_history_html(page.content())
        if rows:
            return True
        page.wait_for_timeout(500)
    return False


def try_more_rows(page):
    """If the 'N Rows' dropdown offers a bigger size, pick the largest (fewer clicks)."""
    try:
        btn = page.locator("button[aria-haspopup='menu']").filter(has_text=re.compile(r"\d+\s*Rows")).first
        btn.click(timeout=5000)
        options = page.get_by_text(re.compile(r"^\s*\d+\s*Rows\s*$")).all_text_contents()
        sizes = sorted({int(re.search(r"\d+", o).group()) for o in options if re.search(r"\d+", o)})
        if sizes:
            page.get_by_text(f"{sizes[-1]} Rows", exact=True).last.click(timeout=5000)
            page.wait_for_timeout(800)
        else:
            page.keyboard.press("Escape")
    except Exception:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass


def settled_rows(page, prev_first=None, timeout_s=25):
    """Wait until the table shows real rows that have stopped changing, then return them.
    The site redraws the table after every click (often through a blank or 'loading' state),
    so one look right after a click is not reliable. If prev_first is given, the first date on
    screen must also differ from it (i.e. the new page has really arrived).
    Returns (rows, page_no, total_pages), or ([], None, None) if it never settled."""
    end = time.time() + timeout_s
    last_sig = None
    while time.time() < end:
        rows, cur, total = parse_history_html(page.content())
        if rows and (prev_first is None or rows[0]["date"] != prev_first):
            sig = (rows[0]["date"], rows[-1]["date"], len(rows), cur)
            if sig == last_sig:                 # same on two looks in a row: it has settled
                return rows, cur, total
            last_sig = sig
        else:
            last_sig = None
        page.wait_for_timeout(700)
    return [], None, None


def read_all_pages(page):
    """Click through every page of the table. Returns (rows, note)."""
    all_rows, note = [], ""
    rows, cur, total = settled_rows(page)
    prev_first = None
    for n in range(MAX_PAGES):
        if not rows:
            note = "table had no readable rows" if not all_rows else "next page never loaded"
            break
        all_rows += rows
        prev_first = rows[0]["date"]
        if total is None or (cur is not None and cur >= total):
            break
        nxt = page.locator("nav button[aria-label='Next']")
        if nxt.count() == 0:
            nxt = page.get_by_role("button", name="Next", exact=True)
        if nxt.count() == 0 or nxt.first.is_disabled():
            break
        nxt.first.scroll_into_view_if_needed(timeout=STEP_TIMEOUT_MS)
        nxt.first.click(timeout=STEP_TIMEOUT_MS)
        time.sleep(random.uniform(0.3, 0.8))
        rows, cur, total = settled_rows(page, prev_first=prev_first)
    else:
        note = f"stopped after {MAX_PAGES} pages"
    if total and cur and cur < total and not note:
        note = f"read {cur} of {total} pages"
    return all_rows, note


DEBUG_DIR = ROOT / "data" / "raw" / "debug"
DEBUG_MENU = False   # set by --debug-menu


def dump_debug(page, symbol):
    """Keep a screenshot and the page text of a failed stock so we can see what the page looked like."""
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(DEBUG_DIR / f"{symbol}.png"), full_page=True)
        (DEBUG_DIR / f"{symbol}.html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass


def fetch_stock(context, rp, symbol):
    """Return ('ok'|'missing', rows, note). Raises sd.StopDownloading if the site pushes back."""
    url = sd.BASE + HIST_PATH.format(symbol=symbol)
    if not rp.can_fetch(sd.USER_AGENT, url):
        raise sd.StopDownloading(f"robots.txt disallows {url}")
    page = context.new_page()
    problems = []                                  # failed requests to the site itself (not ads)
    host = sd.BASE.split("//", 1)[-1].split("/")[0]

    def on_response(r):
        try:
            if r.status >= 400 and host in r.url:
                problems.append((r.status, r.url))
        except Exception:
            pass
    page.on("response", on_response)
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
        status = resp.status if resp else 0
        if status in (403, 429) or status >= 500:
            raise sd.StopDownloading(f"HTTP {status} from {url} - the site is pushing back, so stopping. "
                                     "Wait hours before running again and use a larger --delay.")
        if status == 404:
            return "missing", [], "no History page (404)"
        try:
            page.wait_for_selector("table tbody tr", timeout=STEP_TIMEOUT_MS)
        except Exception:
            raise sd.StopDownloading(f"{url} has no data table (blocked, challenge page or layout change?). "
                                     "Stopping; not trying to get around it.")
        note = set_range(page, symbol, debug=DEBUG_MENU)
        if not wait_for_rows(page):
            dump_debug(page, symbol)
            return "ok", [], "; ".join(n for n in (note, f"table stayed empty - see {DEBUG_DIR.name}/{symbol}.png") if n)
        try_more_rows(page)
        rows, note2 = read_all_pages(page)
        pushed = [p for p in problems if p[0] in (403, 429) or p[0] >= 500]
        if pushed:
            dump_debug(page, symbol)
            raise sd.StopDownloading(f"HTTP {pushed[0][0]} from {pushed[0][1]} while paging {symbol} - the site is "
                                     "pushing back, so stopping. Wait hours before running again.")
        if not rows or "never loaded" in note2 or "no readable" in note2:
            dump_debug(page, symbol)
            if problems:
                note2 = "; ".join(x for x in (note2, f"site answered HTTP {problems[0][0]} for {problems[0][1][:80]}") if x)
        return "ok", rows, "; ".join(n for n in (note, note2) if n)
    finally:
        page.close()


# ------------------------------------------------------------ main
def pick_symbols(args):
    if args.symbols:
        syms = [s.upper() for s in args.symbols]
        bad = [s for s in syms if not re.fullmatch(r"[A-Z0-9\-]+", s)]
        if bad:
            sys.exit(f"unexpected characters in symbols: {bad}")
        return syms, {}
    if not sd.LIST_FILE.exists():
        sys.exit("no saved list page yet - run:  python sa_download.py list")
    rows = parse_list(sd.LIST_FILE.read_text(encoding="utf-8"))
    by_sym = {r["symbol"]: r for r in rows}
    todo = []
    for s in by_sym:
        if (PRICE_DIR / f"{s}.csv").exists():
            continue
        if not args.include_no_financials and not (FIN_DIR / f"{s}.html").exists():
            continue
        todo.append(s)
    todo.sort(key=lambda s: -(by_sym[s]["market_cap"] or 0))   # biggest companies first
    return todo, by_sym


def append_status(rec):
    new = not STATUS_FILE.exists()
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATUS_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "symbol", "rows", "first_date", "last_date", "note"])
        w.writerow(rec)


def main():
    ap = argparse.ArgumentParser(description="Daily prices from StockAnalysis for stocks Yahoo lacks.")
    ap.add_argument("symbols", nargs="*")
    ap.add_argument("--max", type=int, default=0, help="stop after this many stocks")
    ap.add_argument("--delay", type=float, default=sd.DEFAULT_DELAY, help="seconds between stocks")
    ap.add_argument("--include-no-financials", action="store_true")
    ap.add_argument("--refresh", action="store_true", help="download even if a price file exists (named symbols only)")
    ap.add_argument("--show", action="store_true", help="show the browser window")
    ap.add_argument("--debug-menu", action="store_true", help="save a screenshot + page copy of the open range menu")
    args = ap.parse_args()
    global DEBUG_MENU
    DEBUG_MENU = args.debug_menu

    if sd.CONTACT.startswith("CHANGE-ME"):
        sys.exit("Set your project URL first, so the site can see who is asking:\n"
                 "   PowerShell:  $env:SA_CONTACT = 'https://github.com/yourname/egx-scorer'\n"
                 "(or edit CONTACT in sa_download.py)")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("Playwright is not installed. Run:  pip install playwright   then:  playwright install chromium")

    symbols, by_sym = pick_symbols(args)
    if not args.refresh:
        symbols = [s for s in symbols if not (PRICE_DIR / f"{s}.csv").exists()]
    if args.max:
        symbols = symbols[:args.max]
    if not symbols:
        print("nothing to do: every stock already has a price file")
        return
    delay = max(args.delay, sd.MIN_DELAY)
    print(f"{len(symbols)} stock(s): {', '.join(symbols[:12])}{' ...' if len(symbols) > 12 else ''}")
    print(f"about {len(symbols) * (delay + 12) / 60:.0f} minutes at {delay:.0f}s between stocks")

    session = sd.new_session()
    rp = sd.load_robots(session)

    done = skipped = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.show)
        context = browser.new_context(user_agent=sd.USER_AGENT, viewport={"width": 1280, "height": 900})
        try:
            for i, s in enumerate(symbols, 1):
                if i > 1:
                    time.sleep(delay + random.uniform(0, 1.5))
                status, rows, note = fetch_stock(context, rp, s)
                stamp = time.strftime("%Y-%m-%d %H:%M:%S")
                if status != "ok" or not rows:
                    skipped += 1
                    append_status([stamp, s, 0, "", "", note or "no rows"])
                    print(f"[{i}/{len(symbols)}] {s:8} nothing saved ({note or 'no rows'})")
                    continue
                rows = write_csv(PRICE_DIR / f"{s}.csv", rows)
                flag = "" if len(rows) >= MIN_ROWS_OK else f"only {len(rows)} days"
                tail = "; ".join(x for x in (note, flag) if x)
                ref = (by_sym.get(s) or {}).get("price")
                if ref and abs(rows[-1]["close"] / ref - 1) > 0.05:
                    tail = "; ".join(x for x in (tail, f"last close {rows[-1]['close']} vs list price {ref}") if x)
                append_status([stamp, s, len(rows), rows[0]["date"], rows[-1]["date"], tail])
                done += 1
                print(f"[{i}/{len(symbols)}] {s:8} {len(rows)} days, {rows[0]['date']} to {rows[-1]['date']}"
                      + (f"   NOTE: {tail}" if tail else ""))
        except sd.StopDownloading as e:
            print(f"\nSTOPPED: {e}")
        finally:
            browser.close()
    print(f"\nsaved {done}, nothing saved {skipped}. Status log: {STATUS_FILE}")
    print("Next:  python check_coverage.py   then   python score_all.py")


if __name__ == "__main__":
    main()
