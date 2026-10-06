"""
sa_parse.py - turn saved StockAnalysis pages into clean Python data.

Works on HTML text only (no network), so it can be tested on pages you saved
by hand. Two page types:

  parse_list(html)        https://stockanalysis.com/list/egyptian-stock-exchange/
  parse_financials(html)  https://stockanalysis.com/quote/egx/<SYMBOL>/financials/

Quick test on saved files:
    python sa_parse.py "List of Stocks on the Egyptian Stock Exchange (EGX).html"
    python sa_parse.py "Commercial International Bank ... Financials Overview.html"

Source of the data: StockAnalysis.com. Credit it wherever you show the results.
"""
import re
import sys

from bs4 import BeautifulSoup

UNIT_WORDS = {"": 1, "thousands": 1e3, "millions": 1e6, "billions": 1e9}
SUFFIX = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}

# The site's row labels can differ a little between company types; try each.
NET_INCOME_LABELS = ["Net Income", "Net Income to Common"]
REVENUE_LABELS = ["Revenue"]
EPS_LABELS = ["Earnings Per Share", "EPS (Diluted)", "EPS"]
DPS_LABELS = ["Dividend Per Share"]
YIELD_LABELS = ["Dividend Yield"]
PE_LABELS = ["PE Ratio"]


# ---------------------------------------------------------------- numbers
def parse_number(text):
    """'1,234' -> 1234.0   '24.41%' -> 24.41   '434.80B' -> 4.348e11
    '-' or '' -> None.   Percentages stay in percent units (24.41, not 0.2441)."""
    if text is None:
        return None
    t = text.strip().replace("−", "-").replace("–", "-").replace(",", "")
    if t in ("", "-", "--", "n/a", "N/A", "—"):
        return None
    mult = 1.0
    if t.endswith("%"):
        t = t[:-1]
    elif t[-1] in SUFFIX and re.fullmatch(r"-?\d+(\.\d+)?[KMBT]", t):
        mult = SUFFIX[t[-1]]
        t = t[:-1]
    try:
        return float(t) * mult
    except ValueError:
        return None


def _text(el):
    return " ".join(el.get_text(" ", strip=True).split())


# ---------------------------------------------------------------- list page
def parse_list(html):
    """Return a list of dicts: symbol, name, market_cap, price, change_pct, revenue_ttm."""
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table")
    if table is None:
        raise ValueError("no table found - is this the list page?")
    heads = [_text(th) for th in table.find("thead").find_all("th")]
    col = {h: i for i, h in enumerate(heads)}
    need = ["Symbol", "Company Name", "Market Cap", "Stock Price", "% Change", "Revenue"]
    missing = [n for n in need if n not in col]
    if missing:
        raise ValueError(f"list page is missing columns {missing}; found {heads}")

    rows = []
    for tr in table.find("tbody").find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < len(heads):
            continue
        a = tds[col["Symbol"]].find("a")
        symbol = _text(a) if a else _text(tds[col["Symbol"]])
        rows.append({
            "symbol": symbol,
            "name": _text(tds[col["Company Name"]]),
            "market_cap": parse_number(_text(tds[col["Market Cap"]])),
            "price": parse_number(_text(tds[col["Stock Price"]])),
            "change_pct": parse_number(_text(tds[col["% Change"]])),
            "revenue_ttm": parse_number(_text(tds[col["Revenue"]])),
        })
    return rows


# ---------------------------------------------------------- financials page
def parse_financials(html):
    """Return {'meta': {...}, 'tables': [ {period: ..., rows: {label: {period: value}}} ]}.
    Values keep the page's own units (see meta['unit_multiplier'])."""
    soup = BeautifulSoup(html, "lxml")

    meta = {}
    m = re.search(r'rel="canonical" href="https://stockanalysis\.com/quote/egx/([A-Za-z0-9\-]+)/', html)
    meta["symbol"] = m.group(1) if m else None
    m = re.search(r"curr:\{main:\"(\w+)\",price:\"(\w+)\",dividend:\"(\w+)\",financial:\"(\w+)\"\}", html)
    meta["currency_price"] = m.group(2) if m else None
    meta["currency_financial"] = m.group(4) if m else None
    m = re.search(r'template:"([^"]+)"', html)
    meta["template"] = m.group(1) if m else None
    m = re.search(r'apiext:"([^"]*)"', html)
    meta["yahoo_suffix"] = m.group(1) if m else None
    m = re.search(r"Financials in (?:(thousands|millions|billions)\s+)?([A-Z]{3})\.\s*Fiscal year is ([A-Za-z]+ - [A-Za-z]+)", html)
    if m:
        meta["unit_multiplier"] = UNIT_WORDS[m.group(1) or ""]
        meta["unit_currency"] = m.group(2)
        meta["fiscal_year"] = m.group(3)
    else:
        meta["unit_multiplier"] = None  # unknown -> caller must not trust magnitudes
        meta["unit_currency"] = None
        meta["fiscal_year"] = None

    tables = []
    for table in soup.find_all("table"):
        thead = table.find("thead")
        tbody = table.find("tbody")
        if thead is None or tbody is None:
            continue
        head_rows = thead.find_all("tr")
        periods = [_text(th) for th in head_rows[0].find_all("th")][1:]
        ends = {}
        if len(head_rows) > 1:
            for p, th in zip(periods, head_rows[1].find_all("th")[1:]):
                spans = th.find_all("span")  # [short "Jun '26", long "Jun 30, 2026"]
                ends[p] = _text(spans[-1]) if spans else _text(th)
        rows = {}
        for tr in tbody.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) != len(periods) + 1:
                continue
            label_el = tds[0].find("div", class_=re.compile(r"truncate"))
            label = _text(label_el) if label_el else _text(tds[0])
            if label and label not in rows:  # first occurrence wins
                rows[label] = {p: parse_number(_text(td)) for p, td in zip(periods, tds[1:])}
        if rows:
            tables.append({"periods": periods, "period_end": ends, "rows": rows})
    return {"meta": meta, "tables": tables}


def _find_row(parsed, labels):
    for t in parsed["tables"]:
        for lab in labels:
            if lab in t["rows"]:
                return t["rows"][lab], t["period_end"]
    return None, {}


def summarize_financials(parsed):
    """Pick out the numbers the scorer needs. Money values are converted to full
    currency units (EGP), percentages stay in percent."""
    meta = parsed["meta"]
    mult = meta.get("unit_multiplier")

    def money(v):
        return None if v is None or mult is None else v * mult

    ni_row, ends = _find_row(parsed, NET_INCOME_LABELS)
    rev_row, _ = _find_row(parsed, REVENUE_LABELS)
    eps_row, _ = _find_row(parsed, EPS_LABELS)
    dps_row, _ = _find_row(parsed, DPS_LABELS)
    yld_row, _ = _find_row(parsed, YIELD_LABELS)
    pe_row, _ = _find_row(parsed, PE_LABELS)

    def by_year(row):
        out = {}
        for p, v in (row or {}).items():
            m = re.fullmatch(r"FY (\d{4})", p)
            if m and v is not None:
                out[m.group(1)] = v
        return out

    def current(row):
        if not row:
            return None
        for p in ("Current", "TTM"):
            if p in row:
                return row[p]
        return None

    ni_years = {y: money(v) for y, v in by_year(ni_row).items()}
    return {
        "symbol": meta.get("symbol"),
        "currency_price": meta.get("currency_price"),
        "currency_financial": meta.get("currency_financial"),
        "template": meta.get("template"),
        "fiscal_year": meta.get("fiscal_year"),
        "yahoo_suffix": meta.get("yahoo_suffix"),
        "unit_multiplier": mult,
        "net_income": ni_years,
        "net_income_ttm": money((ni_row or {}).get("TTM")),
        "revenue_ttm": money((rev_row or {}).get("TTM")),
        "eps_ttm": (eps_row or {}).get("TTM"),
        "pe_current": current(pe_row),
        "pe_by_year": by_year(pe_row),
        "dividend_per_share": current(dps_row),
        "dividend_yield_pct": current(yld_row),
        "latest_period_end": ends.get("TTM") or ends.get("FY " + (max(ni_years) if ni_years else "")),
    }


# ----------------------------------------------------------------- checks
def check_record(list_row, fin):
    """Cross-check one stock. Returns a list of warning strings (empty = clean).
    These feed the confidence flag shown next to each score."""
    w = []
    if fin.get("unit_multiplier") is None:
        w.append("units not detected on the financials page")
    if fin.get("currency_financial") and fin.get("currency_price") \
            and fin["currency_financial"] != fin["currency_price"]:
        w.append(f"reports in {fin['currency_financial']} but trades in {fin['currency_price']}")

    price, eps, pe = list_row.get("price"), fin.get("eps_ttm"), fin.get("pe_current")
    if price and eps and eps > 0 and pe:
        ratio = (price / eps) / pe
        if abs(ratio - 1) > 0.05:
            w.append(f"P/E on page differs from price/EPS by {abs(ratio - 1):.0%}")

    rev_list, rev_fin = list_row.get("revenue_ttm"), fin.get("revenue_ttm")
    if rev_list and rev_fin:
        ratio = rev_fin / rev_list
        if not 0.9 <= ratio <= 1.1:
            w.append(f"revenue on financials page is {ratio:.2f}x the list page (unit problem?)")

    if len(fin.get("net_income", {})) < 3:
        w.append("fewer than 3 years of net income")
    return w


# ------------------------------------------------------------------ demo
def _demo(path):
    import pathlib
    if not pathlib.Path(path).exists():          # allow:  python sa_parse.py MOIL
        guess = pathlib.Path(__file__).parent / "data" / "html" / "fin" / f"{path.upper()}.html"
        if guess.exists():
            path = str(guess)
    html = open(path, encoding="utf-8", errors="replace").read()
    if "/list/egyptian-stock-exchange/" in html[:400]:
        rows = parse_list(html)
        print(f"{len(rows)} stocks. First 5 and last 2:")
        for r in rows[:5] + rows[-2:]:
            print("  ", r)
        syms = [r["symbol"] for r in rows]
        print("duplicate symbols:", len(syms) - len(set(syms)))
        print("rows missing price:", sum(r["price"] is None for r in rows))
        print("rows missing revenue:", sum(r["revenue_ttm"] is None for r in rows))
    else:
        parsed = parse_financials(html)
        print("meta:", parsed["meta"])
        print("tables:", len(parsed["tables"]))
        s = summarize_financials(parsed)
        for k, v in s.items():
            print(f"  {k}: {v}")


if __name__ == "__main__":
    for p in sys.argv[1:]:
        print("=" * 70, "\n", p)
        _demo(p)


# ----------------------------------------------------------------- price history
def parse_history(html):
    """Daily prices embedded in a StockAnalysis /history/ page.

    The server puts about 6 months of daily rows in the page (about 120 trading days).
    Longer ranges are loaded later by the browser and are NOT in the downloaded page.
    Returns a DataFrame indexed by date with Open, High, Low, Close, Volume (oldest first),
    or an empty DataFrame if the page has no such block.
    """
    import pandas as pd
    start = html.find("data:[{a:")
    if start < 0:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    end = html.find("}]", start)
    block = html[start:end + 1]
    recs = []
    for obj in re.findall(r"\{([^{}]*)\}", block):
        d = dict(re.findall(r'(\w+):("[^"]*"|[-+\d.eE]+)', obj))
        try:
            recs.append({
                "Date": d["t"].strip('"'), "Open": float(d["o"]), "High": float(d["h"]),
                "Low": float(d["l"]), "Close": float(d["c"]), "Volume": float(d["v"]),
            })
        except (KeyError, ValueError):
            continue
    if not recs:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    df = pd.DataFrame(recs)
    df["Date"] = pd.to_datetime(df["Date"])
    return df.drop_duplicates("Date").set_index("Date").sort_index()
