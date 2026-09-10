"""Forecasting, trend and volatility endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from backend.database import get_db
from backend.services import analytics as A
from backend.services import bom_engine as B
from backend.services import forecast_service as F
from backend.forecasting.models import capability_report

router = APIRouter(prefix="/api/forecast", tags=["forecast"])


@router.get("/capabilities")
def capabilities():
    return {"models": capability_report(), "series": F.DEFAULT_SERIES}


@router.get("")
def get_forecast(commodity: str, provider: str | None = None, product: str | None = None,
                 compute: bool = False, db=Depends(get_db)):
    ccode = commodity.upper()
    if provider is None and product is None:
        provider, product = B.headline_of(ccode)
    out = F.get_forecast(db, ccode, provider, product, compute_if_missing=compute)
    if not out.get("available"):
        raise HTTPException(404, out.get("message", "no forecast available"))

    s = A.daily_series(A.load_series(db, ccode, provider, product))
    if not s.empty:
        hist = s.tail(260)
        out["history"] = [{"date": d.strftime("%Y-%m-%d"), "price": round(float(v), 2)}
                          for d, v in hist.items()]
        out["last_price"] = round(float(s.iloc[-1]), 2)
        out["volatility"] = A.volatility(s)
        out["band"] = A.price_band(s)
        fpct = None
        for r in out["results"]:
            if r["horizon_days"] == 30:
                fpct = (r["forecast_price"] - float(s.iloc[-1])) / float(s.iloc[-1]) * 100
        out["trend"] = A.classify_trend(s, fpct)
    out["disclaimer"] = ("Forecasts are statistical projections from historical "
                         "prices with back-tested error bands. They are not a "
                         "guarantee of future market levels.")
    return out


@router.post("/refresh")
def refresh(commodity: str | None = None, db=Depends(get_db)):
    """Recompute and persist. Slow by design - this is the retrain path."""
    series = None
    if commodity:
        c = commodity.upper()
        series = [s for s in F.DEFAULT_SERIES if s[0] == c]
        if not series:
            raise HTTPException(404, f"no default series for commodity {c}")
    return F.refresh_forecasts(series=series)


@router.get("/leaderboard")
def leaderboard(commodity: str, provider: str | None = None, product: str | None = None,
                db=Depends(get_db)):
    """Why this model won: the full back-test table."""
    ccode = commodity.upper()
    if provider is None and product is None:
        provider, product = B.headline_of(ccode)
    key = F._key(ccode, provider, product)
    cached = F._cache_get(key)
    if not cached:
        out = F.get_forecast(db, ccode, provider, product, compute_if_missing=True)
        cached = F._cache_get(key) or {"leaderboard": out.get("leaderboard"),
                                       "method": out.get("method")}
    return {"series": key, **cached}


@router.get("/trend")
def trend(commodity: str, provider: str | None = None, product: str | None = None,
          db=Depends(get_db)):
    ccode = commodity.upper()
    if provider is None and product is None:
        provider, product = B.headline_of(ccode)
    s = A.daily_series(A.load_series(db, ccode, provider, product))
    if s.empty:
        raise HTTPException(404, "no data")
    fpct = F.forecast_pct(db, ccode, provider, product, 30)
    return {"commodity": ccode, "provider": provider, "product": product,
            "trend": A.classify_trend(s, fpct),
            "volatility": A.volatility(s), "band": A.price_band(s),
            "momentum": A.momentum(s), "moving_averages": A.moving_averages(s)}


@router.get("/volatility")
def volatility(db=Depends(get_db)):
    rows = []
    for ccode in B.HEADLINE:
        pv, pr = B.headline_of(ccode)
        s = A.daily_series(A.load_series(db, ccode, pv, pr))
        if s.empty:
            continue
        v = A.volatility(s)
        rows.append({"commodity": ccode, "provider": pv, "product": pr, **v})
    return {"rows": rows}
