"""
Management summary (spec §16) and the executive view (§25.21).

Every sentence is assembled from live numbers. Nothing is hard-coded: if a
figure is unavailable the clause is omitted rather than filled with a guess.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import select

from backend.config import settings
from backend.models import HistoricalPrice
from backend.services import alerts as AL
from backend.services import analytics as A
from backend.services import bom_engine as B
from backend.services import forecast_service as F
from backend.services import procurement as P
from backend.utils.units import inr_compact


def _pct(x, nd=1):
    return f"{x:+.{nd}f}%" if x is not None else "n/a"


def management_summary(db) -> dict[str, Any]:
    """Narrative paragraph + the structured facts it was built from."""
    sentences: list[str] = []
    facts: dict[str, Any] = {}

    # --- market movement -------------------------------------------------
    for ccode, name in (("CU", "Copper"), ("AL", "Aluminium")):
        pv, pr = B.headline_of(ccode)
        s = A.daily_series(A.load_series(db, ccode, pv, pr))
        if s.empty:
            continue
        d30 = A.pct_change_over(s, 30)
        cur = float(s.iloc[-1])
        ma = A.moving_averages(s)
        band = A.price_band(s)
        vol = A.volatility(s)
        facts[ccode] = {"current": round(cur, 2), "d30": d30,
                        "percentile": band.get("percentile"), "zone": band.get("zone"),
                        "vol_band": vol.get("band"), "vol": vol.get("annualised")}
        if d30 is not None:
            direction = "increased" if d30 > 0 else "decreased" if d30 < 0 else "was flat"
            sentences.append(
                f"{name} ({pv} {pr.replace('_', ' ').title()}) {direction} "
                f"{abs(d30):.1f}% over the last 30 days to {cur:,.0f} INR/MT.")
        if ma.get("ma90"):
            rel = "above" if cur > ma["ma90"] else "below"
            sentences.append(
                f"{name} is trading {rel} its 90-day moving average of "
                f"{ma['ma90']:,.0f}, at the {band['percentile']:.0f}th percentile of its "
                f"three-year range ({band['zone']} zone) with {vol['band'].lower()} "
                f"volatility at {vol['annualised']:.0f}% annualised.")

    # --- provider spread -------------------------------------------------
    pc = A.provider_comparison(db)
    for t in pc["table"]:
        if t["product"] == "AL_WIREROD":
            hi = max(t["entries"], key=lambda e: e["price"])
            sentences.append(
                f"On aluminium wire rod, {hi['provider'].title()} is "
                f"{hi['vs_avg_pct']:+.1f}% against the three-producer average of "
                f"{t['average']:,.0f} INR/MT; {t['lowest_provider'].title()} is the "
                f"cheapest source at {t['lowest']:,.0f} "
                f"(spread {t['spread']:,.0f} INR/MT, {t['spread_pct']:.2f}%).")
            facts["al_spread"] = t

    # --- forecast --------------------------------------------------------
    for ccode, name in (("CU", "Copper"), ("AL", "Aluminium")):
        pv, pr = B.headline_of(ccode)
        fc = F.forecast_price_at(db, ccode, pv, pr, 30)
        if not fc:
            continue
        s = A.daily_series(A.load_series(db, ccode, pv, pr))
        if s.empty:
            continue
        cur = float(s.iloc[-1])
        chg = (fc["forecast_price"] - cur) / cur * 100
        word = ("a moderate increase" if 1 < chg <= 4 else "a sharp increase" if chg > 4
                else "a moderate decline" if -4 <= chg < -1 else "a sharp decline" if chg < -4
                else "broadly flat pricing")
        sentences.append(
            f"The selected {name.lower()} model ({fc['model_used']}, "
            f"{fc['mape']:.1f}% back-tested MAPE) indicates {word} over 30 days to "
            f"{fc['forecast_price']:,.0f} INR/MT ({chg:+.1f}%).")
        facts.setdefault(ccode, {})["forecast_30d"] = fc

    # --- exposure --------------------------------------------------------
    roll = B.bom_rollup(db)
    impact = B.price_impact(db)
    if roll["transformers"]:
        sentences.append(
            f"Across {len(roll['transformers'])} live jobs covering "
            f"{roll['totals']['units']} transformers, current commodity content is "
            f"{inr_compact(roll['totals']['current_material_cost'])}, "
            f"{_pct((roll['totals']['current_material_cost'] - roll['totals']['bom_material_cost']) / roll['totals']['bom_material_cost'] * 100)} "
            f"against the rates the jobs were costed at.")
        sentences.append(
            f"On current forecasts the portfolio carries "
            f"{inr_compact(impact['total_project_exposure'])} of commodity exposure, "
            f"{inr_compact(impact['total_impact_per_transformer'])} per transformer.")
    facts["exposure"] = {"total_project_exposure": impact["total_project_exposure"],
                         "per_transformer": impact["total_impact_per_transformer"],
                         "material_value": roll["totals"]["current_material_cost"]}

    # --- coverage --------------------------------------------------------
    for cov in B.coverage(db):
        if cov["commodity"] in ("CU", "AL") and cov["coverage_pct"] is not None:
            sentences.append(
                f"{cov['commodity']} coverage stands at {cov['coverage_pct']:.0f}% of a "
                f"{cov['requirement_mt']:.1f} MT requirement, leaving "
                f"{cov['uncovered_mt']:.1f} MT uncovered worth "
                f"{inr_compact(cov['uncovered_exposure'])} at today's rate.")
            facts.setdefault("coverage", {})[cov["commodity"]] = cov

    # --- recommendation --------------------------------------------------
    recos = {}
    for ccode in ("CU", "AL"):
        r = P.recommend(db, ccode)
        if r.get("available"):
            recos[ccode] = {"signal": r["signal"], "score": r["score"],
                            "cover_pct": r["suggested_cover_pct"],
                            "lot_mt": r["suggested_lot_mt"]}
    if recos:
        parts = [f"{c} {v['signal'].lower()}" for c, v in recos.items()]
        sentences.append("Procurement decision support: " + "; ".join(parts) + ".")
    facts["recommendations"] = recos

    # --- data provenance -------------------------------------------------
    classes = [r[0] for r in db.execute(
        select(HistoricalPrice.data_class).distinct()).all()]
    if "DEMO_DATA" in classes:
        sentences.append(
            "Note: the price history in this environment is DEMO DATA - a seeded "
            "simulation, not market data. Connect a licensed feed or import official "
            "circulars before using these figures commercially.")
    facts["data_classes"] = classes
    facts["data_mode"] = settings.DATA_MODE

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "summary": " ".join(sentences),
        "sentences": sentences,
        "facts": facts,
        "disclaimer": P.DISCLAIMER,
    }


def executive_view(db) -> dict[str, Any]:
    """The single management screen from spec §25.21."""
    market, risk, action = {}, {}, {}
    for ccode, name in (("CU", "Copper"), ("AL", "Aluminium")):
        pv, pr = B.headline_of(ccode)
        s = A.daily_series(A.load_series(db, ccode, pv, pr))
        if s.empty:
            continue
        cur = float(s.iloc[-1])
        market[ccode] = {
            "name": name, "price": round(cur, 2),
            "d1": A.pct_change_over(s, 1), "d30": A.pct_change_over(s, 30),
            "provider": pv, "product": pr,
            "as_of": s.index[-1].strftime("%Y-%m-%d"),
        }
        r = P.recommend(db, ccode)
        if r.get("available"):
            action[ccode] = {"signal": r["signal"], "score": r["score"],
                             "cover_pct": r["suggested_cover_pct"],
                             "lot_mt": r["suggested_lot_mt"],
                             "reasons": r["reasons"][:3]}

    pm = P.priority_matrix(db)
    for c, v in pm["risk_by_commodity"].items():
        risk[c] = v
    bands = [v["risk_band"] for v in pm["risk_by_commodity"].values()]
    overall = "HIGH" if "HIGH" in bands else "MEDIUM" if "MEDIUM" in bands else "LOW"

    roll = B.bom_rollup(db)
    exposure = {c: v["current_value"] for c, v in roll["by_commodity"].items()}
    total_exposure = sum(exposure.values())

    fc_exposure = {}
    for horizon in (30, 90):
        tot = 0.0
        for ccode, agg in roll["by_commodity"].items():
            pv, pr = B.headline_of(ccode)
            fc = F.forecast_price_at(db, ccode, pv, pr, horizon)
            cur = B._headline_price(db, ccode, roll["rates"])
            price = fc["forecast_price"] if fc else cur
            tot += agg["requirement_mt"] * price
        fc_exposure[f"{horizon}d"] = round(tot, 2)

    top = pm["critical"][0] if pm["critical"] else (pm["items"][0] if pm["items"] else None)

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "market": market,
        "exposure": {k: round(v, 2) for k, v in exposure.items()},
        "total_exposure": round(total_exposure, 2),
        "forecast_exposure": fc_exposure,
        "potential_exposure_30d": round(fc_exposure["30d"] - total_exposure, 2),
        "potential_exposure_90d": round(fc_exposure["90d"] - total_exposure, 2),
        "risk": risk, "overall_risk": overall,
        "action": action,
        "top_project_at_risk": top,
        "open_alerts": len(AL.recent_alerts(db, 100)),
        "disclaimer": P.DISCLAIMER,
    }


def procurement_kpis(db) -> dict[str, Any]:
    """Supply-chain KPI scorecard (spec §25.23)."""
    from backend.models import PurchaseHistory
    from backend.services import quotes as Q

    roll = B.bom_rollup(db)
    cov = {c["commodity"]: c for c in B.coverage(db)}
    out = {}

    for ccode in ("CU", "AL"):
        pv, pr = B.headline_of(ccode)
        s = A.daily_series(A.load_series(db, ccode, pv, pr))
        if s.empty:
            continue
        market = float(s.iloc[-1])
        cid = A.resolve_ids(db)["commodity"][ccode]
        pos = list(db.scalars(select(PurchaseHistory)
                              .where(PurchaseHistory.commodity_id == cid)
                              .order_by(PurchaseHistory.po_date)))
        qty = sum(float(p.quantity_mt) for p in pos)
        spend = sum(float(p.quantity_mt) * float(p.rate_inr_mt) for p in pos)
        avg_buy = spend / qty if qty else None
        bench_avg = float(s.tail(len(pos) * 21 or 260).mean())

        agg = roll["by_commodity"].get(ccode, {})
        bom_rate = (agg.get("bom_value", 0) / agg["requirement_mt"]
                    if agg.get("requirement_mt") else None)

        qs = [q for q in Q.list_quotes(db, ccode, include_expired=False)
              if q["effective_price"]]
        best_q = min(qs, key=lambda q: q["landed_cost"]) if qs else None
        worst_q = max(qs, key=lambda q: q["landed_cost"]) if qs else None

        fc = F.forecast_price_at(db, ccode, pv, pr, 30)
        c = cov.get(ccode, {})

        out[ccode] = {
            "commodity": ccode,
            "market_price": round(market, 2),
            "benchmark_avg_period": round(bench_avg, 2),
            "average_buying_price": round(avg_buy, 2) if avg_buy else None,
            "purchase_timing_gain_per_mt": round(bench_avg - avg_buy, 2) if avg_buy else None,
            "purchase_timing_gain_total": round((bench_avg - avg_buy) * qty, 2) if avg_buy else None,
            "quantity_purchased_mt": round(qty, 3),
            "total_spend": round(spend, 2),
            "bom_rate": round(bom_rate, 2) if bom_rate else None,
            "bom_vs_market_pct": round((market - bom_rate) / bom_rate * 100, 3) if bom_rate else None,
            "market_vs_po_pct": round((market - float(c["avg_po_rate"])) / float(c["avg_po_rate"]) * 100, 3)
            if c.get("avg_po_rate") else None,
            "commodity_cost_exposure": agg.get("current_value"),
            "uncovered_exposure": c.get("uncovered_exposure"),
            "open_po_coverage_pct": round(
                (c["open_po_mt"] + c["confirmed_mt"]) / c["requirement_mt"] * 100, 2)
            if c.get("requirement_mt") else None,
            "coverage_pct": c.get("coverage_pct"),
            "best_supplier": best_q["supplier"] if best_q else None,
            "best_supplier_landed": best_q["landed_cost"] if best_q else None,
            "worst_supplier": worst_q["supplier"] if worst_q else None,
            "worst_supplier_landed": worst_q["landed_cost"] if worst_q else None,
            "supplier_price_variance_pct": round(
                (worst_q["landed_cost"] - best_q["landed_cost"]) / best_q["landed_cost"] * 100, 3)
            if best_q and worst_q else None,
            "forecast_accuracy_mape": fc["mape"] if fc else None,
            "forecast_model": fc["model_used"] if fc else None,
            "price_avoidance": round((bench_avg - avg_buy) * qty, 2) if avg_buy else None,
        }
    return {"generated_at": dt.datetime.now(dt.timezone.utc).isoformat(), "kpis": out}
