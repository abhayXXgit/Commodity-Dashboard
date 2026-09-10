"""Transformer BOM, exposure, sensitivity, what-if and coverage endpoints."""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select

from backend.api.schemas import BomItemIn, ImpactIn, TransformerIn, WhatIfIn
from backend.database import get_db
from backend.models import BomItem, Transformer
from backend.services import analytics as A
from backend.services import bom_engine as B
from backend.services import procurement as P
from backend.utils.cache import invalidate as invalidate_cache

router = APIRouter(prefix="/api/bom", tags=["bom"])


@router.get("/rollup")
def rollup(jobs: str | None = Query(None, description="comma separated job numbers"),
           db=Depends(get_db)):
    return B.bom_rollup(db, jobs.split(",") if jobs else None)


@router.get("/transformers")
def transformers(db=Depends(get_db)):
    roll = B.bom_rollup(db)
    return {"transformers": [{k: v for k, v in t.items() if k != "items"}
                             for t in roll["transformers"]],
            "totals": roll["totals"]}


@router.get("/transformers/{job_no}")
def transformer_detail(job_no: str, db=Depends(get_db)):
    roll = B.bom_rollup(db, [job_no])
    if not roll["transformers"]:
        raise HTTPException(404, f"job {job_no} not found")
    return roll["transformers"][0]


@router.post("/transformers")
def create_transformer(payload: TransformerIn, db=Depends(get_db)):
    if db.scalar(select(Transformer).where(Transformer.job_no == payload.job_no)):
        raise HTTPException(409, f"job {payload.job_no} already exists")
    t = Transformer(
        job_no=payload.job_no, customer=payload.customer,
        transformer_type=payload.transformer_type, rating_mva=payload.rating_mva,
        voltage_ratio=payload.voltage_ratio, quantity=payload.quantity,
        delivery_date=dt.date.fromisoformat(payload.delivery_date) if payload.delivery_date else None,
        mfg_status=payload.mfg_status, base_cost_inr=payload.base_cost_inr,
        costing_version="CV-manual", bom_version="BOM-1.0")
    db.add(t)
    db.commit()
    invalidate_cache("bom write")
    return {"ok": True, "job_no": t.job_no, "id": t.id}


@router.post("/transformers/{job_no}/items")
def add_bom_item(job_no: str, payload: BomItemIn, db=Depends(get_db)):
    t = db.scalar(select(Transformer).where(Transformer.job_no == job_no))
    if not t:
        raise HTTPException(404, f"job {job_no} not found")
    ids = A.resolve_ids(db)
    from backend.utils.units import unit_factor
    try:
        mt = float(payload.quantity) / unit_factor(payload.unit)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    item = BomItem(
        transformer_id=t.id, bom_item=payload.bom_item,
        material_category=payload.material_category.upper(),
        material=payload.bom_item, grade=payload.grade,
        quantity=payload.quantity, unit=payload.unit.upper(),
        total_weight_mt=mt,
        commodity_id=ids["commodity"].get((payload.commodity or "").upper()),
        provider_id=ids["provider"].get((payload.provider or "").upper()),
        product_id=ids["product"].get((payload.product or "").upper()),
        bom_rate_inr_mt=payload.bom_rate_inr_mt, supplier=payload.supplier,
        price_basis="EX_PLANT", currency="INR")
    db.add(item)
    db.commit()
    invalidate_cache("bom write")
    return {"ok": True, "id": item.id, "total_weight_mt": round(mt, 6)}


@router.delete("/transformers/{job_no}")
def delete_transformer(job_no: str, db=Depends(get_db)):
    t = db.scalar(select(Transformer).where(Transformer.job_no == job_no))
    if not t:
        raise HTTPException(404, f"job {job_no} not found")
    db.delete(t)
    db.commit()
    invalidate_cache("bom write")
    return {"ok": True, "deleted": job_no}


@router.post("/impact")
def impact(payload: ImpactIn | None = None, db=Depends(get_db)):
    payload = payload or ImpactIn()
    return B.price_impact(db, payload.changes, payload.jobs)


@router.get("/impact")
def impact_get(db=Depends(get_db)):
    return B.price_impact(db)


@router.get("/waterfall")
def waterfall(job_no: str | None = None, db=Depends(get_db)):
    out = B.cost_waterfall(db, job_no)
    if not out.get("available"):
        raise HTTPException(404, out.get("message"))
    return out


@router.get("/sensitivity")
def sensitivity(commodity: str = "CU", jobs: str | None = None, db=Depends(get_db)):
    out = B.sensitivity(db, commodity.upper(), jobs.split(",") if jobs else None)
    if not out.get("available"):
        raise HTTPException(404, out.get("message"))
    return out


@router.post("/what-if")
def what_if(payload: WhatIfIn, db=Depends(get_db)):
    return B.what_if(db, payload.commodity, payload.scenario_price,
                     payload.consumption_mt, payload.quantity, payload.current_price)


@router.get("/coverage")
def coverage(commodity: str | None = None, db=Depends(get_db)):
    return {"rows": B.coverage(db, commodity.upper() if commodity else None)}


@router.get("/exposure/monthly")
def monthly(db=Depends(get_db)):
    return P.monthly_exposure(db)


@router.get("/exposure/matrix")
def matrix(db=Depends(get_db)):
    return P.priority_matrix(db)


@router.get("/backtest")
def backtest(job_no: str | None = None, db=Depends(get_db)):
    out = P.historical_cost_backtest(db, job_no)
    if not out.get("available"):
        raise HTTPException(404, out.get("message"))
    return out


@router.get("/flow")
def flow(commodity: str = "CU", job_no: str | None = None, db=Depends(get_db)):
    """Commodity -> BOM -> project flow (spec §25.14), computed end to end."""
    ccode = commodity.upper()
    roll = B.bom_rollup(db, [job_no] if job_no else None)
    agg = roll["by_commodity"].get(ccode)
    if not agg:
        raise HTTPException(404, f"no BOM consumption for {ccode}")
    pv, pr = B.headline_of(ccode)
    price = B._headline_price(db, ccode, roll["rates"])
    units = roll["totals"]["units"] or 1
    per_unit_mt = agg["requirement_mt"] / units
    from backend.services import forecast_service as F
    fc = F.forecast_price_at(db, ccode, pv, pr, 30)
    fprice = fc["forecast_price"] if fc else price
    reco = P.recommend(db, ccode)
    return {
        "commodity": ccode, "provider": pv, "product": pr,
        "price": round(price, 2),
        "consumption_mt_per_transformer": round(per_unit_mt, 4),
        "material_cost_per_transformer": round(per_unit_mt * price, 2),
        "quantity": units,
        "total_exposure": round(agg["requirement_mt"] * price, 2),
        "forecast_price": round(fprice, 2),
        "forecast_horizon_days": 30,
        "forecast_model": fc["model_used"] if fc else None,
        "forecast_exposure": round(agg["requirement_mt"] * fprice, 2),
        "potential_exposure": round(agg["requirement_mt"] * (fprice - price), 2),
        "recommendation": reco.get("signal"),
        "recommendation_reasons": reco.get("reasons", [])[:3],
    }
