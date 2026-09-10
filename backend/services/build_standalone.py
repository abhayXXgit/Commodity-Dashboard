"""
Build `frontend/standalone.html` — a single file that opens with no server,
no Python and no network.

It is generated FROM the live database, so the numbers in it are exactly the
numbers the API serves at build time. A snapshot timestamp is stamped into the
page and every figure keeps its data_class, so nobody can mistake the snapshot
for a live feed.

    python -m backend.services.build_standalone
"""
from __future__ import annotations

import base64
import datetime as dt
import json
from pathlib import Path

from backend.config import settings
from backend.database import SessionLocal
from backend.services import alerts as AL
from backend.services import analytics as A
from backend.services import bom_engine as B
from backend.services import forecast_service as F
from backend.services import procurement as P
from backend.services import quotes as Q
from backend.services import summary as S
from backend.utils.logging_config import get_logger

log = get_logger(__name__)

FRONTEND = Path(settings.FRONTEND_DIR)
# 3 years weekly + 6 months daily keeps the file small while preserving both
# the long-run shape and the recent detail a buyer actually looks at.
WEEKLY_YEARS = 3
DAILY_DAYS = 190


def _series_payload(db, ccode, pv, pr, label):
    df = A.load_series(db, ccode, pv, pr)
    s = A.daily_series(df)
    if s.empty:
        return None
    weekly = s.resample("W-FRI").last().dropna()
    cutoff = s.index[-1] - dt.timedelta(days=DAILY_DAYS)
    daily = s[s.index > cutoff]
    merged = weekly[weekly.index <= cutoff]._append(daily) if len(weekly) else daily

    ma = {}
    for w in (7, 30, 90, 200):
        if len(s) >= w:
            r = s.rolling(w).mean()
            ma[f"ma{w}"] = [None if v != v else round(float(v), 2)
                            for v in r.reindex(merged.index)]
    last = df.iloc[-1]
    return {
        "key": f"{ccode}|{pv}|{pr}", "label": label,
        "commodity": ccode, "provider": pv, "product": pr,
        "dates": [d.strftime("%Y-%m-%d") for d in merged.index],
        "prices": [round(float(v), 2) for v in merged.values],
        "ma": ma,
        "current": round(float(s.iloc[-1]), 2),
        "as_of": s.index[-1].strftime("%Y-%m-%d"),
        "changes": {k: A._r(A.pct_change_over(s, v)) for k, v in A.PERIODS.items()},
        "moving_averages": A.moving_averages(s),
        "volatility": A.volatility(s),
        "band": A.price_band(s),
        "momentum": A.momentum(s),
        "trend": A.classify_trend(s, F.forecast_pct(db, ccode, pv, pr, 30)),
        "source": str(last["source"]), "data_class": str(last["data_class"]),
        "source_price": round(float(last["src_price"]), 2),
        "source_currency": str(last["currency"]), "source_unit": str(last["unit"]),
        "monthly": A.resample_frames(s)["monthly"],
        "forecast": F.stored_forecasts(db, ccode, pv, pr),
        "forecast_path": (F._cache_get(F._key(ccode, pv, pr)) or {}).get("paths", {}).get("30"),
    }


def collect(db) -> dict:
    series = []
    for ccode, pv, pr, label in F.DEFAULT_SERIES:
        p = _series_payload(db, ccode, pv, pr, label)
        if p:
            series.append(p)

    roll = B.bom_rollup(db)
    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "app": settings.APP_NAME, "version": settings.APP_VERSION,
        "data_mode": settings.DATA_MODE,
        "series": series,
        "kpi_cards": _kpis(db),
        "compare": A.provider_comparison(db, "AL"),
        "al_stats": A.aluminium_stats(db),
        "parity": A.copper_parity(db),
        "rollup": {"transformers": roll["transformers"],
                   "by_commodity": roll["by_commodity"], "totals": roll["totals"]},
        "impact": B.price_impact(db),
        "waterfall": B.cost_waterfall(db),
        "sensitivity": {c: B.sensitivity(db, c) for c in ("CU", "AL")},
        "coverage": B.coverage(db),
        "monthly": P.monthly_exposure(db),
        "matrix": P.priority_matrix(db),
        "flow": {c: _flow(db, c) for c in ("CU", "AL")},
        "backtest": P.historical_cost_backtest(db),
        "recommendations": [P.recommend(db, c) for c in B.HEADLINE],
        "price_lock": {c: P.price_lock_analysis(db, c) for c in ("CU", "AL")},
        "quotes": Q.list_quotes(db),
        "alerts": AL.recent_alerts(db, 40),
        "summary": S.management_summary(db),
        "executive": S.executive_view(db),
        "proc_kpis": S.procurement_kpis(db),
        "headline": {k: {"provider": v[0], "product": v[1]} for k, v in B.HEADLINE.items()},
    }


def _kpis(db):
    cards = [A.kpi_block(db, "CU", "LME", "CU_CATHODE", "LME Copper Cash"),
             A.kpi_block(db, "CU", "BME", "CU_CATHODE", "Copper - India Reference"),
             A.kpi_block(db, "CU", "BME", "CU_WIREROD", "Copper Wire Rod")]
    for pv in ("NALCO", "BALCO", "HINDALCO"):
        cards.append(A.kpi_block(db, "AL", pv, "AL_INGOT", f"{pv.title()} Ingot"))
    for c in cards:
        if c.get("available"):
            fc = F.forecast_price_at(db, c["commodity"], c["provider"], c["product"], 30)
            if fc:
                c["forecast_30d"] = fc["forecast_price"]
                c["forecast_30d_pct"] = round(
                    (fc["forecast_price"] - c["current"]) / c["current"] * 100, 2)
                c["forecast_model"] = fc["model_used"]
    return [c for c in cards if c.get("available")]


def _flow(db, ccode):
    roll = B.bom_rollup(db)
    agg = roll["by_commodity"].get(ccode)
    if not agg:
        return None
    pv, pr = B.headline_of(ccode)
    price = B._headline_price(db, ccode, roll["rates"])
    units = roll["totals"]["units"] or 1
    per_unit = agg["requirement_mt"] / units
    fc = F.forecast_price_at(db, ccode, pv, pr, 30)
    fprice = fc["forecast_price"] if fc else price
    reco = P.recommend(db, ccode)
    return {"commodity": ccode, "provider": pv, "product": pr, "price": round(price, 2),
            "consumption_mt_per_transformer": round(per_unit, 4),
            "material_cost_per_transformer": round(per_unit * price, 2),
            "quantity": units, "total_exposure": round(agg["requirement_mt"] * price, 2),
            "forecast_price": round(fprice, 2), "forecast_horizon_days": 30,
            "forecast_model": fc["model_used"] if fc else None,
            "forecast_exposure": round(agg["requirement_mt"] * fprice, 2),
            "potential_exposure": round(agg["requirement_mt"] * (fprice - price), 2),
            "recommendation": reco.get("signal"),
            "recommendation_reasons": reco.get("reasons", [])[:3]}


def build(out_path: Path | None = None) -> Path:
    out_path = out_path or (FRONTEND / "standalone.html")
    with SessionLocal() as db:
        data = collect(db)

    css = (FRONTEND / "css" / "style.css").read_text(encoding="utf-8")
    util = (FRONTEND / "js" / "util.js").read_text(encoding="utf-8")
    charts = (FRONTEND / "js" / "charts.js").read_text(encoding="utf-8")
    shell = (FRONTEND / "standalone_shell.html").read_text(encoding="utf-8")
    app = (FRONTEND / "js" / "standalone_app.js").read_text(encoding="utf-8")

    echarts_path = FRONTEND / "assets" / "echarts.min.js"
    if echarts_path.exists():
        echarts_js = echarts_path.read_text(encoding="utf-8")
        echarts_block = f"<script>{echarts_js}</script>"
    else:
        echarts_block = ('<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.1/'
                         'dist/echarts.min.js"></script>')
        log.warning("echarts asset missing - standalone will need the CDN")

    payload = json.dumps(data, separators=(",", ":"), default=str)
    # base64 keeps the JSON out of the HTML parser's way entirely: no escaping
    # of </script>, quotes or unicode to get wrong.
    b64 = base64.b64encode(payload.encode("utf-8")).decode("ascii")

    html = (shell
            .replace("/*__CSS__*/", css)
            .replace("__ECHARTS__", echarts_block)
            .replace("__DATA_B64__", b64)
            .replace("/*__UTIL__*/", util)
            .replace("/*__CHARTS__*/", charts)
            .replace("/*__APP__*/", app)
            .replace("__GENERATED__", data["generated_at"])
            .replace("__MODE__", data["data_mode"]))
    out_path.write_text(html, encoding="utf-8")
    size_mb = out_path.stat().st_size / 1e6
    log.info("standalone.html written: %.2f MB (%d series, %d transformers)",
             size_mb, len(data["series"]), len(data["rollup"]["transformers"]))
    return out_path


if __name__ == "__main__":
    p = build()
    print(f"{p}  ({p.stat().st_size/1e6:.2f} MB)")
