"""
Analytics engine: series loading, KPIs, moving averages, volatility, price
bands, trend classification, provider comparison and LME-parity premium.

All maths runs on the normalised INR/MT column. Source prices are untouched.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Iterable

import numpy as np
import pandas as pd
from sqlalchemy import and_, select

from backend.config import settings
from backend.models import (Commodity, ExchangeRate, HistoricalPrice, Product,
                            Provider)

TRADING_DAYS = 252

PERIODS = {"1D": 1, "7D": 7, "30D": 30, "90D": 90, "180D": 180, "365D": 365}
VOL_BANDS = [(0, 8, "LOW"), (8, 16, "MEDIUM"), (16, 28, "HIGH"), (28, 1e9, "EXTREME")]
BAND_ZONES = [(0, 10, "VERY LOW"), (10, 30, "LOW"), (30, 70, "NORMAL"),
              (70, 90, "HIGH"), (90, 100.01, "VERY HIGH")]


# --------------------------------------------------------------------------- loading
def resolve_ids(db) -> dict[str, dict[str, int]]:
    return {
        "commodity": {c.code: c.id for c in db.scalars(select(Commodity))},
        "provider": {p.code: p.id for p in db.scalars(select(Provider))},
        "product": {p.code: p.id for p in db.scalars(select(Product))},
    }


def load_series(db, commodity: str | None = None, provider: str | None = None,
                product: str | None = None, start: dt.date | None = None,
                end: dt.date | None = None, currency_filter: str | None = None) -> pd.DataFrame:
    """
    Returns a tidy frame: date, price (INR/MT), plus provenance columns.
    Empty frame (correct columns) when nothing matches - callers never see None.
    """
    q = (select(HistoricalPrice.price_date, HistoricalPrice.price_inr_mt,
                HistoricalPrice.price, HistoricalPrice.currency, HistoricalPrice.unit,
                HistoricalPrice.open_price, HistoricalPrice.high_price,
                HistoricalPrice.low_price, HistoricalPrice.close_price,
                HistoricalPrice.source, HistoricalPrice.data_class,
                Provider.code.label("provider"), Product.code.label("product"),
                Commodity.code.label("commodity"))
         .join(Commodity, Commodity.id == HistoricalPrice.commodity_id)
         .join(Provider, Provider.id == HistoricalPrice.provider_id)
         .outerjoin(Product, Product.id == HistoricalPrice.product_id))

    conds = []
    if commodity:
        conds.append(Commodity.code == commodity)
    if provider:
        conds.append(Provider.code == provider)
    if product:
        conds.append(Product.code == product)
    if start:
        conds.append(HistoricalPrice.price_date >= start)
    if end:
        conds.append(HistoricalPrice.price_date <= end)
    if currency_filter:
        conds.append(HistoricalPrice.currency == currency_filter)
    if conds:
        q = q.where(and_(*conds))
    q = q.order_by(HistoricalPrice.price_date)

    rows = db.execute(q).all()
    cols = ["date", "price", "src_price", "currency", "unit", "open", "high", "low",
            "close", "source", "data_class", "provider", "product", "commodity"]
    if not rows:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame(rows, columns=cols)
    df["date"] = pd.to_datetime(df["date"])
    for c in ("price", "src_price", "open", "high", "low", "close"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["price"]).sort_values("date").reset_index(drop=True)


def daily_series(df: pd.DataFrame) -> pd.Series:
    """Collapse a (possibly multi-provider) frame into one daily INR/MT series."""
    if df.empty:
        return pd.Series(dtype=float)
    return df.groupby("date")["price"].mean().sort_index()


def latest_fx(db, base: str = "USD") -> tuple[float, dt.date | None, str]:
    row = db.execute(
        select(ExchangeRate).where(ExchangeRate.base_currency == base,
                                   ExchangeRate.quote_currency == "INR")
        .order_by(ExchangeRate.rate_date.desc()).limit(1)).scalar_one_or_none()
    if row:
        return float(row.rate), row.rate_date, row.source
    fallback = settings.FALLBACK_USDINR if base == "USD" else settings.FALLBACK_EURINR
    return fallback, None, "Configured fallback (ESTIMATED)"


# --------------------------------------------------------------------------- indicators
def moving_averages(s: pd.Series, windows: Iterable[int] = settings.MA_WINDOWS) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for w in windows:
        out[f"ma{w}"] = float(s.rolling(w).mean().iloc[-1]) if len(s) >= w else None
    return out


def pct_change_over(s: pd.Series, days: int) -> float | None:
    """Change vs the observation closest to `days` calendar days back."""
    if s.empty:
        return None
    end_date = s.index[-1]
    target = end_date - pd.Timedelta(days=days)
    past = s[s.index <= target]
    if past.empty:
        return None
    prev = float(past.iloc[-1])
    if prev == 0:
        return None
    return (float(s.iloc[-1]) - prev) / prev * 100.0


def volatility(s: pd.Series) -> dict[str, Any]:
    """Realised volatility on log returns, annualised, plus a banded index."""
    if len(s) < 3:
        return {"daily": None, "v7": None, "v30": None, "v90": None,
                "annualised": None, "band": "UNKNOWN", "index": None}
    r = np.log(s / s.shift(1)).dropna()

    def ann(window: int) -> float | None:
        if len(r) < window:
            return None
        return float(r.tail(window).std(ddof=1) * np.sqrt(TRADING_DAYS) * 100)

    a = ann(min(len(r), TRADING_DAYS))
    band = "UNKNOWN"
    if a is not None:
        band = next(n for lo, hi, n in VOL_BANDS if lo <= a < hi)
    return {
        "daily": float(r.tail(30).std(ddof=1) * 100) if len(r) >= 5 else None,
        "v7": ann(7), "v30": ann(30), "v90": ann(90),
        "annualised": a, "band": band,
        # 0-100 index: 0% vol -> 0, 35% vol -> 100
        "index": None if a is None else round(min(100.0, a / 35.0 * 100), 1),
        "rolling_sd_30": float(s.tail(30).std(ddof=1)) if len(s) >= 30 else None,
    }


def price_band(s: pd.Series, lookback_days: int = 365 * 3) -> dict[str, Any]:
    """Historical percentile of the current price -> procurement price zone."""
    if s.empty:
        return {"percentile": None, "zone": "UNKNOWN"}
    window = s[s.index >= s.index[-1] - pd.Timedelta(days=lookback_days)]
    if len(window) < 10:
        window = s
    cur = float(s.iloc[-1])
    pctl = float((window <= cur).sum()) / len(window) * 100.0
    zone = next(n for lo, hi, n in BAND_ZONES if lo <= pctl < hi)
    return {
        "percentile": round(pctl, 1), "zone": zone,
        "low": float(window.min()), "high": float(window.max()),
        "average": float(window.mean()), "median": float(window.median()),
        "current": cur, "observations": int(len(window)),
    }


def momentum(s: pd.Series) -> dict[str, float | None]:
    """Rate-of-change and a normalised RSI-style oscillator."""
    out = {"roc10": pct_change_over(s, 10), "roc30": pct_change_over(s, 30)}
    if len(s) >= 15:
        d = s.diff().dropna().tail(14)
        gain = d[d > 0].sum()
        loss = -d[d < 0].sum()
        rs = gain / loss if loss > 0 else np.inf
        out["rsi14"] = float(100 - 100 / (1 + rs)) if np.isfinite(rs) else 100.0
    else:
        out["rsi14"] = None
    return out


def classify_trend(s: pd.Series, forecast_pct: float | None = None) -> dict[str, Any]:
    """
    Composite trend score in [-100, +100] built from five weighted components,
    not a single moving-average crossover (spec §7 requires the combination).
      MA structure 30%, momentum 25%, forecast 20%, percentile 15%, slope 10%
    """
    if len(s) < 10:
        return {"trend": "INSUFFICIENT DATA", "score": None, "components": {}}

    cur = float(s.iloc[-1])
    ma = moving_averages(s)
    mom = momentum(s)
    band = price_band(s)
    comp: dict[str, float] = {}

    ma30, ma90, ma200 = ma.get("ma30"), ma.get("ma90"), ma.get("ma200")
    ma_score = 0.0
    if ma30:
        ma_score += 40 if cur > ma30 else -40
    if ma30 and ma90:
        ma_score += 35 if ma30 > ma90 else -35
    if ma90 and ma200:
        ma_score += 25 if ma90 > ma200 else -25
    elif ma90:
        ma_score += 25 if cur > ma90 else -25
    comp["ma_structure"] = max(-100, min(100, ma_score))

    roc = mom.get("roc30")
    comp["momentum"] = 0.0 if roc is None else max(-100, min(100, roc * 12))

    comp["forecast"] = 0.0 if forecast_pct is None else max(-100, min(100, forecast_pct * 12))

    p = band.get("percentile")
    comp["percentile"] = 0.0 if p is None else (p - 50) * 2.0

    if len(s) >= 30:
        y = s.tail(30).to_numpy(dtype=float)
        x = np.arange(len(y), dtype=float)
        slope = np.polyfit(x, y, 1)[0]
        comp["slope"] = float(max(-100, min(100, slope / max(y.mean(), 1e-9) * 100 * 250)))
    else:
        comp["slope"] = 0.0

    weights = {"ma_structure": .30, "momentum": .25, "forecast": .20,
               "percentile": .15, "slope": .10}
    score = sum(comp[k] * w for k, w in weights.items())

    if score >= 45:
        trend = "STRONG BULLISH"
    elif score >= 15:
        trend = "BULLISH"
    elif score > -15:
        trend = "SIDEWAYS"
    elif score > -45:
        trend = "BEARISH"
    else:
        trend = "STRONG BEARISH"

    return {"trend": trend, "score": round(score, 1),
            "components": {k: round(v, 1) for k, v in comp.items()},
            "weights": weights}


# --------------------------------------------------------------------------- aggregates
def resample_frames(s: pd.Series) -> dict[str, list[dict]]:
    """Weekly and monthly aggregates with high/low envelope for charting."""
    if s.empty:
        return {"weekly": [], "monthly": []}
    out = {}
    for label, rule in (("weekly", "W-FRI"), ("monthly", "ME")):
        g = s.resample(rule)
        agg = pd.DataFrame({"avg": g.mean(), "high": g.max(), "low": g.min(),
                            "open": g.first(), "close": g.last()}).dropna()
        out[label] = [{"date": d.strftime("%Y-%m-%d"),
                       "avg": round(r.avg, 2), "high": round(r.high, 2),
                       "low": round(r.low, 2), "open": round(r.open, 2),
                       "close": round(r.close, 2)} for d, r in agg.iterrows()]
    return out


def kpi_block(db, commodity: str, provider: str | None, product: str | None,
              label: str) -> dict[str, Any]:
    """One KPI card's worth of numbers, fully sourced."""
    df = load_series(db, commodity, provider, product)
    s = daily_series(df)
    if s.empty:
        return {"label": label, "available": False,
                "message": "No verified data for this series"}
    last = df.iloc[-1]
    prev = float(s.iloc[-2]) if len(s) > 1 else None
    cur = float(s.iloc[-1])
    return {
        "label": label, "available": True,
        "commodity": commodity, "provider": provider, "product": product,
        "current": round(cur, 2),
        "previous": round(prev, 2) if prev else None,
        "change": round(cur - prev, 2) if prev else None,
        "change_pct": round((cur - prev) / prev * 100, 3) if prev else None,
        "d7": _r(pct_change_over(s, 7)), "d30": _r(pct_change_over(s, 30)),
        "d90": _r(pct_change_over(s, 90)), "d365": _r(pct_change_over(s, 365)),
        "source_price": round(float(last.src_price), 2),
        "source_currency": last.currency, "source_unit": last.unit,
        "source": last.source, "data_class": last.data_class,
        "as_of": s.index[-1].strftime("%Y-%m-%d"),
        "observations": int(len(s)),
    }


def _r(x, nd: int = 2):
    return None if x is None else round(float(x), nd)


# --------------------------------------------------------------------------- comparisons
def provider_comparison(db, commodity: str = "AL",
                        providers: tuple[str, ...] = ("NALCO", "BALCO", "HINDALCO"),
                        products: tuple[str, ...] = ("AL_INGOT", "AL_WIREROD", "AL_BILLET"),
                        ) -> dict[str, Any]:
    """Current price per provider x product, with spread and the cheapest source."""
    table, spread_hist = [], {}
    for prod in products:
        entries = []
        for pv in providers:
            s = daily_series(load_series(db, commodity, pv, prod))
            if s.empty:
                continue
            entries.append({"provider": pv, "price": round(float(s.iloc[-1]), 2),
                            "d30": _r(pct_change_over(s, 30)),
                            "as_of": s.index[-1].strftime("%Y-%m-%d")})
        if not entries:
            continue
        prices = [e["price"] for e in entries]
        lo, hi, avg = min(prices), max(prices), sum(prices) / len(prices)
        lowest = min(entries, key=lambda e: e["price"])["provider"]
        highest = max(entries, key=lambda e: e["price"])["provider"]
        for e in entries:
            e["vs_avg_pct"] = round((e["price"] - avg) / avg * 100, 2)
            e["is_lowest"] = e["provider"] == lowest
        table.append({
            "product": prod, "entries": entries,
            "lowest_provider": lowest, "highest_provider": highest,
            "lowest": lo, "highest": hi, "average": round(avg, 2),
            "spread": round(hi - lo, 2), "spread_pct": round((hi - lo) / lo * 100, 3),
        })

        # historical spread trend (max-min across providers, per day)
        frames = []
        for pv in providers:
            si = daily_series(load_series(db, commodity, pv, prod))
            if not si.empty:
                frames.append(si.rename(pv))
        if len(frames) > 1:
            wide = pd.concat(frames, axis=1).dropna()
            sp = (wide.max(axis=1) - wide.min(axis=1)).tail(260)
            spread_hist[prod] = [{"date": d.strftime("%Y-%m-%d"), "spread": round(float(v), 2)}
                                 for d, v in sp.items()]
    return {"table": table, "spread_history": spread_hist}


def copper_parity(db) -> dict[str, Any]:
    """
    LME -> Indian equivalent, and the premium the market/supplier charges.
        Indian Equivalent = LME USD/MT x USDINR + applicable premium
        Premium           = Indian price - LME equivalent
    """
    lme = daily_series(load_series(db, "CU", "LME", "CU_CATHODE"))
    ind = daily_series(load_series(db, "CU", "BME", "CU_CATHODE"))
    fx, fx_date, fx_src = latest_fx(db, "USD")

    lme_raw = load_series(db, "CU", "LME", "CU_CATHODE")
    lme_usd = (lme_raw.groupby("date")["src_price"].mean().sort_index()
               if not lme_raw.empty else pd.Series(dtype=float))

    if lme.empty or ind.empty:
        return {"available": False, "message": "Copper parity needs both LME and Indian series"}

    joined = pd.concat([lme.rename("lme_inr"), ind.rename("india")], axis=1).dropna()
    joined["premium"] = joined["india"] - joined["lme_inr"]
    joined["premium_pct"] = joined["premium"] / joined["lme_inr"] * 100

    tail = joined.tail(400)
    return {
        "available": True,
        "usdinr": round(fx, 4),
        "usdinr_date": fx_date.isoformat() if fx_date else None,
        "usdinr_source": fx_src,
        "lme_usd": round(float(lme_usd.iloc[-1]), 2) if not lme_usd.empty else None,
        "lme_inr_equivalent": round(float(joined["lme_inr"].iloc[-1]), 2),
        "india_price": round(float(joined["india"].iloc[-1]), 2),
        "premium": round(float(joined["premium"].iloc[-1]), 2),
        "premium_pct": round(float(joined["premium_pct"].iloc[-1]), 3),
        "premium_avg_90": round(float(joined["premium"].tail(90).mean()), 2),
        "premium_min_1y": round(float(joined["premium"].tail(260).min()), 2),
        "premium_max_1y": round(float(joined["premium"].tail(260).max()), 2),
        "basis_status": ("WIDE" if joined["premium"].iloc[-1] >
                         joined["premium"].tail(260).quantile(0.8)
                         else "NARROW" if joined["premium"].iloc[-1] <
                         joined["premium"].tail(260).quantile(0.2) else "NORMAL"),
        "series": [{"date": d.strftime("%Y-%m-%d"),
                    "lme_inr": round(float(r.lme_inr), 2),
                    "india": round(float(r.india), 2),
                    "premium": round(float(r.premium), 2)}
                   for d, r in tail.iterrows()],
    }


def aluminium_stats(db, providers=("NALCO", "BALCO", "HINDALCO"),
                    products=("AL_INGOT", "AL_WIREROD", "AL_BILLET")) -> list[dict]:
    """Per provider x product: averages, extremes and the current percentile."""
    out = []
    for pv in providers:
        for pr in products:
            s = daily_series(load_series(db, "AL", pv, pr))
            if s.empty:
                continue
            band = price_band(s)
            out.append({
                "provider": pv, "product": pr,
                "current": round(float(s.iloc[-1]), 2),
                "avg_30": round(float(s.tail(30).mean()), 2),
                "avg_90": round(float(s.tail(90).mean()), 2),
                "avg_365": round(float(s.tail(260).mean()), 2),
                "min_all": round(float(s.min()), 2),
                "max_all": round(float(s.max()), 2),
                "percentile": band["percentile"], "zone": band["zone"],
                "d30": _r(pct_change_over(s, 30)), "d365": _r(pct_change_over(s, 365)),
            })
    return out


def series_payload(db, commodity: str, provider: str | None, product: str | None,
                   start: dt.date | None = None, end: dt.date | None = None,
                   with_ma: bool = True) -> dict[str, Any]:
    """Everything a chart needs for one series."""
    df = load_series(db, commodity, provider, product, start, end)
    s = daily_series(df)
    if s.empty:
        return {"available": False, "points": [], "message": "No data for this selection"}

    frame = pd.DataFrame({"price": s})
    if with_ma:
        for w in settings.MA_WINDOWS:
            if len(s) >= w:
                frame[f"ma{w}"] = s.rolling(w).mean()

    points = []
    for d, row in frame.iterrows():
        pt = {"date": d.strftime("%Y-%m-%d"), "price": round(float(row["price"]), 2)}
        for w in settings.MA_WINDOWS:
            col = f"ma{w}"
            if col in frame.columns and pd.notna(row.get(col)):
                pt[col] = round(float(row[col]), 2)
        points.append(pt)

    agg = resample_frames(s)
    return {
        "available": True, "commodity": commodity, "provider": provider,
        "product": product, "points": points,
        "weekly": agg["weekly"], "monthly": agg["monthly"],
        "moving_averages": moving_averages(s),
        "volatility": volatility(s), "band": price_band(s),
        "momentum": momentum(s),
        "changes": {k: _r(pct_change_over(s, v)) for k, v in PERIODS.items()},
        "data_classes": sorted(df["data_class"].unique().tolist()),
        "sources": sorted(df["source"].unique().tolist())[:5],
        "as_of": s.index[-1].strftime("%Y-%m-%d"),
    }
