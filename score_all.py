"""
score_all.py - score every EGX stock on the four measurements.

Reads (all produced by the earlier steps):
    data/html/list.html            list of all stocks            (sa_download.py list)
    data/html/fin/<SYMBOL>.html    financials pages              (sa_download.py fin --all)
    data/raw/prices/<SYMBOL>.csv   daily prices + volume   (fetch_prices.py = Yahoo, sa_prices.py = StockAnalysis for the gaps)

Writes:
    docs/data/scores.json          what the public web page reads (scores + a few figures)
    data/out/diagnostics.csv       every number behind every score (stays on your computer)

Usage:   python score_all.py

The four measurements (0-100, higher = better) are screening aids, not advice:
  1 profit growth        real (after-inflation) growth of net income over the available years,
                         and in how many of those years profit beat inflation
  2 undervalued          P/E against similar companies, P/E against the stock's own history,
                         and dividend yield
  3 away from speculators  how spiky, volatile, patchy and fast-turning its trading is.
                         (Free float is not available, so this is a proxy.)
  4 no manipulation flags  days at the price limit, big jumps on thin volume, and next-day
                         reversals over the last year. Red flags, not proof.

Data: financials from StockAnalysis.com; prices from Yahoo Finance, with StockAnalysis.com filling the stocks Yahoo lacks.
"""
import json
import math
import pathlib
import re
import time
from collections import Counter

import numpy as np
import pandas as pd

from sa_parse import parse_list, parse_financials, summarize_financials, check_record

ROOT = pathlib.Path(__file__).parent
LIST_FILE = ROOT / "data" / "html" / "list.html"
FIN_DIR = ROOT / "data" / "html" / "fin"
PRICE_DIR = ROOT / "data" / "raw" / "prices"
OUT_JSON = ROOT / "docs" / "data" / "scores.json"
DIAG_CSV = ROOT / "data" / "out" / "diagnostics.csv"

# ======================= settings you can edit =======================
# Egypt CPI inflation, annual average, as decimals. Source: FocusEconomics
# (https://www.focus-economics.com/country-indicator/egypt/inflation/).
# Swap in CAPMAS / CBE figures if you prefer them. A year missing here uses DEFAULT_INFLATION.
INFLATION = {"2022": 0.139, "2023": 0.339, "2024": 0.283, "2025": 0.141}
DEFAULT_INFLATION = 0.14
# Companies that report in dollars are judged in dollars: their profit growth is deflated by US inflation
# (US BLS annual-average CPI, usinflationcalculator.com), not by Egyptian inflation.
US_INFLATION = {"2022": 0.080, "2023": 0.041, "2024": 0.029, "2025": 0.026}
DEFAULT_US_INFLATION = 0.03

MIN_GROWTH_YEARS = 3           # fewer years of profit history -> no growth score
MIN_GROUP = 8                  # compare P/E inside a company type only if it has this many stocks
W_GROWTH = {"real_cagr": 0.7, "years_beating": 0.3}
W_VALUE = {"peer_pe": 0.5, "own_history": 0.3, "dividend": 0.2}

LOOKBACK = 250                 # trading days used for the price-based measurements (~1 year)
MIN_PRICE_DAYS = 100           # fewer days than this (about 5 months) -> no price-based scores
LIMIT_MOVE = 0.095             # a daily move this big counts as "hit the limit" (EGX limit is about 10%)
CORP_ACTION = 0.11             # bigger moves are treated as splits / bonus shares and ignored
BIG_MOVE = 0.05                # "big jump"
USD_EGP_FALLBACK = 50.0        # used only if the rate cannot be worked out from the data
# EGX 30 index members (list supplied by the project owner, October 2026). The index is reviewed
# periodically, so update this list when it changes. Members get an extra note on the page.
# Companies whose StockAnalysis financials come from a different exchange page (their main listing is abroad).
# Their prices and currencies on that page do not match the EGX list page, so the P/E consistency checks
# are not applied to them.
FOREIGN_LISTING = {"ORAS": "Abu Dhabi"}
# Newly listed companies: there are no annual statements or a year of prices yet, so most scores cannot exist.
# Shown instead of the generic "no data" notes. Remove an entry once the company has a year of history.
RECENT_LISTINGS = {"KORA": "listed on EGX in May 2026"}
EGX30 = {"ABUK", "ADIB", "AMOC", "ARCC", "BTFH", "COMI", "EAST", "EFID", "HRHO", "EFIH", "EGAL", "EGCH",
         "EMFD", "FWRY", "GBCO", "HELI", "ISPH", "JUFO", "MCQE", "ORAS", "ORHD", "OIH", "ORWE", "PHDC",
         "CCAP", "RAYA", "TMGH", "ETEL", "RMDA", "VLMR"}
ILLIQ_ZERO_SHARE = 0.5         # no trades on more than half the days -> too illiquid to judge
ILLIQ_VALUE_EGP = 100_000      # average traded value per day below this -> too illiquid to judge
THIN_VALUE_EGP = 250_000       # average daily traded value below this is flagged as very thin
STALE_DAYS = 30                # last price older than this (vs the newest date in the market) = stale
PE_MISMATCH_MAX = 0.25         # P/E on the page vs price/EPS: bigger gap than this -> do not score valuation
PRICE_MATCH_TOL = 0.05         # a price file whose recent closes are not within 5% of the listed price is a different security
FLAG_PENALTY = 10              # manipulation score loses this many points per red flag
# =====================================================================

BAND_NOTE = "Scores are 0-100 screening aids, not investment advice."


# ----------------------------------------------------------- utilities
def pct_rank(values, higher_is_better=True):
    """0-100 rank among the non-missing values (100 = best). Ties share the middle rank."""
    v = pd.Series(values, dtype=float)
    ok = v.notna()
    out = pd.Series(np.nan, index=v.index)
    n = int(ok.sum())
    if n == 0:
        return out
    if n == 1:
        out[ok] = 50.0
        return out
    p = (v[ok].rank(method="average") - 1) / (n - 1) * 100
    out[ok] = p if higher_is_better else 100 - p
    return out


def clean(obj):
    """Make anything JSON-safe: numpy -> python, NaN/inf -> None."""
    if isinstance(obj, dict):
        return {str(k): clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    if isinstance(obj, (np.floating, float)):
        f = float(obj)
        return None if math.isnan(f) or math.isinf(f) else f
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.bool_):
        return bool(obj)
    return obj


def nz(x):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else x


# ------------------------------------------------- measurement 1: growth
def growth_metrics(fin):
    ni = fin.get("net_income") or {}
    years = sorted(ni)[-5:]
    m = {"status": "ok", "n_years": len(years), "real_cagr": None, "years_beating": None,
         "from_year": None, "currency": None}
    cur = (fin.get("currency_financial") or "EGP").upper()
    m["currency"] = cur
    if cur == "USD":
        infl, default_infl = US_INFLATION, DEFAULT_US_INFLATION
    elif cur == "EGP":
        infl, default_infl = INFLATION, DEFAULT_INFLATION
    else:
        m["status"] = "foreign_currency"      # no inflation table for this currency, so no honest growth score
        return m
    if len(years) < MIN_GROWTH_YEARS:
        m["status"] = "short_history"
        return m
    # Use the latest unbroken run of years (a missing year breaks the run) ...
    run = [years[-1]]
    for y in reversed(years[:-1]):
        if int(run[0]) - int(y) == 1:
            run.insert(0, y)
        else:
            break
    gap = len(run) < len(years)
    # ... and start it at the first profitable year (growth from a loss has no meaningful percentage).
    while run and ni[run[0]] <= 0:
        run.pop(0)
    ttm = fin.get("net_income_ttm")
    if not run or ni[run[-1]] <= 0 or (ttm is not None and ttm <= 0):
        m["status"] = "loss_making"
        return m
    if len(run) < MIN_GROWTH_YEARS:
        m["status"] = "gap_in_years" if gap else "turnaround"
        return m
    vals = [ni[y] for y in run]
    deflators = [1 + infl.get(y, default_infl) for y in run[1:]]
    steps = len(vals) - 1
    m["real_cagr"] = ((vals[-1] / vals[0]) / float(np.prod(deflators))) ** (1 / steps) - 1
    beats = [b / a > d for a, b, d in zip(vals, vals[1:], deflators)]
    m["years_beating"] = sum(beats) / len(beats)
    m["years_checked"] = len(beats)
    if len(run) < len(years):
        m["from_year"] = run[0]               # window is shorter than the 5 years available
    return m


# ------------------------------------------------ measurement 2: value
def value_metrics(fin):
    m = {"status": "ok", "pe": None, "own_discount": None, "own_median_pe": None, "dividend_yield": None}
    eps, pe = fin.get("eps_ttm"), fin.get("pe_current")
    if eps is not None and eps <= 0:
        m["status"] = "loss_making"
        return m
    if not pe or pe <= 0:
        m["status"] = "no_pe"
        return m
    m["pe"] = pe
    hist = [v for v in (fin.get("pe_by_year") or {}).values() if v and v > 0]
    if len(hist) >= 3:
        med = float(np.median(hist))
        m["own_median_pe"] = med
        m["own_discount"] = max(-1.0, min(1.0, 1 - pe / med))   # +0.3 = 30% cheaper than its own usual P/E
    m["dividend_yield"] = fin.get("dividend_yield_pct") or 0.0
    return m


# ---------------------------------------- measurements 3 and 4: prices
def market_gap_days(prices):
    """Days on which (nearly) every stock shows zero volume = the data source has a hole, not a quiet market."""
    vol = pd.DataFrame({s: p["Volume"] for s, p in prices.items()})
    if vol.empty:
        return set()
    zero_share = (vol == 0).sum(axis=1) / vol.notna().sum(axis=1)
    return set(zero_share[zero_share >= 0.8].index)


def price_metrics(px, market_cap, gaps):
    p = px[~px.index.isin(gaps)].copy()
    p["ret"] = p["Close"].pct_change()
    p = p.tail(LOOKBACK)
    if len(p) < MIN_PRICE_DAYS:
        return {"status": "short_history", "days": len(p)}
    v = p["Volume"]
    nonzero = v[v > 0]
    r = p["ret"]
    ok = r.abs() <= CORP_ACTION
    traded = p["Close"] * v
    big = (r.abs() >= BIG_MOVE) & ok
    nxt = r.shift(-1)
    limit_days = int(((r.abs() >= LIMIT_MOVE) & ok).sum())
    lowvol_jumps = int((big & (v < v.median())).sum())
    reversals = int((big & (np.sign(nxt) == -np.sign(r)) & (nxt.abs() >= 0.6 * r.abs())).sum())
    return {
        "status": "ok",
        "days": len(p),
        "last_date": str(p.index[-1].date()),
        "spike": (nonzero.quantile(0.99) / nonzero.median()) if len(nonzero) >= 20 else np.nan,
        "volatility": r[ok].std() * np.sqrt(250),
        "zero_share": float((v == 0).mean()),
        "avg_value": float(traded.mean()),
        "turnover": float(traded.sum() / market_cap) if market_cap else np.nan,
        "corp_action_days": int((r.abs() > CORP_ACTION).sum()),
        "limit_days": limit_days,
        "lowvol_jumps": lowvol_jumps,
        "reversals": reversals,
        "flags": limit_days + lowvol_jumps + reversals,
    }


# ---------------------------------------------------------- the engine
def score_universe(rows, fins, prices):
    """rows: {symbol: list-page row}; fins: {symbol: summary}; prices: {symbol: DataFrame}.
    Returns (diagnostics DataFrame, public results dict)."""
    gaps = market_gap_days(prices)
    syms = list(rows)

    g = {s: growth_metrics(fins[s]) for s in syms if s in fins}
    val = {s: value_metrics(fins[s]) for s in syms if s in fins}
    pm = {}
    newest = max((p.index[-1] for p in prices.values() if len(p)), default=None)
    for s in syms:
        if s not in prices:
            continue
        if newest is not None and len(prices[s]) and (newest - prices[s].index[-1]).days > STALE_DAYS:
            pm[s] = {"status": "stale_prices", "last": prices[s].index[-1].date()}   # suspended / stopped trading
            continue
        ref = rows[s].get("price")
        recent = prices[s]["Close"].tail(5)
        if ref and len(recent) and float((recent / ref - 1).abs().min()) > PRICE_MATCH_TOL:
            # e.g. SEIGA / ORAS: the price file belongs to another security or currency
            pm[s] = {"status": "price_mismatch"}
            continue
        pm[s] = price_metrics(prices[s], rows[s].get("market_cap"), gaps)

    df = pd.DataFrame(index=syms)
    df["name"] = [rows[s]["name"] for s in syms]
    df["template"] = [fins[s].get("template") if s in fins else None for s in syms]
    df["growth_status"] = [g[s]["status"] if s in g else "no_financials" for s in syms]
    df["real_cagr"] = [g[s]["real_cagr"] if s in g else np.nan for s in syms]
    df["years_beating"] = [g[s]["years_beating"] if s in g else np.nan for s in syms]
    df["growth_from"] = [g[s]["from_year"] if s in g else None for s in syms]
    df["growth_cur"] = [g[s]["currency"] if s in g else None for s in syms]
    df["growth_years"] = [g[s]["n_years"] if s in g else 0 for s in syms]
    df["value_status"] = [val[s]["status"] if s in val else "no_financials" for s in syms]
    df["pe"] = [val[s]["pe"] if s in val else np.nan for s in syms]
    df["own_discount"] = [val[s]["own_discount"] if s in val else np.nan for s in syms]
    df["dividend_yield"] = [val[s]["dividend_yield"] if s in val else np.nan for s in syms]
    df["price_status"] = [pm[s]["status"] if s in pm else "no_prices" for s in syms]
    for col in ["days", "spike", "volatility", "zero_share", "avg_value", "turnover",
                "corp_action_days", "limit_days", "lowvol_jumps", "reversals", "flags"]:
        df[col] = [pm[s].get(col, np.nan) if s in pm else np.nan for s in syms]
    df["warnings"] = [
        "; ".join(check_record(rows[s], fins[s])) if s in fins else "" for s in syms
    ]

    # ---- valuation is skipped when the stock's own numbers disagree with each other
    def bad_value_data(w):
        if not w:
            return False
        if re.search(r"reports in \w+ but trades in \w+", w):          # EPS and price in different currencies
            return True
        return any(int(x) / 100 > PE_MISMATCH_MAX for x in re.findall(r"price/EPS by (\d+)%", w))
    unreliable = df["warnings"].map(bad_value_data) & df["value_status"].eq("ok") & ~df.index.isin(list(FOREIGN_LISTING))
    df.loc[unreliable, "value_status"] = "pe_unreliable"

    # ---- 1. growth score
    ok_g = df["growth_status"].eq("ok") & df["real_cagr"].notna()
    cagr_rank = pct_rank(df["real_cagr"].where(ok_g))
    beat = (df["years_beating"].where(ok_g) * 100)
    growth = W_GROWTH["real_cagr"] * cagr_rank + W_GROWTH["years_beating"] * beat
    growth[df["growth_status"].eq("loss_making")] = 0.0

    # ---- 2. value score
    ok_v = df["value_status"].eq("ok") & df["pe"].notna()
    pe_ok = df["pe"].where(ok_v)
    market_rank = pct_rank(pe_ok, higher_is_better=False)
    peer_rank = market_rank.copy()
    for tpl, idx in df[ok_v].groupby("template").groups.items():
        if tpl is not None and len(idx) >= MIN_GROUP:
            peer_rank.loc[idx] = pct_rank(pe_ok[idx], higher_is_better=False)
    hist_rank = pct_rank(df["own_discount"].where(ok_v))
    div_rank = pct_rank(df["dividend_yield"].where(ok_v))
    parts = pd.DataFrame({"peer_pe": peer_rank, "own_history": hist_rank, "dividend": div_rank})
    weights = pd.Series(W_VALUE)
    present = parts.notna().astype(float) * weights
    value = (parts.fillna(0) * weights).sum(axis=1) / present.sum(axis=1).replace(0, np.nan)
    value[~ok_v] = np.nan
    value[df["value_status"].eq("loss_making")] = 0.0

    # ---- dollar-quoted stocks (MOIL, SAIB, EGSA ...): traded value comes out in USD, but market caps and our
    # thresholds are in EGP. Work out the rate from the data (list-page revenue in EGP / financials revenue in USD).
    rates = []
    for s_ in syms:
        f_ = fins.get(s_)
        if f_ and str(f_.get("currency_price") or "").upper() == "USD" and str(f_.get("currency_financial") or "").upper() == "USD":
            rl, rf = rows[s_].get("revenue_ttm"), f_.get("revenue_ttm")
            if rl and rf and rf > 0:
                rates.append(rl / rf)
    usd_rate = float(np.median(rates)) if len(rates) >= 3 and 20 < float(np.median(rates)) < 120 else USD_EGP_FALLBACK
    fx = pd.Series([usd_rate if (s_ in fins and str(fins[s_].get("currency_price") or "").upper() == "USD") else 1.0
                    for s_ in syms], index=syms)
    df["avg_value"] = df["avg_value"] * fx
    df["turnover"] = df["turnover"] * fx
    df["cur_known"] = [s_ in fins for s_ in syms]       # without a financials page we do not know the quote currency

    # ---- a stock that hardly trades looks "calm" and "clean" only because nothing happens.
    # That is not evidence of anything, so it gets no price-based scores.
    # (The money test only applies when the currency is known; otherwise only the no-trade-days test is used.)
    illiquid = df["price_status"].eq("ok") & ((df["zero_share"] > ILLIQ_ZERO_SHARE) |
                                              ((df["avg_value"] < ILLIQ_VALUE_EGP) & df["cur_known"]))
    df.loc[illiquid, "price_status"] = "illiquid"

    # ---- 3. speculation score
    ok_p = df["price_status"].eq("ok")
    comps = pd.DataFrame({
        "spike": pct_rank(df["spike"].where(ok_p), higher_is_better=False),
        "vol": pct_rank(df["volatility"].where(ok_p), higher_is_better=False),
        "zero": pct_rank(df["zero_share"].where(ok_p), higher_is_better=False),
        "turn": pct_rank(df["turnover"].where(ok_p), higher_is_better=False),
    })
    spec = comps.mean(axis=1)
    spec[comps.notna().sum(axis=1) < 3] = np.nan

    # ---- 4. manipulation flags score (absolute, not ranked)
    manip = (100 - FLAG_PENALTY * df["flags"]).clip(lower=0).where(ok_p)

    df["score_growth"], df["score_value"] = growth, value
    df["score_speculation"], df["score_manipulation"] = spec, manip
    score_cols = ["score_growth", "score_value", "score_speculation", "score_manipulation"]
    df["n_scores"] = df[score_cols].notna().sum(axis=1)
    df["overall"] = df[score_cols].mean(axis=1).where(df["n_scores"] >= 3)

    # ---- notes and confidence
    notes_map = {
        "short_history": "fewer than 3 years of profit history",
        "gap_in_years": "profit history has missing years, leaving too few unbroken years to measure growth",
        "loss_making": "loss-making recently",
        "turnaround": "profit only recently turned positive after losses, so there are too few profitable years to measure growth",
        "no_financials": "no financials page on the source",
        "no_pe": "no usable P/E",
        "foreign_currency": "reports profit in a currency other than EGP or USD, so growth after inflation is not scored",
        "pe_unreliable": "P/E on the source page disagrees with price/EPS by more than 25% (or the currencies differ), so the valuation score is skipped",
    }
    confidences, notes = [], []
    for s, row in df.iterrows():
        n = []
        for st in (row["growth_status"], row["value_status"]):
            if st in notes_map and notes_map[st] not in n:
                n.append(notes_map[st])
        if s in FOREIGN_LISTING and s in fins:
            n.append(f"financials come from the company's {FOREIGN_LISTING[s]} listing page, and the P/E could not be cross-checked against the Egyptian price")
        if row["growth_cur"] == "USD" and row["growth_status"] in ("ok", "loss_making"):
            n.append("reports profit in dollars, so growth is measured in dollars after US inflation")
        if row["growth_status"] == "ok" and row["growth_from"]:
            n.append(f"profit growth is measured from {row['growth_from']} because earlier years were losses or missing")
        if row["price_status"] == "no_prices":
            n.append("no price history available")
        elif row["price_status"] == "illiquid":
            n.append("hardly trades (no trades on over half the days, or under 100k EGP a day), so calm-price and red-flag scores would mean nothing and are skipped")
        elif row["price_status"] == "stale_prices":
            n.append("price history stopped more than a month ago (suspended or not trading), so price-based scores are skipped")
        elif row["price_status"] == "price_mismatch":
            n.append("price history does not match the listed price, so price-based scores are skipped")
        elif row["price_status"] == "short_history":
            n.append("less than about 5 months of price history, so price scores are skipped")
        if row["price_status"] == "ok" and pd.notna(row["days"]) and row["days"] < 200:
            n.append(f"only {int(row['days'])} trading days of prices (under a year), so price scores are less reliable")
        if pd.notna(row["avg_value"]) and row["avg_value"] < THIN_VALUE_EGP and row["cur_known"]:
            n.append("very thin trading (under 250k EGP a day)")
        # The raw data checks (unit mismatches, P/E vs price/EPS gaps) stay in diagnostics.csv: they are for me,
        # not for visitors. Visitors only see one plain line when they lowered the confidence.
        if row["warnings"] and not n and row["n_scores"] == 4:
            n.append("some figures on the source page do not fully agree with each other, so treat the numbers loosely")
        if s in RECENT_LISTINGS:
            n = [f"recently listed ({RECENT_LISTINGS[s]}): too little history for most scores yet, "
                 "which will fill in as prices and annual results accumulate"] + \
                [x for x in n if not x.startswith(("no financials", "no price history", "no usable P/E", "less than about 5 months"))]
        notes.append(n)
        has_warn = bool(row["warnings"])
        if row["n_scores"] == 4 and not has_warn and row["days"] >= 200:
            confidences.append("high")
        elif row["n_scores"] >= 3 and len(row["warnings"].split("; ")) <= 1:
            confidences.append("medium")
        else:
            confidences.append("low")
    df["confidence"] = confidences
    df["notes"] = [" | ".join(n) for n in notes]

    # ---- public file: scores plus a few supporting figures, with credit
    results = {}
    for (s, row), n in zip(df.iterrows(), notes):
        def r0(x):
            return None if pd.isna(x) else int(round(x))
        results[s] = {
            "name": row["name"],
            "link": f"https://stockanalysis.com/quote/egx/{s}/",
            "scores": {
                "profit_growth": r0(row["score_growth"]),
                "undervalued": r0(row["score_value"]),
                "away_from_speculators": r0(row["score_speculation"]),
                "no_manipulation_flags": r0(row["score_manipulation"]),
            },
            "overall": r0(row["overall"]),
            "egx30": s in EGX30,
            "scores_available": int(row["n_scores"]),
            "confidence": row["confidence"],
            "notes": n,
            "figures": {
                "pe": None if pd.isna(row["pe"]) else round(float(row["pe"]), 2),
                "dividend_yield_pct": None if pd.isna(row["dividend_yield"]) else round(float(row["dividend_yield"]), 2),
                "real_profit_growth_per_year_pct": None if pd.isna(row["real_cagr"]) else round(float(row["real_cagr"]) * 100, 1),
                "years_profit_beat_inflation": None if pd.isna(row["years_beating"]) else round(float(row["years_beating"]), 2),
                "avg_daily_traded_value_egp": None if pd.isna(row["avg_value"]) else int(round(float(row["avg_value"]), -3)),
                "annual_volatility_pct": None if pd.isna(row["volatility"]) else round(float(row["volatility"]) * 100, 1),
                "red_flags": None if pd.isna(row["flags"]) else int(row["flags"]),
            },
        }
    return df, results


# ------------------------------------------------------------- file I/O
def load_all():
    rows = {r["symbol"]: r for r in parse_list(LIST_FILE.read_text(encoding="utf-8"))}
    fins, problems = {}, []
    for s in rows:
        f = FIN_DIR / f"{s}.html"
        if not f.exists():
            continue
        try:
            fin = summarize_financials(parse_financials(f.read_text(encoding="utf-8", errors="replace")))
        except Exception as e:  # keep going; report at the end
            problems.append(f"{s}: could not parse financials page ({e})")
            continue
        if fin.get("symbol") and fin["symbol"].upper() != s:
            problems.append(f"{s}: financials file is for {fin['symbol']} - ignored")
            continue
        fins[s] = fin
    prices = {}
    for s in rows:
        f = PRICE_DIR / f"{s}.csv"
        if f.exists():
            try:
                prices[s] = pd.read_csv(f, index_col=0, parse_dates=True)
            except Exception as e:
                problems.append(f"{s}: could not read price file ({e})")
    return rows, fins, prices, problems


def report(df, problems, rows, fins, prices):
    print(f"\nstocks in list: {len(rows)} | financials parsed: {len(fins)} | price files: {len(prices)}")
    print("company types:", dict(Counter(df["template"].dropna())))
    print("confidence:", dict(Counter(df["confidence"])))
    print("scores available per stock:", dict(sorted(Counter(df["n_scores"]).items())))
    print("growth status:", dict(Counter(df["growth_status"])))
    print("value status:", dict(Counter(df["value_status"])))
    print("price status:", dict(Counter(df["price_status"])))
    missing30 = sorted(EGX30 - set(df.index))
    if missing30:
        print("EGX 30 symbols not found in the StockAnalysis list (check the spelling):", missing30)
    e30 = df.loc[[x for x in EGX30 if x in df.index], ["score_growth", "score_value", "score_speculation", "score_manipulation", "overall", "confidence"]]
    print(f"EGX 30 members found: {len(e30)} | with an overall score: {int(e30['overall'].notna().sum())} | "
          f"without: {', '.join(sorted(e30.index[e30['overall'].isna()])) or 'none'}")
    warn = Counter(w for ws in df["warnings"] for w in ws.split("; ") if w)
    if warn:
        print("most common data warnings:")
        for w, c in warn.most_common(8):
            print(f"   {c:4d}  {w}")
    if problems:
        print("problems:")
        for p in problems[:15]:
            print("  ", p)

    show = ["COMI", "SWDY", "ETEL", "TMGH", "EAST", "HRHO", "ABUK", "EGAL", "FWRY"]
    show = [s for s in show if s in df.index]
    cols = ["score_growth", "score_value", "score_speculation", "score_manipulation", "overall", "confidence"]
    t = df.loc[show, cols].copy()
    t.columns = ["growth", "value", "specul.", "manip.", "overall", "conf."]
    print("\nthe stocks you already know:")
    print(t.round(0).to_string())

    good = df[df["confidence"].isin(["high", "medium"])].sort_values("overall", ascending=False)
    print("\ntop 10 by simple average (high/medium confidence only):")
    print(good[["name", "score_growth", "score_value", "score_speculation", "score_manipulation", "overall"]]
          .head(10).round(0).to_string())


def main():
    if not LIST_FILE.exists():
        raise SystemExit("no saved list page - run:  python sa_download.py list")
    rows, fins, prices, problems = load_all()
    df, results = score_universe(rows, fins, prices)

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    DIAG_CSV.parent.mkdir(parents=True, exist_ok=True)
    last_dates = [str(p.index[-1].date()) for p in prices.values() if len(p)]
    payload = {
        "generated": time.strftime("%Y-%m-%d"),
        "prices_as_of": max(last_dates) if last_dates else None,
        "note": BAND_NOTE,
        "sources": "Financial data and some prices: StockAnalysis.com. Other prices: Yahoo Finance. Scores: this project.",
        "stocks": results,
    }
    text = json.dumps(clean(payload), indent=1, ensure_ascii=False, allow_nan=False)
    OUT_JSON.write_text(text, encoding="utf-8")
    OUT_JSON.with_suffix(".js").write_text("window.EGX_SCORES = " + text + ";\n", encoding="utf-8")   # lets the page work when double-clicked
    df.to_csv(DIAG_CSV, index_label="symbol")
    why = {"no_financials": "no financial statements found on StockAnalysis",
           "short_history": "fewer than 3 years of annual profit",
           "gap_in_years": "annual profit has missing years",
           "turnaround": "profitable for fewer than 3 years after losses",
           "foreign_currency": "reports in a currency we have no inflation table for"}
    ng = df[df["growth_status"].isin(why) | df["real_cagr"].isna() & ~df["growth_status"].eq("loss_making")].copy()
    ng["reason"] = ng["growth_status"].map(why).fillna(ng["growth_status"])
    ng[["name", "growth_status", "reason", "growth_years", "growth_cur"]].to_csv(
        DIAG_CSV.with_name("no_growth.csv"), index_label="symbol")
    print("\nstocks with no growth score:", len(ng), dict(Counter(ng["growth_status"])))
    report(df, problems, rows, fins, prices)
    print(f"\nwrote {OUT_JSON}\nwrote {DIAG_CSV}  (keep this one private)")


if __name__ == "__main__":
    main()
