# TransformerProcure Intelligence v2.0

Commodity price tracking, forecasting and **transformer BOM cost-exposure** analysis for
power and solar transformer procurement.

It answers the questions a procurement desk actually asks:

> *If copper moves ₹10,000/MT, what does one 10 MVA transformer cost me — and what is my
> exposure across every open project? How much is already covered by stock and open POs?
> Should I buy now, buy partially, wait, negotiate, or lock the price?*

---

## Contents

- [What it does](#what-it-does)
- [Quick start](#quick-start)
- [Docker](#docker)
- [How data gets in](#how-data-gets-in)
- [The data-honesty rules](#the-data-honesty-rules)
- [The forecasting engine](#the-forecasting-engine)
- [The procurement decision engine](#the-procurement-decision-engine)
- [BOM cost-impact model](#bom-cost-impact-model)
- [Project layout](#project-layout)
- [API reference](#api-reference)
- [Configuration](#configuration)
- [Scheduled jobs](#scheduled-jobs)
- [Tests](#tests)
- [Extending it](#extending-it)
- [Known limits](#known-limits)

---

## What it does

| Area | Detail |
|---|---|
| **Commodities** | Copper (LME cash, Indian reference, cathode, wire rod), Aluminium (NALCO / BALCO-Vedanta / Hindalco × ingot, wire rod, billet), plus CRGO, MS plate and transformer oil for full BOM coverage |
| **History** | Daily prices with 7/30/90/200-day moving averages, weekly and monthly aggregates, high/low envelopes, 3-year percentile bands |
| **Forecasting** | 12 competing models, walk-forward back-tested at every horizon; the winner is chosen on out-of-sample MAPE and must beat a random-walk baseline |
| **Trend** | Composite classification from MA structure, momentum, forecast, percentile and slope — not a single crossover rule |
| **Volatility** | 7/30/90-day and annualised realised volatility, banded LOW → EXTREME, with a 0–100 index |
| **Decision support** | BUY NOW / BUY PARTIAL / LOCK PRICE / PHASED / NEGOTIATE / MONITOR / WAIT / DO NOT LOCK, each with a written rationale and factor breakdown |
| **BOM exposure** | Transformer master + commodity-linked BOM, cost waterfall, −20%…+20% sensitivity, what-if simulator, coverage, price-lock calculator, priority matrix, monthly exposure, historical cost back-test |
| **Suppliers** | Quotation entry with landed-cost build-up, same-product benchmarking, best-price flagging and expiry tracking |
| **Data quality** | Missing dates, duplicates, outliers, price jumps, bad units/currencies, negative prices, stale feeds |
| **Delivery** | Web dashboard, offline single-file snapshot, KPI scorecard page, Excel/CSV/PDF export |

---

## Quick start

**The easiest way:** double-click `start-mac-linux.command` (macOS/Linux) or
`start-windows.bat` (Windows). The first run installs everything and takes 2–3 minutes;
after that it starts in seconds and opens the browser for you.

**No installation at all:** open `frontend/standalone.html` in any browser. It is a
self-contained snapshot — every chart and calculation works offline — but it cannot fetch
new prices.

**By hand**, if you prefer. Requires **Python 3.10+**. No database server needed — it
defaults to a local SQLite file.

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
python backend/main.py
```

Then open **<http://127.0.0.1:8000/>**.

On first run the app creates the schema, generates a clearly-labelled demo dataset
(~12,500 price rows over three years, five transformer projects with full BOMs), and starts
training the forecast models in the background. The dashboard is usable immediately; the
forecast panels fill in after a few minutes.

### Useful flags

```bash
python backend/main.py --seed              # rebuild the demo dataset
python backend/main.py --force-seed        # wipe prices and regenerate
python backend/main.py --forecast          # train models now, in the foreground
python backend/main.py --no-serve          # bootstrap only, then exit
python backend/main.py --port 9000 --reload
```

### Other entry points

| URL | What it is |
|---|---|
| `/` | Full dashboard (10 sections) |
| `/procurement_kpi.html` | Supply-chain KPI scorecard + historical BOM cost back-test |
| `/standalone.html` | Offline snapshot — see below |
| `/docs` | Interactive OpenAPI documentation |

### The offline snapshot

```bash
python -m backend.services.build_standalone
```

Writes `frontend/standalone.html` — a single ~1.7 MB file with the data, the charting
library and all the logic inlined. Open it by double-clicking; it needs no server, no
Python and no network, and it makes **zero** network requests. Useful for emailing a
position to management or working from a site with no connectivity.

It is a *snapshot*: it cannot fetch live prices, and it says so at the top of every page.

---

## Docker

```bash
docker compose up -d --build
open http://localhost:8000
```

Brings up PostgreSQL 16 (schema applied automatically) and the app. Drop official producer
circulars into `./data/imports/` on the host and they are ingested on the next refresh.

To run without PostgreSQL, delete the `db` service and unset `DATABASE_URL` — the app falls
back to SQLite.

---

## How data gets in

```
SOURCE  →  validate  →  clean  →  unit convert  →  currency convert
        →  database  →  analytics  →  forecast  →  dashboard
```

There are four ingestion paths, and the right one depends on what the source permits.

### 1. Licensed API (copper / FX)

LME data is licensed. Point the app at the endpoint your organisation is entitled to use:

```bash
LME_API_URL=https://your-entitled-endpoint/lme
LME_API_KEY=...
```

With nothing configured the adapter reports `UNAVAILABLE` and the dashboard shows
*"Live data unavailable — last verified price shown"*. It never invents a number.

### 2. Official producer circulars (aluminium) — the default path

NALCO, BALCO/Vedanta and Hindalco publish price circulars as PDF/Excel on their own portals
and do not offer an open machine API. Scraping them can breach their terms, so the
supported route is an approved import:

1. Save the official circular into `data/imports/`
2. Put the producer's name in the filename — `nalco_2026-09.xlsx`, `hindalco_sept.xlsx`
3. Press **Refresh data** (or wait for the daily job)

The same file also works through **Historical data → Upload workbook**. A producer's own
circular has no "Commodity" or "Provider" column — NALCO does not restate on every row that
it is selling aluminium — so the importer infers both from the filename and reports what it
inferred. Free-text product names (`Ingot`, `Wire Rod`, `Billet`, `P1020A Ingot`) are mapped
to product codes automatically.

The adapter matches on the filename, parses flexible column names (`date`/`w.e.f.`/`effective`,
`price`/`rate`/`basic price`, …) and stores each row as `VERIFIED_HISTORICAL` with the
filename recorded as its source. A worked example ships in
`data/imports/nalco_circular_sample.xlsx`.

If you *do* have an entitled endpoint, set `NALCO_CIRCULAR_URL` etc. and it will be used too.

### 3. Manual workbook upload

**Historical data → Upload workbook**, or `POST /api/data/import`.

Mandatory columns: `date`, `commodity`, `provider`, `price`.
Optional: `product`, `grade`, `currency`, `unit`, `basis`, `premium`, `freight`, `location`,
`source`, `remarks`. Column names are matched by alias, so `Rate`, `Basic Price` and `Amount`
all map to `price`.

Tick **validate only** for a dry run. Every rejected row comes back with the reason —
nothing is silently dropped. Download a pre-formatted template from the same panel.

### 4. Supplier quotations

Entered on the **Supplier quotes** page or via `POST /api/quotes`. Prices in USD or per-KG
are normalised automatically; the original figures are preserved.

---

## The data-honesty rules

These are enforced in code, not just documented, because a procurement decision made on a
fabricated price is worse than no dashboard at all.

**1. Source data is never overwritten.** `price`, `currency` and `unit` are stored exactly
as published. The analytics value lives in a separate `price_inr_mt` column and can be
recomputed at any time without touching the original.

**2. Every price declares what kind of number it is.**

| Class | Meaning |
|---|---|
| `LIVE` | Fetched from a configured feed within the staleness window |
| `VERIFIED_HISTORICAL` | From an official circular or an approved import |
| `SUPPLIER_QUOTE` | A supplier's offer, not a market price |
| `ESTIMATED` | Derived using a configured fallback (e.g. the FX constant) |
| `FORECAST` | A model projection |
| `DEMO_DATA` | Simulated. Not market data. |

The UI tags these everywhere and never blends them into one number.

**3. Better data always wins.** Each class carries a trust rank
(`LIVE` > `VERIFIED_HISTORICAL` > `SUPPLIER_QUOTE` > `ESTIMATED` > `DEMO_DATA`). When an
import or a feed brings a *more* trustworthy observation for a series and date that is
already held, it supersedes the stored row and the response reports `rows_superseded`.
Without this the first real circular an operator imports is discarded as a "duplicate" of
the simulated row, and the dashboard keeps showing the demo price with no sign anything
went wrong. The rule never works in reverse: demo data cannot overwrite verified data.

**4. Freshness is stated per commodity, not globally.** One producer circular landing for
aluminium says nothing about copper. `GET /api/market/live` returns a per-commodity status
(`CURRENT` / `STALE` / `DEMO` / `NO_DATA`) and the banner names exactly which commodities
are not current — *"Live data unavailable for CU, CRGO, STEEL, OIL"* — rather than letting
a single success clear the warning for the whole screen.

**5. A missing feed is reported, not filled in.** An unconfigured or unreachable adapter
returns `UNAVAILABLE` with the reason, the dashboard shows a red banner, and a `STALE_LIVE`
alert fires. The last verified price stays on screen with its timestamp.

**6. Demo data announces itself.** When the demo generator is in play, a persistent banner
sits above every page, the management summary ends with an explicit warning, and the PDF
export carries the same notice.

**7. Recommendations are labelled as procurement decision support**, never as financial
advice — in the API payload, the UI and the PDF.

**8. Secrets stay on the server.** API keys are read from the environment inside the
backend. `GET /api/config` deliberately returns only *whether* a source is configured. A
test asserts that no key-like string ever appears in that response.

---

## The forecasting engine

Twelve models compete: random walk with drift, three moving averages, two exponential
moving averages, three linear regressions over different lookbacks, ARIMA (order chosen by
AIC), Holt-Winters damped trend, and a random forest on lag features. Prophet joins the
field automatically if it is installed.

Selection is evidence-based:

1. **Rolling-origin back-test** at the *actual* forecast horizon, over several folds.
   Training never sees the validation window, so MAE/RMSE/MAPE are out-of-sample scores.
2. **Lowest mean MAPE wins**, subject to two guards:
   - it must beat random-walk-with-drift — an unbeaten naive model *is* the honest answer;
   - ties within 2% relative MAPE go to the simpler model.
3. **Confidence bands** come from the winner's out-of-sample residual scale, widened with
   √h — not from the in-sample fit, which is always too optimistic.

Two guards keep long horizons sane:

- **Damped trend extrapolation.** A linear fit over 60 days extended across a quarter
  assumes a local slope holds for months, which no metal price does. Each successive step's
  trend contribution is shrunk by φ = 0.98, so the projection flattens with distance instead
  of running away. Without this the 90-day copper forecast came out below the 180-day one —
  an incoherent term structure that would mislead anyone reading the table.
- **A ±3σ sanity clamp**, where σ is the series' own realised volatility scaled to the
  horizon. Wide enough never to touch a plausible projection, tight enough to stop a
  modelling artefact reaching a management report. Any clamp is logged, never silent.

Different horizons legitimately select different models. On the shipped demo data the naive
baseline wins at 7 days, Holt-Winters at 30, and the random forest at 90 — and the
**Forecast → Model selection evidence** panel shows the full leaderboard, so you can see
exactly why.

Retraining is weekly (or **Retrain models** on demand). Results are persisted, so dashboard
requests are served from storage rather than re-running a multi-minute back-test.

---

## The procurement decision engine

Six weighted factors, each scored in [−100, +100], where positive means *buying sooner is
favourable*:

| Factor | Weight | What it measures |
|---|---|---|
| Forecast direction | 30% | Expected 30-day move |
| Price band | 20% | Historical percentile — cheap is a buy |
| Trend | 15% | Composite trend score |
| Coverage gap | 15% | How exposed the uncovered quantity is |
| Urgency | 10% | Delivery date against supplier lead time |
| Budget variance | 10% | Market against the approved budget rate |

**Volatility deliberately does not move the score.** It changes the *shape* of the action:
high volatility converts a buy into phased procurement and caps the recommended lot size;
extreme volatility turns LOCK PRICE into DO NOT LOCK. That is how a procurement desk
actually behaves — high volatility is a reason to stage a purchase, not a reason to want the
material more or less.

A delivery inside the supplier lead time with coverage below target overrides everything and
forces BUY NOW; the rationale says so explicitly.

Every recommendation returns its factor scores, its weights and a written rationale
assembled from the actual figures.

---

## BOM cost-impact model

The core arithmetic, straight through:

```
Material cost / transformer   = commodity price × BOM consumption
Total project exposure        = price × consumption × transformer quantity
Material cost impact          = price change × consumption
Future procurement exposure   = forecast price × UNPROTECTED quantity
Uncovered quantity            = requirement − stock − open PO − confirmed supply
```

Consumption is scaled from a 10 MVA reference design (2.850 MT copper, 1.250 MT aluminium,
3.200 MT CRGO, 4.500 MT steel, 3.000 MT oil) using the usual sub-linear exponents — active
material grows roughly with rating^0.75–0.85, tank and oil with rating^0.70–0.72. Edit the
BOM per job through the API or the database; nothing here is hard-coded into the UI.

Worked example, reproduced by the test suite:

```
Copper price          ₹850,000/MT
Copper BOM            2.850 MT / transformer
Material cost                        ₹2,422,500 / transformer
Price rises           ₹10,000/MT
Impact                               ₹28,500 / transformer
Project quantity      20 transformers
Total exposure                       ₹570,000
```

One canonical `HEADLINE` map decides *the* price of each commodity (copper = the Indian
reference, not the LME print, because the BOM is bought domestically). Every module resolves
prices and forecasts through it, so a roll-up can never accidentally mix an exchange series
into a domestic cost calculation.

---

## Project layout

```
commodity-dashboard/
├── backend/
│   ├── main.py                 # FastAPI app, CLI, static hosting
│   ├── config.py               # environment-driven settings
│   ├── database.py             # engine, session, dialect-safe time helpers
│   ├── models.py               # 18 SQLAlchemy models
│   ├── scheduler.py            # APScheduler daily/weekly/monthly jobs
│   ├── api/                    # routes: market, forecast, bom, procurement,
│   │                           #         quotes, data, meta  (+ pydantic schemas)
│   ├── services/
│   │   ├── analytics.py        # series, KPIs, MAs, volatility, bands, trend
│   │   ├── forecast_service.py # persistence + caching of forecast runs
│   │   ├── bom_engine.py       # roll-up, impact, waterfall, sensitivity, coverage
│   │   ├── procurement.py      # decision engine, price lock, priority matrix
│   │   ├── alerts.py           # rule evaluation
│   │   ├── quotes.py           # landed cost + benchmarking
│   │   ├── excel_io.py         # validated import / multi-sheet export
│   │   ├── etl.py              # ingestion orchestration + job definitions
│   │   ├── summary.py          # dynamic management narrative
│   │   ├── reports.py          # PDF + monthly archive
│   │   ├── seed_data.py        # masters + DEMO generator
│   │   └── build_standalone.py # offline single-file build
│   ├── forecasting/
│   │   ├── models.py           # the model zoo
│   │   └── engine.py           # back-test, selection, intervals
│   ├── data_sources/           # LME, FX and producer-circular adapters
│   └── utils/                  # units, validation, cache, logging
├── frontend/
│   ├── index.html              # dashboard
│   ├── procurement_kpi.html    # KPI scorecard
│   ├── standalone.html         # generated offline build
│   ├── css/style.css
│   ├── js/                     # util, api, charts, app, standalone_app
│   └── assets/echarts.min.js   # vendored so it works air-gapped
├── database/
│   ├── schema.sql              # PostgreSQL DDL, 18 tables
│   └── seed.sql                # masters, alert rules, policy settings
├── data/
│   ├── historical/             # sample datasets
│   └── imports/                # drop producer circulars here
├── reports/                    # generated PDFs and workbooks
├── tests/                      # 85 tests
├── START-HERE.txt              # plain-English guide, no jargon
├── start-mac-linux.command    # one-click launcher
├── start-windows.bat          # one-click launcher
├── requirements.txt
├── requirements-dev.txt
├── .env.example
├── Dockerfile
└── docker-compose.yml
```

---

## API reference

Full interactive docs at `/docs`. The main endpoints:

**Market**
`GET /api/market/masters` · `kpis` · `series` · `providers/compare` · `copper/parity` ·
`aluminium/stats` · `live` · `quality` · `fx`

**Forecast**
`GET /api/forecast` · `forecast/trend` · `forecast/volatility` · `forecast/leaderboard` ·
`forecast/capabilities` · `POST /api/forecast/refresh`

**BOM & exposure**
`GET /api/bom/rollup` · `transformers` · `transformers/{job}` · `impact` · `waterfall` ·
`sensitivity` · `coverage` · `exposure/monthly` · `exposure/matrix` · `flow` · `backtest`
`POST /api/bom/what-if` · `transformers` · `transformers/{job}/items`

**Procurement**
`GET /api/procurement/recommendation` · `recommendations` · `summary` · `executive` ·
`kpis` · `alerts` · `price-lock`
`POST /api/procurement/price-lock` · `alerts/evaluate` · `alerts/{id}/ack`

**Quotes** `GET|POST /api/quotes` · `POST /api/quotes/recalculate` · `DELETE /api/quotes/{id}`

**Data** `GET /api/data/template` · `export/excel` · `export/csv` · `export/pdf` ·
`source-log` · `settings` · `POST /api/data/import` · `refresh` · `PUT /api/data/settings`

**Meta** `GET /api/health` · `config` · `stats` · `scheduler`

---

## Configuration

Copy `.env.example` to `.env`. Everything has a working default; nothing below is required
to run the app.

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | SQLite file | `postgresql+psycopg://user:pw@host:5432/db` for production |
| `DATA_MODE` | `DEMO` | `LIVE` uses configured adapters |
| `LME_API_URL` / `LME_API_KEY` | — | Licensed copper feed |
| `METALS_API_URL` / `METALS_API_KEY` | — | Licensed redistributor |
| `FX_API_URL` / `FX_API_KEY` | exchangerate.host | USD/INR, EUR/INR |
| `FALLBACK_USDINR` | 88.50 | Used only if FX is unreachable; result labelled `ESTIMATED` |
| `NALCO_/BALCO_/HINDALCO_CIRCULAR_URL` | — | Optional entitled endpoints |
| `STALE_AFTER_HOURS` | 26 | When live data is declared stale |
| `ENABLE_SCHEDULER` | true | Background jobs |
| `BOOTSTRAP_FORECASTS` | true | Train once at startup if nothing is stored |
| `BACKTEST_FOLDS` | 3 | Validation folds per horizon |
| `ALERT_DAY_MOVE_PCT` | 2.0 | Daily move alert threshold |

Procurement policy values — budget rates, target coverage, risk appetite, GST — live in the
`user_settings` table and are editable from **Settings** without a restart.

---

## Scheduled jobs

Times are IST (`Asia/Kolkata`).

| Job | Default | Does |
|---|---|---|
| Daily | `30 6 * * *` | Fetch prices and FX, validate, refresh the live snapshot, recalculate quotes, evaluate alerts |
| Weekly | `0 3 * * 1` | Retrain and re-select every forecast model, re-evaluate alerts |
| Monthly | `0 4 1 * *` | Write the management PDF and archive the data workbook to `reports/` |

Every job is wrapped so a failure is logged and the scheduler keeps running — a broken
external source can never stop the pipeline. **Refresh data** in the top bar runs the daily
job on demand; the last run and next run times are on the Settings page.

Alert evaluation de-duplicates over a rolling 20-hour window, so a scheduled run and a
manual run minutes apart will not double up.

---

## Tests

```bash
pip install -r requirements-dev.txt   # pytest, httpx, ruff, pypdf
pytest -q                             # 85 tests
pytest tests/test_core.py -q          # units, validation, models, BOM maths
pytest tests/test_api.py -q           # end-to-end API against a temporary database
ruff check backend/ tests/            # lint
```

Test dependencies live in `requirements-dev.txt`, separate from `requirements.txt`, so a
production image never ships a test runner.

The suite concentrates on the things that would quietly corrupt a decision: unit and
currency normalisation, validation rejections, the BOM arithmetic, forecast-selection
honesty, internal consistency of every roll-up (per-project costs must sum to the portfolio
total; the waterfall must close; coverage must satisfy its identity), and the guarantee that
a missing feed is reported rather than faked.

Eight real bugs were caught during development and are now regression-covered or fixed:

- a unit parser that mangled `INR/KG` into an unparseable token, breaking any import that
  wrote the unit with a currency prefix;
- a timezone mismatch — alerts stored in UTC, compared against a local calendar day, on a
  SQLite column that drops `tzinfo` — which made the daily job duplicate every alert around
  midnight IST;
- supplier quotes benchmarked against the commodity headline instead of their own product,
  so every aluminium wire-rod quote read ~6% expensive purely because of the conversion
  charge;
- the price-lock calculator valuing a 30-day lock against the 90-day forecast;
- **real imported data being discarded as a duplicate of the demo row** it was meant to
  replace, so connecting a live source appeared to do nothing;
- a producer's own circular being rejected by the manual upload because it lacks the
  `Commodity`/`Provider` columns no producer would ever print;
- one source succeeding clearing the "not current" warning for every commodity, including
  those with no source at all;
- cached front-end assets letting an open tab run stale JavaScript after an upgrade.

---

## Extending it

**A new commodity** — insert into `commodity_master` and `product_master`, add an entry to
`BOM_MODEL` in `seed_data.py` (or add BOM lines through the API), and add it to `HEADLINE`
in `bom_engine.py`. Analytics, forecasting, alerts and exposure pick it up automatically.

**A new price source** — subclass `BaseAdapter` in `backend/data_sources/`, implement
`_fetch()` returning `PriceRecord`s, declare any required settings in `requires`, and
register it in `etl.refresh_prices()`. Retry, timeout, logging and the "never fabricate"
contract are handled by the base class.

**A new forecast model** — implement `fit(y)` / `predict(h)`, set `min_points`, and add it
to `build_model_zoo()`. It joins the back-test automatically and will only be selected if it
earns its place.

**A new alert** — insert a row into `alert_rules`, then handle its `rule_type` in
`alerts.evaluate_all()`. Thresholds for existing rule types need no code change at all.

---

## Known limits

- **The shipped dataset is simulated.** It is a seeded stochastic model anchored to
  plausible September 2026 levels, labelled `DEMO_DATA` everywhere. Connect a licensed feed
  or import official circulars before using any figure commercially.
- **No authentication.** Deploy behind your existing SSO/reverse proxy; the app assumes a
  trusted network.
- **Front-end assets are version-stamped** (`app.js?v=<version>-<mtime>`) and served
  `no-cache`. Without both, a browser tab that is already open keeps running the previous
  script against the new API after an upgrade — a fault that presents as "the feature just
  doesn't work" and is near-impossible to diagnose from a user's description.
- **Forecast horizons beyond 90 days carry wide bands** — as they should. Treat the 180 and
  365-day figures as scenario anchors, not point estimates.
- **Each horizon selects its own model**, so the term structure can still be non-monotonic
  when models genuinely disagree — a 90-day figure below the 180-day one means the 90-day
  winner reads the recent trend differently from the 180-day winner. The damping and clamp
  keep this inside the confidence bands, and the leaderboard shows exactly which model won
  each horizon and on what error. Read the band, not just the point.
- **The standalone build is a snapshot**, frozen at build time, and cannot fetch live data.
- **Prophet is optional.** Holt-Winters damped trend covers the same ground without the
  heavyweight dependency; uncomment it in `requirements.txt` to add it to the back-test.

---

*Recommendations are procurement decision-support signals derived from price, forecast,
volatility, BOM exposure and coverage data. They are not financial or investment advice.
Forecasts are statistical projections with back-tested error bands, not guarantees of future
market levels.*

---

## Running it as a background service (macOS)

The scheduler lives inside the app, so the daily/weekly/monthly jobs only run while it
is running. To keep it up across reboots without leaving a terminal window open, a
launch agent is installed at
`~/Library/LaunchAgents/com.transformerprocure.dashboard.plist`:

```bash
launchctl load   ~/Library/LaunchAgents/com.transformerprocure.dashboard.plist   # start
launchctl unload ~/Library/LaunchAgents/com.transformerprocure.dashboard.plist   # stop
launchctl list | grep transformerprocure                                          # status
```

It binds to `127.0.0.1` only — reachable from this machine, not from the network.
Logs go to `reports/service.log`.
