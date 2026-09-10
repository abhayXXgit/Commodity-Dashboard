"""
Supplier quotation module (spec §12).

Two different numbers, never conflated:

  * BASIC PRICE  - the supplier's ex-plant rate, normalised to INR/MT.
                   This is what gets benchmarked against the market, because
                   the published market reference is itself an ex-plant rate.
  * LANDED COST  - basic + premium + freight, grossed up by tax.
                   This is what procurement actually pays and what ranks
                   suppliers against each other.

Comparing a landed cost to an ex-plant benchmark overstates every supplier by
the tax rate, so the benchmark always uses the basic price and always uses the
SAME PRODUCT (a wire-rod quote is never scored against an ingot reference).
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import select

from backend.models import Commodity, Product, SupplierQuote
from backend.services import analytics as A
from backend.utils.units import landed_cost, normalise_price

STATUS_BEST = "BEST PRICE"
STATUS_ABOVE = "ABOVE MARKET"
STATUS_BELOW = "BELOW MARKET"
STATUS_EXPIRED = "EXPIRED QUOTE"


def _benchmark(db, ccode: str, prcode: str | None) -> tuple[float | None, str]:
    """Market ex-plant reference for the quote's own product."""
    if prcode:
        s = A.daily_series(A.load_series(db, ccode, product=prcode, currency_filter="INR"))
        if not s.empty:
            return float(s.iloc[-1]), f"{prcode} market average (INR, ex-plant)"
    from backend.services.bom_engine import headline_of
    pv, pr = headline_of(ccode)
    s = A.daily_series(A.load_series(db, ccode, pv, pr, currency_filter="INR"))
    if not s.empty:
        return float(s.iloc[-1]), f"{ccode} headline ({pv}/{pr})"
    return None, "no benchmark available"


def recalculate(db, quote: SupplierQuote) -> SupplierQuote:
    """Recompute every derived field on one quote."""
    ccode = db.get(Commodity, quote.commodity_id).code
    prcode = db.get(Product, quote.product_id).code if quote.product_id else None

    fx = None
    if (quote.currency or "INR").upper() != "INR":
        fx, _, _ = A.latest_fx(db, quote.currency.upper())
    basic = normalise_price(float(quote.price), quote.currency or "INR",
                            quote.unit or "MT", fx)

    quote.effective_price_inr_mt = round(basic, 4)
    quote.landed_cost_inr_mt = round(landed_cost(
        basic, float(quote.premium or 0), float(quote.freight or 0),
        float(quote.tax_pct or 0)), 4)

    bench, _src = _benchmark(db, ccode, prcode)
    if bench:
        quote.variance_vs_market = round(basic - bench, 4)
        quote.variance_pct = round((basic - bench) / bench * 100, 4)
    else:
        quote.variance_vs_market = quote.variance_pct = None

    prev = db.scalar(
        select(SupplierQuote)
        .where(SupplierQuote.supplier_name == quote.supplier_name,
               SupplierQuote.commodity_id == quote.commodity_id,
               SupplierQuote.product_id == quote.product_id,
               SupplierQuote.quote_date < quote.quote_date)
        .order_by(SupplierQuote.quote_date.desc()).limit(1))
    quote.variance_vs_prev = (round(basic - float(prev.effective_price_inr_mt), 4)
                              if prev and prev.effective_price_inr_mt else None)

    today = dt.date.today()
    if quote.valid_until and quote.valid_until < today:
        quote.status_flag = STATUS_EXPIRED
    elif quote.variance_pct is None:
        quote.status_flag = None
    elif quote.variance_pct > 0:
        quote.status_flag = STATUS_ABOVE
    else:
        quote.status_flag = STATUS_BELOW
    return quote


def recalculate_all(db) -> int:
    quotes = list(db.scalars(select(SupplierQuote)))
    for q in quotes:
        recalculate(db, q)
    db.flush()
    _mark_best(db, quotes)
    db.flush()
    return len(quotes)


def _mark_best(db, quotes) -> None:
    """Cheapest LANDED cost per (commodity, product) among live quotes wins."""
    groups: dict[tuple, list] = {}
    for q in quotes:
        if q.status_flag == STATUS_EXPIRED:
            continue
        groups.setdefault((q.commodity_id, q.product_id), []).append(q)
    for _, items in groups.items():
        best = min(items, key=lambda x: float(x.landed_cost_inr_mt or 1e18))
        best.status_flag = STATUS_BEST


def list_quotes(db, commodity: str | None = None,
                include_expired: bool = True) -> list[dict]:
    cmap = {c.id: c.code for c in db.scalars(select(Commodity))}
    cname = {c.id: c.name for c in db.scalars(select(Commodity))}
    pmap = {p.id: p.code for p in db.scalars(select(Product))}
    today = dt.date.today()
    out = []
    for q in db.scalars(select(SupplierQuote).order_by(SupplierQuote.quote_date.desc())):
        ccode = cmap.get(q.commodity_id)
        if commodity and ccode != commodity:
            continue
        expired = bool(q.valid_until and q.valid_until < today)
        if expired and not include_expired:
            continue
        bench, bench_src = _benchmark(db, ccode, pmap.get(q.product_id))
        out.append({
            "id": q.id, "quote_ref": q.quote_ref, "supplier": q.supplier_name,
            "commodity": ccode, "commodity_name": cname.get(q.commodity_id),
            "product": pmap.get(q.product_id),
            "quote_date": q.quote_date.isoformat() if q.quote_date else None,
            "valid_until": q.valid_until.isoformat() if q.valid_until else None,
            "days_to_expiry": (q.valid_until - today).days if q.valid_until else None,
            "expired": expired,
            "price": float(q.price), "currency": q.currency, "unit": q.unit,
            "freight": float(q.freight or 0), "premium": float(q.premium or 0),
            "tax_pct": float(q.tax_pct or 0),
            "effective_price": float(q.effective_price_inr_mt) if q.effective_price_inr_mt else None,
            "landed_cost": float(q.landed_cost_inr_mt) if q.landed_cost_inr_mt else None,
            "benchmark": round(bench, 2) if bench else None,
            "benchmark_source": bench_src,
            "variance_vs_market": float(q.variance_vs_market) if q.variance_vs_market is not None else None,
            "variance_pct": float(q.variance_pct) if q.variance_pct is not None else None,
            "variance_vs_prev": float(q.variance_vs_prev) if q.variance_vs_prev is not None else None,
            "status_flag": q.status_flag,
            "moq_mt": float(q.moq_mt) if q.moq_mt else None,
            "payment_terms": q.payment_terms, "delivery_days": q.delivery_days,
            "price_basis": q.price_basis, "remarks": q.remarks,
            "data_class": q.data_class,
        })
    return out


def create_quote(db, payload: dict[str, Any]) -> dict:
    ids = A.resolve_ids(db)
    ccode = payload["commodity"]
    q = SupplierQuote(
        quote_ref=payload.get("quote_ref"),
        supplier_name=payload["supplier"],
        commodity_id=ids["commodity"][ccode],
        product_id=ids["product"].get(payload.get("product")),
        quote_date=dt.date.fromisoformat(payload.get("quote_date") or dt.date.today().isoformat()),
        valid_until=dt.date.fromisoformat(payload["valid_until"]) if payload.get("valid_until") else None,
        price=float(payload["price"]),
        currency=(payload.get("currency") or "INR").upper(),
        unit=(payload.get("unit") or "MT").upper(),
        freight=float(payload.get("freight") or 0),
        premium=float(payload.get("premium") or 0),
        tax_pct=float(payload.get("tax_pct") or 0),
        moq_mt=float(payload["moq_mt"]) if payload.get("moq_mt") else None,
        payment_terms=payload.get("payment_terms"),
        delivery_days=int(payload["delivery_days"]) if payload.get("delivery_days") else None,
        price_basis=payload.get("price_basis") or "EX_PLANT",
        remarks=payload.get("remarks"),
        data_class="SUPPLIER_QUOTE",
    )
    db.add(q)
    db.flush()
    recalculate(db, q)
    recalculate_all(db)
    db.flush()
    return list_quotes(db)[0] if not list_quotes(db) else next(
        (x for x in list_quotes(db) if x["id"] == q.id), None)
