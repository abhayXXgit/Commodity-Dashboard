"""Market data: KPIs, series, provider comparison, parity, quality."""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select

from backend.config import settings
from backend.database import get_db
from backend.models import (Commodity, DataQualityFlag, DataSourceLog,
                            HistoricalPrice, Product, Provider)
from backend.services import analytics as A
from backend.services import bom_engine as B
from backend.services import forecast_service as F
from backend.utils.validation import (detect_missing_dates,
                                      detect_outliers_and_jumps, is_stale)

router = APIRouter(prefix="/api/market", tags=["market"])


def _date(v: str | None) -> dt.date | None:
    return dt.date.fromisoformat(v) if v else None


@router.get("/masters")
def masters(db=Depends(get_db)):
    """Everything the UI needs to build its filters."""
    commodities = [{"code": c.code, "name": c.name, "category": c.category,
                    "benchmark": c.benchmark_source} for c in db.scalars(select(Commodity))]
    providers = [{"code": p.code, "name": p.name, "type": p.provider_type,
                  "currency": p.default_currency, "unit": p.default_unit,
                  "basis": p.default_basis, "ingestion": p.ingestion_mode}
                 for p in db.scalars(select(Provider))]
    cmap = {c.id: c.code for c in db.scalars(select(Commodity))}
    products = [{"code": p.code, "name": p.name, "grade": p.grade,
                 "commodity": cmap.get(p.commodity_id), "spec": p.specification}
                for p in db.scalars(select(Product))]
    headline = {k: {"provider": v[0], "product": v[1]} for k, v in B.HEADLINE.items()}
    return {"commodities": commodities, "providers": providers,
            "products": products, "headline": headline,
            "horizons": list(settings.FORECAST_HORIZONS),
            "ma_windows": list(settings.MA_WINDOWS)}


@router.get("/kpis")
def kpis(db=Depends(get_db)):
    """Executive KPI cards for copper and aluminium (spec §1)."""
    cards = []
    cards.append(A.kpi_block(db, "CU", "LME", "CU_CATHODE", "LME Copper Cash"))
    cards.append(A.kpi_block(db, "CU", "BME", "CU_CATHODE", "Copper - India Reference"))
    cards.append(A.kpi_block(db, "CU", "BME", "CU_WIREROD", "Copper Wire Rod"))
    for pv in ("NALCO", "BALCO", "HINDALCO"):
        cards.append(A.kpi_block(db, "AL", pv, "AL_INGOT", f"{pv.title()} Ingot"))

    # aluminium composite averages across the three producers
    for prod, label in (("AL_INGOT", "Aluminium Ingot - 3-producer average"),
                        ("AL_WIREROD", "Aluminium Wire Rod - average"),
                        ("AL_BILLET", "Aluminium Billet - average")):
        vals, d30s, d365s = [], [], []
        for pv in ("NALCO", "BALCO", "HINDALCO"):
            s = A.daily_series(A.load_series(db, "AL", pv, prod))
            if s.empty:
                continue
            vals.append(float(s.iloc[-1]))
            d30s.append(A.pct_change_over(s, 30))
            d365s.append(A.pct_change_over(s, 365))
        if vals:
            cards.append({
                "label": label, "available": True, "commodity": "AL",
                "provider": "AVERAGE", "product": prod,
                "current": round(sum(vals) / len(vals), 2),
                "d30": round(sum(x for x in d30s if x is not None) / max(len([x for x in d30s if x is not None]), 1), 2),
                "d365": round(sum(x for x in d365s if x is not None) / max(len([x for x in d365s if x is not None]), 1), 2),
                "spread": round(max(vals) - min(vals), 2),
                "data_class": "DERIVED",
                "source": "Mean of NALCO, BALCO and Hindalco published circulars",
            })

    for c in cards:
        if not c.get("available"):
            continue
        ccode, pv, pr = c["commodity"], c.get("provider"), c.get("product")
        if pv == "AVERAGE":
            continue
        fc = F.forecast_price_at(db, ccode, pv, pr, 30)
        if fc:
            c["forecast_30d"] = fc["forecast_price"]
            c["forecast_30d_pct"] = round(
                (fc["forecast_price"] - c["current"]) / c["current"] * 100, 2)
            c["forecast_model"] = fc["model_used"]
    return {"cards": cards, "generated_at": dt.datetime.now(dt.timezone.utc).isoformat()}


@router.get("/series")
def series(commodity: str, provider: str | None = None, product: str | None = None,
           start: str | None = None, end: str | None = None,
           period: str | None = Query(None, description="7D,30D,90D,180D,365D,3Y,5Y,ALL"),
           db=Depends(get_db)):
    s_date, e_date = _date(start), _date(end)
    if period and not s_date:
        days = {"7D": 7, "30D": 30, "90D": 90, "180D": 180, "365D": 365,
                "3Y": 1095, "5Y": 1825}.get(period.upper())
        if days:
            s_date = dt.date.today() - dt.timedelta(days=days)
    out = A.series_payload(db, commodity.upper(),
                           provider.upper() if provider else None,
                           product.upper() if product else None, s_date, e_date)
    if not out.get("available"):
        raise HTTPException(404, out.get("message", "no data"))
    out["trend"] = A.classify_trend(
        A.daily_series(A.load_series(db, commodity.upper(),
                                     provider.upper() if provider else None,
                                     product.upper() if product else None, s_date, e_date)),
        F.forecast_pct(db, commodity.upper(),
                       provider.upper() if provider else None,
                       product.upper() if product else None, 30))
    return out


@router.get("/providers/compare")
def compare(commodity: str = "AL", db=Depends(get_db)):
    return A.provider_comparison(db, commodity.upper())


@router.get("/copper/parity")
def parity(db=Depends(get_db)):
    return A.copper_parity(db)


@router.get("/aluminium/stats")
def al_stats(db=Depends(get_db)):
    return {"rows": A.aluminium_stats(db)}


@router.get("/live")
def live_status(db=Depends(get_db)):
    """
    Live-feed truthfulness endpoint. Never claims a live price that does not
    exist - reports exactly what each configured source last did.
    """
    from backend.data_sources.circular_adapter import build_producer_adapters
    from backend.data_sources.fx_adapter import FxAdapter
    from backend.data_sources.lme_adapter import LMEAdapter, MetalsFeedAdapter

    adapters = [LMEAdapter(), MetalsFeedAdapter(), FxAdapter(), *build_producer_adapters()]
    sources = []
    for a in adapters:
        last = db.scalar(select(DataSourceLog)
                         .where(DataSourceLog.adapter == a.name)
                         .order_by(DataSourceLog.run_started.desc()).limit(1))
        last_ok = db.scalar(select(DataSourceLog)
                            .where(DataSourceLog.adapter == a.name,
                                   DataSourceLog.status == "SUCCESS")
                            .order_by(DataSourceLog.run_started.desc()).limit(1))
        stale, why = is_stale(last_ok.run_started if last_ok else None,
                              settings.STALE_AFTER_HOURS)
        sources.append({
            "adapter": a.name, "source": a.source_name,
            "configured": a.is_configured(),
            "missing_config": a.missing_config(),
            "last_run": last.run_started.isoformat() if last else None,
            "last_status": last.status if last else "NEVER_RUN",
            "last_error": last.error_message if last else None,
            "last_success": last_ok.run_started.isoformat() if last_ok else None,
            "stale": stale, "stale_reason": why,
        })

    any_live = any(s["last_status"] == "SUCCESS" and not s["stale"] for s in sources)
    latest = db.scalar(select(func.max(HistoricalPrice.price_date)))
    classes = [r[0] for r in db.execute(select(HistoricalPrice.data_class).distinct()).all()]

    # Freshness is a PER-COMMODITY fact, not a global one. One producer circular
    # landing for aluminium says nothing about copper, and letting a single
    # success clear the warning banner would overstate the data for every other
    # commodity on the screen.
    per_commodity = []
    for ccode in B.HEADLINE:
        pv, pr = B.headline_of(ccode)
        df = A.load_series(db, ccode, pv, pr)
        if df.empty:
            per_commodity.append({"commodity": ccode, "provider": pv, "product": pr,
                                  "status": "NO_DATA", "data_class": None, "as_of": None})
            continue
        last = df.iloc[-1]
        dclass = str(last["data_class"])
        as_of = df["date"].max().date()
        age_days = (dt.date.today() - as_of).days
        if dclass == "DEMO_DATA":
            status = "DEMO"
        elif age_days > 3:
            status = "STALE"
        else:
            status = "CURRENT"
        per_commodity.append({
            "commodity": ccode, "provider": pv, "product": pr,
            "status": status, "data_class": dclass,
            "as_of": as_of.isoformat(), "age_days": age_days,
            "source": str(last["source"])[:120],
        })

    demo_commodities = [c["commodity"] for c in per_commodity if c["status"] == "DEMO"]
    stale_commodities = [c["commodity"] for c in per_commodity if c["status"] == "STALE"]

    if demo_commodities:
        banner = (f"Live data unavailable for {', '.join(demo_commodities)} - "
                  f"last verified price shown")
    elif stale_commodities:
        banner = (f"Data for {', '.join(stale_commodities)} has not updated in "
                  f"over 3 days - last verified price shown")
    else:
        banner = None

    return {
        "data_mode": settings.DATA_MODE,
        "live_available": any_live,
        "fully_current": not demo_commodities and not stale_commodities,
        "banner": banner,
        "commodities": per_commodity,
        "demo_commodities": demo_commodities,
        "stale_commodities": stale_commodities,
        "latest_price_date": latest.isoformat() if latest else None,
        "data_classes_present": classes,
        "demo_data": "DEMO_DATA" in classes,
        "sources": sources,
        "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }


@router.get("/quality")
def quality(db=Depends(get_db)):
    """Data-quality control panel (spec §18)."""
    report = []
    for ccode in B.HEADLINE:
        pv, pr = B.headline_of(ccode)
        df = A.load_series(db, ccode, pv, pr)
        if df.empty:
            continue
        dates = [d.date() for d in df["date"]]
        pairs = list(zip(dates, df["price"].tolist()))
        missing = detect_missing_dates(dates)
        anomalies = detect_outliers_and_jumps(pairs)
        report.append({
            "commodity": ccode, "provider": pv, "product": pr,
            "observations": len(df),
            "first": dates[0].isoformat(), "last": dates[-1].isoformat(),
            "missing_business_days": len(missing),
            "missing_sample": [m.detail for m in missing[:8]],
            "jumps": len([a for a in anomalies if a.flag_type == "JUMP"]),
            "outliers": len([a for a in anomalies if a.flag_type == "OUTLIER"]),
            "anomaly_sample": [a.detail for a in anomalies[:8]],
            "negative_prices": int((df["price"] <= 0).sum()),
            "data_classes": sorted(df["data_class"].unique().tolist()),
        })
    flags = [{"id": f.id, "table": f.table_name, "type": f.flag_type,
              "detail": f.detail, "severity": f.severity,
              "detected_at": f.detected_at.isoformat() if f.detected_at else None}
             for f in db.scalars(select(DataQualityFlag)
                                 .order_by(DataQualityFlag.detected_at.desc()).limit(50))]
    return {"series": report, "stored_flags": flags}


@router.get("/fx")
def fx(db=Depends(get_db)):
    out = {}
    for base in ("USD", "EUR"):
        rate, date, src = A.latest_fx(db, base)
        out[f"{base}INR"] = {"rate": round(rate, 4),
                             "date": date.isoformat() if date else None, "source": src}
    return out
