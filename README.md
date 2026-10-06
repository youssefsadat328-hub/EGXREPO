# EGX Stock Scorer

Search any stock on the Egyptian Exchange and see how it scores from 0 to 100 on four measurements:

1. **Profit growth** - net income growth after inflation, and in how many years profit beat inflation.
2. **Undervalued** - P/E compared with similar companies and with the stock's own history, plus dividend yield.
3. **Away from speculators** - how spiky, volatile, patchy and fast-turning its trading is (a proxy: free float is not available).
4. **No manipulation signs** - starts at 100 and loses 10 points for each red flag in the last year: a day at the price limit, a big jump on low volume, or a big move reversed the next day. Red flags are not proof of anything.

**Live page:** https://youssefsadat328-hub.github.io/EGXREPO/

> **This is a screening aid and a learning project. It is not investment advice, and it is not an all-round financial tool.** It only knows what [StockAnalysis.com](https://stockanalysis.com/list/egyptian-stock-exchange/) (plus Yahoo Finance for some prices) publishes, so it inherits that data's gaps and mistakes.

## What this project is for

I built this as a data analysis / data engineering exercise, end to end: collecting data politely, parsing messy HTML, cleaning it, defining measures, validating them, and publishing a static site. The skills it exercises:

- **Data collection:** cache-first downloads that identify themselves, check robots.txt and stop at the first sign the site objects; a browser-driven crawler for paginated tables, limited to what the free site offers.
- **Parsing and cleaning:** HTML tables to structured records; unit and currency problems (dollar reporters, dollar-quoted stocks, a company whose statements live on a foreign listing page); splits and bonus shares removed from price series.
- **Feature design:** percentile-rank scoring against company-type peers, inflation-adjusted growth, trading-pattern proxies for speculation, rule-based chart red flags.
- **Validation and honesty about data:** cross-checks between sources (P/E against price/EPS, list price against price history), illiquid and stale stocks excluded, a confidence label on every stock, and "not enough data" instead of a guess.
- **Delivery:** a scheduled-style refresh script, a static page on GitHub Pages, and a repo that publishes only computed scores, not the source data.

## How the scores work

- Each measure is a rank against the other EGX stocks: 50 is typical, 90 means better than about 90% of them. The manipulation score is absolute, not ranked.
- **Growth:** 70% real (after-inflation) profit growth per year, 30% share of years where profit beat inflation. Needs at least 3 unbroken profitable years; the window starts at the first profitable year, and the page says when it is shorter than 5 years. Loss-making stocks score 0. Companies reporting in EGP are deflated by Egyptian inflation (yearly averages: 2022 13.9%, 2023 33.9%, 2024 28.3%, 2025 14.1%); companies reporting in US dollars are judged in dollars and deflated by US CPI (8.0%, 4.1%, 2.9%, 2.6%).
- **Value:** 50% P/E rank inside the stock's company type (banks, real estate, and so on, when at least 8 peers exist), 30% P/E against its own past years, 20% dividend yield.
- Prices use the last ~250 trading days. Market-wide days with no trading are removed, and daily moves above 11% are treated as splits or bonus shares and ignored. Stocks that hardly trade get no price-based scores, because a stock where nothing happens looks calm for the wrong reason.
- **Confidence:** high needs all four scores, no data warnings and about a year of prices. Stocks with less data, or with a data warning, are marked medium or low and the page says why.
- The overall number is a simple average of the available scores and needs at least three of the four.
- **EGX 30 members** carry a note: large, heavily traded shares are hard to move with a few trades, but short-term traders also gather there, so a lower "away from speculators" score reflects how busy trading is, not company quality.

## Known limits

- Everything depends on StockAnalysis (and Yahoo for some prices). If they are wrong, late or missing a company, so is this tool.
- Free float and ownership are not in the data, so "away from speculators" is estimated from trading behaviour.
- Some stocks have no financials page, are newly listed, or have a short price history, and show "not enough data" instead of a guess.
- Net income is the figure StockAnalysis reports ("net income to common"), which can differ from what a company calls its profit.
- Scores are a snapshot. The page shows the date the scores were built.

## Data and credit

Financial data comes from [StockAnalysis.com](https://stockanalysis.com/list/egyptian-stock-exchange/). Prices come from Yahoo Finance, and StockAnalysis fills the stocks Yahoo does not carry. This repository publishes only the computed scores and a few supporting figures, with a link back to the source for each stock. The downloaded pages and raw price files stay on my computer and are not in this repo. Downloads are slow, cached, identify themselves honestly, check robots.txt first, and stop at the first sign the site objects.

