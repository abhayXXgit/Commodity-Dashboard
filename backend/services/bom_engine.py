"""
Transformer BOM cost-impact and project-exposure engine (spec §25).

Core business logic implemented here
------------------------------------
    Material Cost / Transformer      = commodity price x BOM consumption
    Total Project Cost Exposure      = price x consumption x transformer qty
    Material Cost Impact             = price change x consumption
    Future Procurement Exposure      = forecast price x UNPROTECTED quantity

Everything below is derived live from transformer_master + transformer_bom
joined to the latest normalised commodity price. Nothing is hard-coded.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Any

from sqlalchemy import select

from backend.models import (Commodity, ProcurementCoverage, Product,
                            Provider, Transformer)
from backend.services import analytics as A
from backend.services import forecast_service as F
from backend.utils.cache import memo

SENSITIVITY_STEPS = (-20, -15, -10, -5, 0, 5, 10, 15, 20)
CATEGORY_ORDER = ["COPPER", "ALUMINIUM", "CRGO", "STEEL", "OIL"]

# Single source of truth for "the price of X".
# The BOM is bought domestically, so the headline series for copper is the
# Indian reference (not the LME print) and for aluminium the NALCO circular.
# Every module resolves prices and forecasts through HEADLINE so a roll-up can
# never silently mix the LME series into an Indian cost calculation.
HEADLINE: dict[str, tuple[str, str]] = {
    "CU":    ("BME", "CU_CATHODE"),
    "AL":    ("NALCO", "AL_INGOT"),
    "CRGO":  ("MARKET", "CRGO_M4"),
    "STEEL": ("MARKET", "STEEL_MS"),
    "OIL":   ("MARKET", "OIL_TRF"),
}


def headline_of(ccode: str) -> tuple[str | None, str | None]:
    return HEADLINE.get(ccode, (None, None))


# --------------------------------------------------------------------------- pricing helpers
@memo(ttl=45.0)
def market_rate_map(db) -> dict[str, dict[str, Any]]:
    """
    Latest INR/MT rate for every product, with provenance.
    Producer-quoted (INR) rows win over exchange (USD) rows for the same product
    because the BOM is bought domestically.
    """
    out: dict[str, dict[str, Any]] = {}
    for p in db.scalars(select(Product)):
        df = A.load_series(db, product=p.code)
        if df.empty:
            continue
        last_day = df["date"].max()
        day = df[df["date"] == last_day]
        inr = day[day["currency"] == "INR"]
        use = inr if not inr.empty else day
        row = use.iloc[-1]
        out[p.code] = {
            "price": float(use["price"].mean()),
            "as_of": last_day.strftime("%Y-%m-%d"),
            "provider": row["provider"], "source": row["source"],
            "data_class": row["data_class"], "currency": row["currency"],
        }
    return out


def _commodity_rate(db, rates: dict, prcode: str | None, ccode: str) -> float:
    if prcode and prcode in rates:
        return rates[prcode]["price"]
    # fall back to the commodity's headline INR series
    _, prefer = headline_of(ccode)
    if prefer and prefer in rates:
        return rates[prefer]["price"]
    return 0.0


# --------------------------------------------------------------------------- core roll-up
@memo(ttl=45.0, key=lambda db, job_filter=None: ",".join(sorted(job_filter or [])) or "ALL")
def bom_rollup(db, job_filter: list[str] | None = None) -> dict[str, Any]:
    """
    Per-transformer and per-commodity cost roll-up at current market rates,
    plus the BOM-rate baseline the job was costed on.
    """
    rates = market_rate_map(db)
    cmap = {c.id: c.code for c in db.scalars(select(Commodity))}
    pmap = {p.id: p.code for p in db.scalars(select(Product))}
    vmap = {p.id: p.code for p in db.scalars(select(Provider))}

    transformers, totals_by_commodity = [], defaultdict(lambda: {
        "requirement_mt": 0.0, "current_value": 0.0, "bom_value": 0.0})
    grand = {"current_material_cost": 0.0, "bom_material_cost": 0.0,
             "base_cost": 0.0, "units": 0}

    q = select(Transformer).order_by(Transformer.job_no)
    for t in db.scalars(q):
        if job_filter and t.job_no not in job_filter:
            continue
        qty = int(t.quantity or 1)
        items, per_unit_cost, per_unit_bom = [], 0.0, 0.0
        for b in t.bom_items:
            ccode = cmap.get(b.commodity_id, "")
            prcode = pmap.get(b.product_id)
            mt = float(b.total_weight_mt or 0)
            mkt = _commodity_rate(db, rates, prcode, ccode)
            bom_rate = float(b.bom_rate_inr_mt or 0) or mkt
            cost = mt * mkt
            bcost = mt * bom_rate
            per_unit_cost += cost
            per_unit_bom += bcost
            meta = rates.get(prcode or "", {})
            items.append({
                "bom_item": b.bom_item, "category": b.material_category,
                "commodity": ccode, "product": prcode,
                "provider": vmap.get(b.provider_id),
                "grade": b.grade, "qty_kg": float(b.quantity or 0),
                "consumption_mt": round(mt, 6),
                "market_rate": round(mkt, 2), "bom_rate": round(bom_rate, 2),
                "rate_variance": round(mkt - bom_rate, 2),
                "rate_variance_pct": round((mkt - bom_rate) / bom_rate * 100, 3) if bom_rate else None,
                "material_cost": round(cost, 2),
                "bom_material_cost": round(bcost, 2),
                "project_material_cost": round(cost * qty, 2),
                "rate_as_of": meta.get("as_of"), "rate_source": meta.get("source"),
                "data_class": meta.get("data_class"),
            })
            totals_by_commodity[ccode]["requirement_mt"] += mt * qty
            totals_by_commodity[ccode]["current_value"] += cost * qty
            totals_by_commodity[ccode]["bom_value"] += bcost * qty

        base = float(t.base_cost_inr or 0)
        transformers.append({
            "job_no": t.job_no, "customer": t.customer, "type": t.transformer_type,
            "rating_mva": float(t.rating_mva), "voltage_ratio": t.voltage_ratio,
            "quantity": qty,
            "delivery_date": t.delivery_date.isoformat() if t.delivery_date else None,
            "delivery_month": t.delivery_date.strftime("%Y-%m") if t.delivery_date else None,
            "status": t.mfg_status, "bom_version": t.bom_version,
            "base_cost_per_unit": round(base, 2),
            "material_cost_per_unit": round(per_unit_cost, 2),
            "bom_material_cost_per_unit": round(per_unit_bom, 2),
            "total_cost_per_unit": round(base + per_unit_cost, 2),
            "project_material_cost": round(per_unit_cost * qty, 2),
            "project_total_cost": round((base + per_unit_cost) * qty, 2),
            "material_variance_per_unit": round(per_unit_cost - per_unit_bom, 2),
            "material_variance_pct": round((per_unit_cost - per_unit_bom) / per_unit_bom * 100, 3)
            if per_unit_bom else None,
            "copper_mt_project": round(sum(i["consumption_mt"] for i in items
                                           if i["category"] == "COPPER") * qty, 4),
            "aluminium_mt_project": round(sum(i["consumption_mt"] for i in items
                                              if i["category"] == "ALUMINIUM") * qty, 4),
            "items": items,
        })
        grand["current_material_cost"] += per_unit_cost * qty
        grand["bom_material_cost"] += per_unit_bom * qty
        grand["base_cost"] += base * qty
        grand["units"] += qty

    by_commodity = {}
    for c, v in totals_by_commodity.items():
        by_commodity[c] = {
            "requirement_mt": round(v["requirement_mt"], 4),
            "current_value": round(v["current_value"], 2),
            "bom_value": round(v["bom_value"], 2),
            "variance": round(v["current_value"] - v["bom_value"], 2),
            "variance_pct": round((v["current_value"] - v["bom_value"]) / v["bom_value"] * 100, 3)
            if v["bom_value"] else None,
        }

    return {
        "as_of": dt.date.today().isoformat(),
        "transformers": transformers,
        "by_commodity": by_commodity,
        "totals": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in grand.items()},
        "rates": rates,
    }


# --------------------------------------------------------------------------- §25.5 / §25.6 impact
def price_impact(db, changes: dict[str, float] | None = None,
                 job_filter: list[str] | None = None) -> dict[str, Any]:
    """
    Material-wise cost impact table (§25.17).
        Impact / Transformer = price change x BOM consumption
        Project exposure     = impact x transformer quantity
    `changes` maps commodity code -> INR/MT change. Omitted commodities use the
    30-day forecast delta, so the default view is the *expected* impact.
    """
    roll = bom_rollup(db, job_filter)
    rows, total_per_unit, total_project = [], 0.0, 0.0

    # per-unit consumption, weighted across the portfolio
    cons_per_unit: dict[str, float] = defaultdict(float)
    units_by_commodity: dict[str, int] = defaultdict(int)
    for t in roll["transformers"]:
        for i in t["items"]:
            cons_per_unit[i["commodity"]] += i["consumption_mt"] * t["quantity"]
            units_by_commodity[i["commodity"]] += 0
    total_units = roll["totals"]["units"] or 1

    for ccode, agg in roll["by_commodity"].items():
        if not ccode:
            continue
        delta = changes.get(ccode) if changes else None
        source = "user"
        if delta is None:
            hpv, hpr = headline_of(ccode)
            fc = F.forecast_price_at(db, ccode, hpv, hpr, horizon=30)
            cur = _headline_price(db, ccode, roll["rates"])
            if fc and cur:
                delta = fc["forecast_price"] - cur
                source = f"30-day forecast ({fc['model_used']})"
            else:
                delta, source = 0.0, "no forecast available"
        req_mt = agg["requirement_mt"]
        per_unit_mt = req_mt / total_units if total_units else 0.0
        impact_unit = delta * per_unit_mt
        impact_project = delta * req_mt
        rows.append({
            "commodity": ccode,
            "bom_mt_per_transformer": round(per_unit_mt, 4),
            "total_requirement_mt": round(req_mt, 4),
            "current_price": round(_headline_price(db, ccode, roll["rates"]), 2),
            "price_change": round(delta, 2),
            "impact_per_transformer": round(impact_unit, 2),
            "total_exposure": round(impact_project, 2),
            "current_value": agg["current_value"],
            "basis": source,
        })
        total_per_unit += impact_unit
        total_project += impact_project

    rows.sort(key=lambda r: CATEGORY_ORDER.index(r["commodity"])
              if r["commodity"] in CATEGORY_ORDER else 99)
    return {
        "rows": rows, "units": total_units,
        "total_impact_per_transformer": round(total_per_unit, 2),
        "total_project_exposure": round(total_project, 2),
        "current_material_value": roll["totals"]["current_material_cost"],
        "as_of": roll["as_of"],
    }


def _headline_price(db, ccode: str, rates: dict) -> float:
    """Current INR/MT for a commodity, taken from its headline series."""
    pv, pr = headline_of(ccode)
    s = A.daily_series(A.load_series(db, ccode, pv, pr, currency_filter="INR"))
    if not s.empty:
        return float(s.iloc[-1])
    if pr and pr in rates:
        return rates[pr]["price"]
    s = A.daily_series(A.load_series(db, ccode, currency_filter="INR"))
    return float(s.iloc[-1]) if not s.empty else 0.0


# --------------------------------------------------------------------------- §25.8 waterfall
def cost_waterfall(db, job_no: str | None = None,
                   changes: dict[str, float] | None = None) -> dict[str, Any]:
    """Base cost -> commodity impacts -> revised cost, per transformer."""
    roll = bom_rollup(db, [job_no] if job_no else None)
    if not roll["transformers"]:
        return {"available": False, "message": "No transformer matched"}

    if job_no:
        t = roll["transformers"][0]
        base = t["base_cost_per_unit"]
        cons = defaultdict(float)
        for i in t["items"]:
            cons[i["commodity"]] += i["consumption_mt"]
        label = f"{t['job_no']} - {t['rating_mva']} MVA"
        units = t["quantity"]
    else:
        base = roll["totals"]["base_cost"] / max(roll["totals"]["units"], 1)
        cons = defaultdict(float)
        for t in roll["transformers"]:
            for i in t["items"]:
                cons[i["commodity"]] += i["consumption_mt"] * t["quantity"]
        units = roll["totals"]["units"]
        for k in cons:
            cons[k] /= max(units, 1)
        label = f"Portfolio average ({units} transformers)"

    material_now = sum(cons[c] * _headline_price(db, c, roll["rates"]) for c in cons)
    steps, running = [], base + material_now
    original = running
    for ccode in sorted(cons, key=lambda c: CATEGORY_ORDER.index(c)
                        if c in CATEGORY_ORDER else 99):
        delta = (changes or {}).get(ccode)
        if delta is None:
            hpv, hpr = headline_of(ccode)
            fc = F.forecast_price_at(db, ccode, hpv, hpr, horizon=30)
            cur = _headline_price(db, ccode, roll["rates"])
            delta = (fc["forecast_price"] - cur) if (fc and cur) else 0.0
        impact = delta * cons[ccode]
        running += impact
        steps.append({"commodity": ccode, "consumption_mt": round(cons[ccode], 4),
                      "price_change": round(delta, 2), "impact": round(impact, 2)})

    return {
        "available": True, "label": label, "units": units,
        "base_cost": round(base, 2),
        "material_cost": round(material_now, 2),
        "original_cost_per_transformer": round(original, 2),
        "steps": steps,
        "revised_cost_per_transformer": round(running, 2),
        "total_impact_per_transformer": round(running - original, 2),
        "increase_pct": round((running - original) / original * 100, 3) if original else None,
        "project_impact": round((running - original) * units, 2),
    }


# --------------------------------------------------------------------------- §25.9 sensitivity
def sensitivity(db, commodity: str = "CU", job_filter: list[str] | None = None,
                steps=SENSITIVITY_STEPS) -> dict[str, Any]:
    """Cost/exposure grid across a -20%..+20% price move."""
    roll = bom_rollup(db, job_filter)
    agg = roll["by_commodity"].get(commodity)
    if not agg:
        return {"available": False, "message": f"No BOM consumption for {commodity}"}
    cur = _headline_price(db, commodity, roll["rates"])
    units = roll["totals"]["units"] or 1
    req = agg["requirement_mt"]
    per_unit_mt = req / units
    base_material = roll["totals"]["current_material_cost"] / units
    base_total = (roll["totals"]["base_cost"] + roll["totals"]["current_material_cost"]) / units

    rows = []
    for pct in steps:
        price = cur * (1 + pct / 100)
        mat_cost_unit = per_unit_mt * price
        delta_unit = per_unit_mt * (price - cur)
        rows.append({
            "scenario_pct": pct, "price": round(price, 2),
            "material_cost_this_commodity": round(mat_cost_unit, 2),
            "transformer_cost": round(base_total + delta_unit, 2),
            "transformer_delta": round(delta_unit, 2),
            "project_cost": round((base_total + delta_unit) * units, 2),
            "project_exposure": round((price - cur) * req, 2),
            "is_current": pct == 0,
        })
    return {
        "available": True, "commodity": commodity, "current_price": round(cur, 2),
        "consumption_mt_per_transformer": round(per_unit_mt, 4),
        "total_requirement_mt": round(req, 4), "units": units,
        "base_material_cost_per_transformer": round(base_material, 2),
        "base_total_cost_per_transformer": round(base_total, 2),
        "rows": rows,
    }


# --------------------------------------------------------------------------- §25.10 what-if
def what_if(db, commodity: str, scenario_price: float,
            consumption_mt: float | None = None, quantity: int | None = None,
            current_price: float | None = None) -> dict[str, Any]:
    """Interactive simulator. Any input may be overridden by the user."""
    roll = bom_rollup(db)
    agg = roll["by_commodity"].get(commodity, {})
    units = quantity if quantity is not None else (roll["totals"]["units"] or 1)
    cur = current_price if current_price is not None else _headline_price(db, commodity, roll["rates"])
    if consumption_mt is None:
        consumption_mt = (agg.get("requirement_mt", 0.0) /
                          max(roll["totals"]["units"] or 1, 1))

    delta = float(scenario_price) - float(cur)
    impact_unit = delta * consumption_mt
    impact_project = impact_unit * units

    base_total_unit = ((roll["totals"]["base_cost"] + roll["totals"]["current_material_cost"])
                       / max(roll["totals"]["units"] or 1, 1))
    return {
        "commodity": commodity,
        "current_price": round(float(cur), 2),
        "scenario_price": round(float(scenario_price), 2),
        "price_change": round(delta, 2),
        "price_change_pct": round(delta / cur * 100, 3) if cur else None,
        "consumption_mt": round(float(consumption_mt), 4),
        "quantity": units,
        "impact_per_transformer": round(impact_unit, 2),
        "total_project_impact": round(impact_project, 2),
        "reference_transformer_cost": round(base_total_unit, 2),
        "transformer_cost_increase_pct": round(impact_unit / base_total_unit * 100, 3)
        if base_total_unit else None,
        "project_cost_increase_pct": round(
            impact_project / (base_total_unit * units) * 100, 3) if base_total_unit and units else None,
        "formula": "Impact = (scenario price - current price) x BOM consumption x quantity",
    }


# --------------------------------------------------------------------------- §25.15 coverage
def coverage(db, commodity: str | None = None) -> list[dict]:
    """
    Uncovered quantity = requirement - stock - open PO - confirmed supply.
    Uncovered exposure is valued at the current market rate.
    """
    roll = bom_rollup(db)
    rates = roll["rates"]
    cmap = {c.id: c.code for c in db.scalars(select(Commodity))}
    cov_rows = {cmap.get(c.commodity_id): c for c in db.scalars(select(ProcurementCoverage))}

    out = []
    for ccode, agg in roll["by_commodity"].items():
        if commodity and ccode != commodity:
            continue
        req = agg["requirement_mt"]
        c = cov_rows.get(ccode)
        # A missing procurement_coverage row means coverage is NOT TRACKED for
        # this material - which is a different statement from "0% covered".
        # The flag lets the UI say so instead of implying total exposure.
        tracked = c is not None
        stock = float(c.stock_mt or 0) if c else 0.0
        po = float(c.open_po_mt or 0) if c else 0.0
        conf = float(c.confirmed_mt or 0) if c else 0.0
        covered = stock + po + conf
        uncovered = max(req - covered, 0.0)
        price = _headline_price(db, ccode, rates)
        out.append({
            "commodity": ccode,
            "tracked": tracked,
            "requirement_mt": round(req, 4),
            "stock_mt": round(stock, 4), "open_po_mt": round(po, 4),
            "confirmed_mt": round(conf, 4), "covered_mt": round(covered, 4),
            "uncovered_mt": round(uncovered, 4),
            "coverage_pct": (round(covered / req * 100, 2) if (req and tracked) else None),
            "current_price": round(price, 2),
            "requirement_value": round(req * price, 2),
            "uncovered_exposure": round(uncovered * price, 2) if tracked else None,
            "avg_po_rate": float(c.avg_po_rate_inr_mt) if c and c.avg_po_rate_inr_mt else None,
            "po_vs_market_pct": round(
                (price - float(c.avg_po_rate_inr_mt)) / float(c.avg_po_rate_inr_mt) * 100, 3)
            if c and c.avg_po_rate_inr_mt else None,
            "lead_time_days": c.supplier_lead_days if c else None,
        })
    order = {c: i for i, c in enumerate(CATEGORY_ORDER)}
    return sorted(out, key=lambda r: order.get(r["commodity"], 99))
