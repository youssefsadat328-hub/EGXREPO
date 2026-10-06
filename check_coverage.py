"""
check_coverage.py - which stocks does each data source cover, and do the prices agree?

No network. It only reads what the earlier steps saved:
    data/html/list.html, data/html/fin/*.html      (StockAnalysis)
    data/raw/prices/<SYMBOL>.csv                   (Yahoo)

It answers:
  1. Which stocks have NO financials page on StockAnalysis?        (hurts measurements 1 and 2)
  2. Which stocks have NO price history on Yahoo?                  (hurts measurements 3 and 4)
  3. How big are the stocks that are missing? (a missing giant matters, a missing micro-cap barely does)
  4. Do Yahoo's closing prices agree with the StockAnalysis list page?
  5. Does Yahoo's COMI volume agree with the exchange's own figure for 30 Sep 2026?

Usage:  python check_coverage.py
"""
import pathlib

import pandas as pd

from sa_parse import parse_list

ROOT = pathlib.Path(__file__).parent
LIST_FILE = ROOT / "data" / "html" / "list.html"
FIN_DIR = ROOT / "data" / "html" / "fin"
PRICE_DIR = ROOT / "data" / "raw" / "prices"

# From the EGX's own page for COMI, "as of last trading date: Sep 30, 2026" (your screenshot)
EGX_COMI_DATE, EGX_COMI_CLOSE, EGX_COMI_VOLUME = "2026-09-30", 126.3, 4_395_320


def money(x):
    if x is None:
        return "n/a"
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(x) >= div:
            return f"{x / div:.1f}{suf}"
    return f"{x:.0f}"


def show(title, symbols, by_sym, extra=None, limit=40):
    print(f"\n{title}: {len(symbols)}")
    ranked = sorted(symbols, key=lambda s: -(by_sym[s]["market_cap"] or 0))
    for s in ranked[:limit]:
        tag = f"   [{extra(s)}]" if extra else ""
        print(f"   {s:7} {money(by_sym[s]['market_cap']):>8} EGP   {by_sym[s]['name'][:48]}{tag}")
    if len(ranked) > limit:
        print(f"   ... and {len(ranked) - limit} smaller ones")


def main():
    rows = parse_list(LIST_FILE.read_text(encoding="utf-8"))
    by_sym = {r["symbol"]: r for r in rows}
    total_cap = sum(r["market_cap"] or 0 for r in rows)
    fin_have = {p.stem for p in FIN_DIR.glob("*.html")}
    price_have = {p.stem for p in PRICE_DIR.glob("*.csv") if "." not in p.stem}  # ignores old COMI.CA.csv files

    sa_missing = [s for s in by_sym if s not in fin_have]
    yh_missing = [s for s in by_sym if s not in price_have]
    both = [s for s in sa_missing if s in yh_missing]

    print(f"stocks listed: {len(rows)}")
    print(f"StockAnalysis financials: {len(fin_have & set(by_sym))} have a page, {len(sa_missing)} do not")
    print(f"Yahoo prices:             {len(price_have & set(by_sym))} have data,   {len(yh_missing)} do not")
    for label, miss in (("StockAnalysis-missing", sa_missing), ("Yahoo-missing", yh_missing)):
        cap = sum(by_sym[s]["market_cap"] or 0 for s in miss)
        print(f"   {label} stocks hold {cap / total_cap:.1%} of total market cap")
    print(f"missing from BOTH (these get no useful score at all): {len(both)}")

    show("Yahoo has no prices for", yh_missing, by_sym,
         extra=lambda s: "has financials" if s in fin_have else "no financials either")
    show("StockAnalysis has no financials page for", sa_missing, by_sym,
         extra=lambda s: "has prices" if s in price_have else "no prices either")

    # ---- do the two sources agree on price?
    checked = matched = 0
    mismatches, short, stale = [], [], []
    newest = None
    series = {}
    for s in price_have & set(by_sym):
        try:
            px = pd.read_csv(PRICE_DIR / f"{s}.csv", index_col=0, parse_dates=True)
        except Exception:
            continue
        if px.empty:
            continue
        series[s] = px
        newest = px.index[-1] if newest is None else max(newest, px.index[-1])
    for s, px in series.items():
        if len(px) < 250:
            short.append((s, len(px)))
        if (newest - px.index[-1]).days > 7:
            stale.append((s, str(px.index[-1].date())))
        ref = by_sym[s]["price"]
        if not ref:
            continue
        checked += 1
        recent = px["Close"].tail(5)
        best = (recent / ref - 1).abs().min()
        if best <= 0.005:
            matched += 1
        else:
            mismatches.append((best, s, ref, float(px["Close"].iloc[-1])))

    print(f"\nprice agreement: {matched} of {checked} stocks have a Yahoo close (last 5 sessions) within 0.5% "
          f"of the StockAnalysis list price")
    if mismatches:
        print("   biggest disagreements (StockAnalysis price vs Yahoo latest close):")
        for best, s, ref, last in sorted(mismatches, reverse=True)[:12]:
            print(f"   {s:7} StockAnalysis {ref:>9.3f}   Yahoo {last:>9.3f}   ({best:.1%} off the closest of 5 sessions)")
    if stale:
        print(f"\nYahoo data that stops more than a week before the newest date ({len(stale)}): "
              + ", ".join(f"{s} ({d})" for s, d in sorted(stale)[:15]))
    if short:
        print(f"\nless than 250 trading days of history ({len(short)}): "
              + ", ".join(f"{s} ({n})" for s, n in sorted(short, key=lambda x: x[1])[:15]))

    # ---- volume check against the exchange's own number
    print("\nCOMI volume check against the exchange:")
    px = series.get("COMI")
    if px is None:
        print("   no COMI price file")
    elif pd.Timestamp(EGX_COMI_DATE) not in px.index:
        print(f"   Yahoo has no row for {EGX_COMI_DATE}")
    else:
        r = px.loc[pd.Timestamp(EGX_COMI_DATE)]
        dv = r["Volume"] / EGX_COMI_VOLUME - 1
        dc = r["Close"] / EGX_COMI_CLOSE - 1
        print(f"   {EGX_COMI_DATE}: Yahoo close {r['Close']:.2f} vs exchange {EGX_COMI_CLOSE}  ({dc:+.2%})")
        print(f"   {EGX_COMI_DATE}: Yahoo volume {r['Volume']:,.0f} vs exchange {EGX_COMI_VOLUME:,}  ({dv:+.1%})")
        print("   -> " + ("volume agrees closely" if abs(dv) <= 0.05 else
                          "volume is noticeably different: Yahoo volume may not be reliable"))


if __name__ == "__main__":
    main()
