"""
Procurement decision-support engine (spec §9 and §25.12).

These are PROCUREMENT DECISION-SUPPORT SIGNALS, not financial advice, and every
payload carries that disclaimer explicitly.

How the signal is produced
--------------------------
Six weighted factors each score in [-100, +100] where POSITIVE means
"buying sooner is favourable" (price pressure is upward / cover is thin):

    forecast direction  30%   expected 30-day move
    price band          20%   historical percentile - cheap is a buy
    trend               15%   composite trend score
    coverage gap        15%   how exposed the uncovered quantity is
    urgency             10%   delivery date vs supplier lead time
    budget variance     10%   market vs the approved budget rate

Volatility does not push the score - it changes the *shape* of the action
(phased vs single buy) and caps the recommended lot size, which is how a
procurement desk actually behaves.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any

from sqlalchemy import select

from backend.models import Commodity, Recommendation, Transformer, UserSetting
from backend.services import analytics as A
from backend.services import bom_engine as B
from backend.services import forecast_service as F

DISCLAIMER = ("Procurement decision-support signal generated from price, forecast, "
              "volatility, BOM exposure and coverage data. Not financial or "
              "investment advice.")

SIGNALS = ["BUY NOW", "BUY PARTIAL", "LOCK PRICE", "PHASED PROCUREMENT",
           "NEGOTIATE", "MONITOR", "WAIT", "DO NOT LOCK"]

WEIGHTS = {"forecast": .30, "band": .20, "trend": .15,
           "coverage": .15, "urgency": .10, "budget": .10}


def settings_horizons():
    from backend.config import settings as _s
    return _s.FORECAST_HORIZONS


def _setting(db, key: str, default: float | str) -> Any:
    row = db.scalar(select(UserSetting).where(UserSetting.setting_key == key))
    if not row or row.setting_value is None:
        return default
    if row.value_type == "float":
        try:
            return float(row.setting_value)
        except ValueError:
            return default
    return row.setting_value


def _nearest_delivery(db, ccode: str) -> tuple[int | None, str | None]:
    """Days to the earliest delivery that consumes this commodity."""
    today = dt.date.today()
    best, job = None, None
    for t in db.scalars(select(Transformer)):
        if not t.delivery_date:
            continue
        uses = any((b.commodity_id and
                    db.get(Commodity, b.commodity_id).code == ccode) for b in t.bom_items)
        if not uses:
            continue
        d = (t.delivery_date - today).days
        if best is None or d < best:
            best, job = d, t.job_no
    return best, job


def recommend(db, ccode: str, product: str | None = None,
              persist: bool = False) -> dict[str, Any]:
    """Full decision-support payload for one commodity."""
    pv, pr = B.headline_of(ccode)
    pr = product or pr

    s = A.daily_series(A.load_series(db, ccode, pv, pr))
    if s.empty:
        return {"available": False, "commodity": ccode,
                "message": "No price history for this commodity"}

    current = float(s.iloc[-1])
    band = A.price_band(s)
    vol = A.volatility(s)
    fc30 = F.forecast_price_at(db, ccode, pv, pr, horizon=30)
    fc90 = F.forecast_price_at(db, ccode, pv, pr, horizon=90)
    fpct = ((fc30["forecast_price"] - current) / current * 100) if fc30 else None
    trend = A.classify_trend(s, fpct)

    cov = next((c for c in B.coverage(db, ccode)), None)
    cov_pct = cov["coverage_pct"] if cov and cov["coverage_pct"] is not None else 0.0
    uncovered_exposure = cov["uncovered_exposure"] if cov else 0.0
    uncovered_mt = cov["uncovered_mt"] if cov else 0.0

    lead = (cov or {}).get("lead_time_days") or 45
    days_to_delivery, critical_job = _nearest_delivery(db, ccode)
    budget = _setting(db, f"budget_price_{ccode}", None)
    target_cover = float(_setting(db, "target_cover_pct", 70.0))

    # ---------- factor scores ----------
    f: dict[str, float] = {}
    f["forecast"] = 0.0 if fpct is None else max(-100, min(100, fpct * 10))
    p = band.get("percentile")
    f["band"] = 0.0 if p is None else (50 - p) * 2.0      # cheap -> positive (buy)
    f["trend"] = trend.get("score") or 0.0
    gap = max(target_cover - cov_pct, 0.0)
    f["coverage"] = max(-100, min(100, gap * 2.0 - (max(cov_pct - target_cover, 0.0))))
    if days_to_delivery is None:
        f["urgency"] = 0.0
    else:
        slack = days_to_delivery - lead
        f["urgency"] = 100.0 if slack <= 0 else max(-100, min(100, (60 - slack) * 2.0))
    if budget:
        f["budget"] = max(-100, min(100, (float(budget) - current) / float(budget) * 400))
    else:
        f["budget"] = 0.0

    score = sum(f[k] * w for k, w in WEIGHTS.items())
    vband = vol.get("band", "UNKNOWN")
    extreme_vol = vband in ("HIGH", "EXTREME")

    # ---------- signal ----------
    reasons: list[str] = []
    if score >= 45:
        signal = "BUY NOW"
        cover = 85.0
    elif score >= 20:
        signal = "BUY PARTIAL"
        cover = 60.0
    elif score >= 5:
        signal = "LOCK PRICE" if (fpct or 0) > 2 else "MONITOR"
        cover = 45.0
    elif score > -20:
        signal = "MONITOR"
        cover = 30.0
    elif score > -45:
        signal = "NEGOTIATE"
        cover = 20.0
    else:
        signal = "WAIT"
        cover = 10.0

    # volatility reshapes the action rather than the score
    if extreme_vol and signal in ("BUY NOW", "BUY PARTIAL"):
        signal = "PHASED PROCUREMENT"
        cover = min(cover, 65.0)
        reasons.append(f"Volatility is {vband} ({vol.get('annualised'):.1f}% annualised), "
                       f"so the requirement is better staged than bought in one lot.")
    if vband == "EXTREME" and signal == "LOCK PRICE":
        signal = "DO NOT LOCK"
        reasons.append("Volatility is EXTREME - locking a long-dated price here "
                       "transfers risk to us at a poor moment.")
    if days_to_delivery is not None and days_to_delivery <= lead and cov_pct < target_cover:
        signal = "BUY NOW"
        cover = max(cover, 80.0)
        reasons.insert(0, f"Delivery for {critical_job} is {days_to_delivery} days out "
                          f"against a {lead}-day supplier lead time with only "
                          f"{cov_pct:.0f}% covered - this is now schedule-driven.")

    # ---------- rationale ----------
    if fpct is not None:
        reasons.append(
            f"The 30-day forecast is {fc30['forecast_price']:,.0f} INR/MT "
            f"({fpct:+.2f}%) from {current:,.0f}, produced by {fc30['model_used']} "
            f"with {fc30['mape']:.2f}% back-tested MAPE ({fc30['confidence']} confidence).")
    if p is not None:
        reasons.append(
            f"Price sits at the {p:.0f}th percentile of its 3-year range "
            f"({band['low']:,.0f} - {band['high']:,.0f}), a {band['zone']} zone.")
    reasons.append(f"Trend reads {trend['trend']} (composite score {trend['score']:+.0f}).")
    if cov:
        reasons.append(
            f"Requirement is {cov['requirement_mt']:.2f} MT with {cov_pct:.0f}% covered; "
            f"{uncovered_mt:.2f} MT remains uncovered, worth "
            f"{uncovered_exposure/1e7:.2f} Cr at today's rate.")
    if fc30 and uncovered_mt:
        fut = (fc30["forecast_price"] - current) * uncovered_mt
        reasons.append(
            f"On the forecast, the uncovered quantity alone moves the bill by "
            f"{fut/1e5:+,.1f} Lakh over 30 days.")
    if budget:
        gapb = (current - float(budget)) / float(budget) * 100
        reasons.append(f"Market is {gapb:+.2f}% against the approved budget rate "
                       f"of {float(budget):,.0f} INR/MT.")

    suggested_mt = round(uncovered_mt * cover / 100, 3) if uncovered_mt else 0.0

    payload = {
        "available": True, "commodity": ccode, "provider": pv, "product": pr,
        "signal": signal, "score": round(score, 1),
        "confidence": ("HIGH" if fc30 and fc30["confidence"] == "HIGH" and not extreme_vol
                       else "LOW" if not fc30 else "MEDIUM"),
        "suggested_cover_pct": cover,
        "suggested_lot_mt": suggested_mt,
        "current_price": round(current, 2),
        "forecast_30d": fc30, "forecast_90d": fc90,
        "forecast_change_pct": round(fpct, 3) if fpct is not None else None,
        "trend": trend, "volatility": vol, "band": band,
        "coverage": cov,
        "days_to_nearest_delivery": days_to_delivery,
        "critical_job": critical_job, "lead_time_days": lead,
        "budget_price": float(budget) if budget else None,
        "factors": {k: round(v, 1) for k, v in f.items()},
        "weights": WEIGHTS,
        "rationale": " ".join(reasons),
        "reasons": reasons,
        "disclaimer": DISCLAIMER,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }

    if persist:
        db.add(Recommendation(
            commodity_id=A.resolve_ids(db)["commodity"][ccode], signal=signal,
            confidence=payload["confidence"], score=score, suggested_cover_pct=cover,
            rationale=payload["rationale"],
            inputs_json=json.dumps({k: payload[k] for k in
                                    ("current_price", "forecast_change_pct", "factors")},
                                   default=str)))
    return payload


# --------------------------------------------------------------------------- §25.16
def price_lock_analysis(db, ccode: str, quantity_mt: float | None = None,
                        lock_period_days: int = 90,
                        supplier_premium: float = 0.0) -> dict[str, Any]:
    """
    Lock-now vs wait, valued over the lock period, with best/base/worst cases
    derived from realised volatility rather than invented percentages.
    """
    pv, pr = B.headline_of(ccode)
    s = A.daily_series(A.load_series(db, ccode, pv, pr))
    if s.empty:
        return {"available": False, "message": "No price history"}

    cur = float(s.iloc[-1])
    vol = A.volatility(s)
    cov = next((c for c in B.coverage(db, ccode)), None)
    qty = quantity_mt if quantity_mt is not None else (cov["uncovered_mt"] if cov else 0.0)

    # Value the wait against the forecast horizon closest to the lock period.
    # A 30-day lock judged on the 90-day forecast prices the wrong decision.
    horizons = list(settings_horizons())
    horizon = min(horizons, key=lambda h: abs(h - lock_period_days))
    fc = F.forecast_price_at(db, ccode, pv, pr, horizon=horizon)
    if not fc:                                   # fall back to any stored horizon
        for h in sorted(horizons, key=lambda h: abs(h - lock_period_days)):
            fc = F.forecast_price_at(db, ccode, pv, pr, horizon=h)
            if fc:
                horizon = h
                break
    expected = fc["forecast_price"] if fc else cur

    ann = (vol.get("annualised") or 20.0) / 100.0
    sigma = ann * (lock_period_days / 365.0) ** 0.5           # horizon sigma, fraction
    lock_price = cur + supplier_premium

    best = expected * (1 - 1.645 * sigma)      # 5th percentile of the wait outcome
    worst = expected * (1 + 1.645 * sigma)     # 95th percentile

    lock_cost = lock_price * qty
    rows = []
    for label, price in (("Best case (wait)", best), ("Base case (wait)", expected),
                         ("Worst case (wait)", worst)):
        wait_cost = price * qty
        rows.append({
            "scenario": label, "price": round(price, 2),
            "wait_cost": round(wait_cost, 2),
            "vs_lock": round(lock_cost - wait_cost, 2),
            "outcome": "Locking saves" if lock_cost < wait_cost else "Waiting saves",
        })

    expected_saving = (expected - lock_price) * qty     # >0 means locking is better
    if expected_saving > 0 and vol.get("band") != "EXTREME":
        reco_pct = 70.0
    elif expected_saving > 0:
        reco_pct = 50.0
    elif abs(expected_saving) < lock_cost * 0.01:
        reco_pct = 40.0
    else:
        reco_pct = 20.0

    return {
        "available": True, "commodity": ccode,
        "current_price": round(cur, 2), "supplier_premium": round(supplier_premium, 2),
        "lock_price": round(lock_price, 2), "quantity_mt": round(qty, 4),
        "lock_period_days": lock_period_days,
        "lock_now_cost": round(lock_cost, 2),
        "expected_price": round(expected, 2),
        "forecast_horizon_days": horizon,
        "forecast_model": fc["model_used"] if fc else None,
        "expected_wait_cost": round(expected * qty, 2),
        "annualised_volatility": vol.get("annualised"),
        "horizon_sigma_pct": round(sigma * 100, 2),
        "scenarios": rows,
        "potential_saving": round(max(expected_saving, 0.0), 2),
        "potential_exposure": round(max(-expected_saving, 0.0), 2),
        "recommended_lock_pct": reco_pct,
        "recommended_lock_mt": round(qty * reco_pct / 100, 3),
        "disclaimer": DISCLAIMER,
    }


# --------------------------------------------------------------------------- §25.20
def priority_matrix(db) -> dict[str, Any]:
    """
    Project x commodity risk grid.
      x = commodity price risk (volatility band + forecast direction + percentile)
      y = project exposure (INR value of that commodity in the project)
    HIGH RISK + HIGH EXPOSURE => CRITICAL PROCUREMENT PRIORITY.
    """
    roll = B.bom_rollup(db)
    risk_by_commodity: dict[str, dict] = {}
    for ccode in roll["by_commodity"]:
        if not ccode:
            continue
        headline = B.headline_of(ccode)
        s = A.daily_series(A.load_series(db, ccode, headline[0], headline[1]))
        if s.empty:
            continue
        vol = A.volatility(s)
        band = A.price_band(s)
        fpct = F.forecast_pct(db, ccode, headline[0], headline[1], 30) or 0.0
        # 0-100 price-risk score
        risk = (min((vol.get("annualised") or 0) / 30 * 100, 100) * 0.45
                + max(min(fpct * 8 + 50, 100), 0) * 0.35
                + (band.get("percentile") or 50) * 0.20)
        risk_by_commodity[ccode] = {
            "risk_score": round(risk, 1),
            "risk_band": "HIGH" if risk >= 60 else "MEDIUM" if risk >= 40 else "LOW",
            "volatility": vol.get("annualised"), "vol_band": vol.get("band"),
            "forecast_pct": round(fpct, 2), "percentile": band.get("percentile"),
        }

    exposures = []
    for t in roll["transformers"]:
        per_cmdty: dict[str, float] = {}
        for i in t["items"]:
            per_cmdty[i["commodity"]] = per_cmdty.get(i["commodity"], 0.0) + i["project_material_cost"]
        for ccode, value in per_cmdty.items():
            r = risk_by_commodity.get(ccode)
            if not r:
                continue
            exposures.append({
                "job_no": t["job_no"], "customer": t["customer"],
                "rating_mva": t["rating_mva"], "quantity": t["quantity"],
                "delivery_date": t["delivery_date"],
                "commodity": ccode, "exposure": round(value, 2),
                "risk_score": r["risk_score"], "risk_band": r["risk_band"],
            })

    if exposures:
        vals = sorted(e["exposure"] for e in exposures)
        hi_cut = vals[int(len(vals) * 0.6)] if len(vals) > 2 else (vals[-1] if vals else 0)
    else:
        hi_cut = 0
    for e in exposures:
        hi_exp = e["exposure"] >= hi_cut and e["exposure"] > 0
        hi_risk = e["risk_band"] == "HIGH"
        e["exposure_band"] = "HIGH" if hi_exp else "LOW"
        e["quadrant"] = f"{'HIGH' if hi_risk else 'LOW'} RISK / {'HIGH' if hi_exp else 'LOW'} EXPOSURE"
        e["critical"] = hi_risk and hi_exp
        if e["critical"]:
            e["flag"] = "CRITICAL PROCUREMENT PRIORITY"

    exposures.sort(key=lambda e: (-e["critical"], -e["exposure"]))
    return {
        "risk_by_commodity": risk_by_commodity,
        "exposure_threshold": round(hi_cut, 2),
        "items": exposures,
        "critical": [e for e in exposures if e["critical"]],
    }


# --------------------------------------------------------------------------- §25.19
def monthly_exposure(db) -> dict[str, Any]:
    """Commodity exposure bucketed by delivery month."""
    roll = B.bom_rollup(db)
    buckets: dict[str, dict[str, float]] = {}
    for t in roll["transformers"]:
        m = t["delivery_month"] or "Unscheduled"
        b = buckets.setdefault(m, {})
        for i in t["items"]:
            b[i["commodity"]] = b.get(i["commodity"], 0.0) + i["project_material_cost"]
    months = sorted(buckets)
    commodities = B.CATEGORY_ORDER
    return {
        "months": months,
        "series": [{"commodity": c,
                    "values": [round(buckets[m].get(c, 0.0), 2) for m in months]}
                   for c in commodities],
        "totals": [round(sum(buckets[m].values()), 2) for m in months],
    }


# --------------------------------------------------------------------------- §25.22
def historical_cost_backtest(db, job_no: str | None = None, points: int = 160) -> dict[str, Any]:
    """
    Hold BOM consumption constant and re-price it at every historical date.
    Shows what the same transformer would have cost through the cycle.
    """
    roll = B.bom_rollup(db, [job_no] if job_no else None)
    if not roll["transformers"]:
        return {"available": False, "message": "No transformer matched"}

    if job_no:
        t = roll["transformers"][0]
        cons: dict[str, float] = {}
        for i in t["items"]:
            cons[i["commodity"]] = cons.get(i["commodity"], 0.0) + i["consumption_mt"]
        label, base = f"{t['job_no']} ({t['rating_mva']} MVA)", t["base_cost_per_unit"]
    else:
        units = roll["totals"]["units"] or 1
        cons = {}
        for t in roll["transformers"]:
            for i in t["items"]:
                cons[i["commodity"]] = cons.get(i["commodity"], 0.0) + i["consumption_mt"] * t["quantity"]
        cons = {k: v / units for k, v in cons.items()}
        label, base = f"Portfolio average ({units} units)", roll["totals"]["base_cost"] / units

    import pandas as pd
    frames = {}
    for ccode in cons:
        headline = B.HEADLINE.get(ccode)
        if not headline:
            continue
        s = A.daily_series(A.load_series(db, ccode, headline[0], headline[1]))
        if not s.empty:
            frames[ccode] = s
    if not frames:
        return {"available": False, "message": "No price history to re-price against"}

    wide = pd.concat(frames, axis=1).ffill().dropna()
    step = max(1, len(wide) // points)
    wide = wide.iloc[::step]

    series = []
    for d, row in wide.iterrows():
        mat = sum(float(row[c]) * cons[c] for c in frames)
        series.append({
            "date": d.strftime("%Y-%m-%d"),
            "material_cost": round(mat, 2),
            "total_cost": round(base + mat, 2),
            **{f"px_{c}": round(float(row[c]), 2) for c in frames},
            **{f"cost_{c}": round(float(row[c]) * cons[c], 2) for c in frames},
        })

    costs = [p["total_cost"] for p in series]
    return {
        "available": True, "label": label,
        "base_cost": round(base, 2),
        "consumption_mt": {k: round(v, 4) for k, v in cons.items()},
        "series": series,
        "min_cost": round(min(costs), 2), "max_cost": round(max(costs), 2),
        "current_cost": round(costs[-1], 2),
        "swing_pct": round((max(costs) - min(costs)) / min(costs) * 100, 2),
        "note": ("BOM consumption held constant at the current design; only "
                 "commodity prices vary. Isolates commodity effect from design change."),
    }
