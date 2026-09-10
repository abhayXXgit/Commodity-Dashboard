"""Supplier quotation module."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from backend.api.schemas import QuoteIn
from backend.database import get_db
from backend.models import SupplierQuote
from backend.services import quotes as Q
from backend.utils.cache import invalidate as invalidate_cache

router = APIRouter(prefix="/api/quotes", tags=["quotes"])


@router.get("")
def list_quotes(commodity: str | None = None, include_expired: bool = True,
                db=Depends(get_db)):
    return {"quotes": Q.list_quotes(db, commodity.upper() if commodity else None,
                                    include_expired)}


@router.post("")
def create(payload: QuoteIn, db=Depends(get_db)):
    from backend.services import analytics as A
    ids = A.resolve_ids(db)
    if payload.commodity not in ids["commodity"]:
        raise HTTPException(400, f"unknown commodity {payload.commodity}")
    if payload.product and payload.product.upper() not in ids["product"]:
        raise HTTPException(400, f"unknown product {payload.product}")
    data = payload.model_dump()
    if data.get("product"):
        data["product"] = data["product"].upper()
    out = Q.create_quote(db, data)
    db.commit()
    invalidate_cache("quote created")
    return {"ok": True, "quote": out}


@router.post("/recalculate")
def recalculate(db=Depends(get_db)):
    n = Q.recalculate_all(db)
    db.commit()
    return {"ok": True, "recalculated": n}


@router.delete("/{quote_id}")
def delete(quote_id: int, db=Depends(get_db)):
    q = db.get(SupplierQuote, quote_id)
    if not q:
        raise HTTPException(404, "quote not found")
    db.delete(q)
    db.commit()
    Q.recalculate_all(db)
    db.commit()
    return {"ok": True, "deleted": quote_id}
