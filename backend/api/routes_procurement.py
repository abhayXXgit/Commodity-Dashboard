"""Decision support, price locking, alerts, executive views."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from backend.api.schemas import LockIn
from backend.database import get_db
from backend.models import Alert
from backend.services import alerts as AL
from backend.services import bom_engine as B
from backend.services import procurement as P
from backend.services import summary as S

router = APIRouter(prefix="/api/procurement", tags=["procurement"])


@router.get("/recommendation")
def recommendation(commodity: str = "CU", db=Depends(get_db)):
    out = P.recommend(db, commodity.upper())
    if not out.get("available"):
        raise HTTPException(404, out.get("message"))
    return out


@router.get("/recommendations")
def recommendations(db=Depends(get_db)):
    rows = []
    for ccode in B.HEADLINE:
        r = P.recommend(db, ccode)
        if r.get("available"):
            rows.append(r)
    return {"rows": rows, "disclaimer": P.DISCLAIMER}


@router.post("/price-lock")
def price_lock(payload: LockIn, db=Depends(get_db)):
    out = P.price_lock_analysis(db, payload.commodity.upper(), payload.quantity_mt,
                               payload.lock_period_days, payload.supplier_premium)
    if not out.get("available"):
        raise HTTPException(404, out.get("message"))
    return out


@router.get("/price-lock")
def price_lock_get(commodity: str = "CU", lock_period_days: int = 90,
                   supplier_premium: float = 0, db=Depends(get_db)):
    out = P.price_lock_analysis(db, commodity.upper(), None, lock_period_days, supplier_premium)
    if not out.get("available"):
        raise HTTPException(404, out.get("message"))
    return out


@router.get("/summary")
def summary(db=Depends(get_db)):
    return S.management_summary(db)


@router.get("/executive")
def executive(db=Depends(get_db)):
    return S.executive_view(db)


@router.get("/kpis")
def kpis(db=Depends(get_db)):
    return S.procurement_kpis(db)


@router.get("/alerts")
def alerts(limit: int = 60, include_ack: bool = False, db=Depends(get_db)):
    return {"alerts": AL.recent_alerts(db, limit, include_ack)}


@router.post("/alerts/evaluate")
def evaluate(db=Depends(get_db)):
    out = AL.evaluate_all(db)
    db.commit()
    return out


@router.post("/alerts/{alert_id}/ack")
def ack(alert_id: int, db=Depends(get_db)):
    import datetime as dt
    a = db.get(Alert, alert_id)
    if not a:
        raise HTTPException(404, "alert not found")
    a.acknowledged = True
    a.acknowledged_at = dt.datetime.now(dt.timezone.utc)
    db.commit()
    return {"ok": True, "id": alert_id}
