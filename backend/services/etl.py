"""
ETL orchestration (spec §19).

    SOURCE -> validate -> clean -> unit convert -> currency convert
           -> database -> analytics -> forecast -> dashboard

Every adapter run is logged to data_source_log with timing, row counts and the
error text. A failing source never takes the dashboard down: the last verified
data stays on screen and a STALE alert fires.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import select

from backend.config import settings
from backend.data_sources.base import (STATUS_SUCCESS,
                                       PriceRecord)
from backend.data_sources.circular_adapter import build_producer_adapters
from backend.data_sources.fx_adapter import FxAdapter
from backend.data_sources.lme_adapter import LMEAdapter, MetalsFeedAdapter
from backend.database import session_scope
from backend.models import (DataQualityFlag, DataSourceLog, ExchangeRate,
                            HistoricalPrice, LivePrice)
from backend.services import analytics as A
from backend.utils.logging_config import get_logger
from backend.utils.cache import invalidate as invalidate_cache
from backend.utils.units import normalise_price
from backend.utils.validation import validate_row

log = get_logger(__name__)


def _log_run(db, source_name, adapter, status, started, **kw) -> DataSourceLog:
    last_ok = db.scalar(select(DataSourceLog)
                        .where(DataSourceLog.adapter == adapter,
                               DataSourceLog.status == STATUS_SUCCESS)
                        .order_by(DataSourceLog.run_started.desc()).limit(1))
    row = DataSourceLog(source_name=source_name, adapter=adapter, status=status,
                        run_started=started,
                        run_finished=dt.datetime.now(dt.timezone.utc),
                        last_success_at=last_ok.run_started if last_ok else None,
                        **kw)
    db.add(row)
    return row


def _persist(db, records: list[PriceRecord], ids: dict) -> tuple[int, int, int]:
    """Validate, normalise and insert. Returns (inserted, rejected, duplicates)."""
    inserted = rejected = dupes = 0
    for r in records:
        ok, issues = validate_row({"price": r.price, "currency": r.currency,
                                   "unit": r.unit, "price_date": r.price_date})
        if not ok:
            rejected += 1
            for i in issues:
                db.add(DataQualityFlag(table_name="historical_prices",
                                       flag_type=i.flag_type, detail=i.detail,
                                       severity="ERROR"))
            continue
        cid = ids["commodity"].get(r.commodity_code)
        pid = ids["provider"].get(r.provider_code)
        prid = ids["product"].get(r.product_code) if r.product_code else None
        if not cid or not pid:
            rejected += 1
            continue
        exists = db.scalar(select(HistoricalPrice).where(
            HistoricalPrice.price_date == r.price_date,
            HistoricalPrice.commodity_id == cid,
            HistoricalPrice.provider_id == pid,
            HistoricalPrice.product_id == prid)
            .order_by(HistoricalPrice.revision.desc()).limit(1))
        if exists:
            # Same precedence rule as the manual importer: a live or verified
            # observation replaces a demo/estimated one for the same slot.
            from backend.services.excel_io import TRUST
            if TRUST.get(r.data_class, 0) > TRUST.get(str(exists.data_class), 0):
                db.delete(exists)
                db.flush()
            else:
                dupes += 1
                continue
        fx = None
        if r.currency.upper() != "INR":
            fx, _, _ = A.latest_fx(db, r.currency.upper())
        db.add(HistoricalPrice(
            price_date=r.price_date, commodity_id=cid, provider_id=pid, product_id=prid,
            grade=r.grade, price=r.price, currency=r.currency, unit=r.unit,
            open_price=r.open_price, high_price=r.high_price, low_price=r.low_price,
            close_price=r.close_price, price_basis=r.price_basis, location=r.location,
            premium=r.premium, freight=r.freight, tax_pct=r.tax_pct,
            price_inr_mt=round(normalise_price(r.price, r.currency, r.unit, fx), 4),
            fx_rate_used=fx, effective_date=r.effective_date or r.price_date,
            source=r.source, source_url=r.source_url, data_class=r.data_class,
            validation_status="PASS"))
        inserted += 1
    return inserted, rejected, dupes


def refresh_fx() -> dict[str, Any]:
    started = dt.datetime.now(dt.timezone.utc)
    adapter = FxAdapter()
    quotes, status, err = adapter.fetch_rates()
    with session_scope() as db:
        n = 0
        for q in quotes:
            exists = db.scalar(select(ExchangeRate).where(
                ExchangeRate.rate_date == q.rate_date,
                ExchangeRate.base_currency == q.base,
                ExchangeRate.quote_currency == q.quote).limit(1))
            if exists:
                exists.rate = q.rate
                exists.source = q.source
                exists.data_class = q.data_class
            else:
                db.add(ExchangeRate(rate_date=q.rate_date, base_currency=q.base,
                                    quote_currency=q.quote, rate=q.rate,
                                    source=q.source, data_class=q.data_class))
                n += 1
        _log_run(db, adapter.source_name, adapter.name, status, started,
                 rows_fetched=len(quotes), rows_inserted=n, error_message=err)
    return {"status": status, "rates": [{"pair": f"{q.base}{q.quote}", "rate": q.rate,
                                         "class": q.data_class} for q in quotes],
            "error": err}


def refresh_prices() -> dict[str, Any]:
    """Run every configured price adapter."""
    results = []
    adapters = [LMEAdapter(), MetalsFeedAdapter(), *build_producer_adapters()]
    for a in adapters:
        started = dt.datetime.now(dt.timezone.utc)
        res = a.fetch()
        with session_scope() as db:
            ids = A.resolve_ids(db)
            ins = rej = dup = 0
            if res.ok and res.records:
                ins, rej, dup = _persist(db, res.records, ids)
            _log_run(db, res.source_name, res.adapter, res.status, started,
                     rows_fetched=len(res.records), rows_inserted=ins,
                     rows_rejected=rej, duplicates_found=dup,
                     error_message=res.error)
        results.append({"adapter": a.name, "source": a.source_name,
                        "status": res.status, "fetched": len(res.records),
                        "inserted": ins, "rejected": rej, "duplicates": dup,
                        "error": res.error, "elapsed_ms": res.elapsed_ms})
    return {"sources": results,
            "any_success": any(r["status"] == STATUS_SUCCESS for r in results)}


def rebuild_live_snapshot() -> int:
    """
    Derive live_prices from the newest stored observation per series, marking
    confidence honestly: LIVE only if it really came from a live adapter today.
    """
    from backend.models import Commodity, Product, Provider
    from backend.utils.validation import is_stale

    with session_scope() as db:
        n = 0
        cmap = {c.code: c.id for c in db.scalars(select(Commodity))}
        pvmap = {p.code: p.id for p in db.scalars(select(Provider))}
        prmap = {p.code: p.id for p in db.scalars(select(Product))}
        for ccode, cid in cmap.items():
            for pvcode, pvid in pvmap.items():
                for prcode, prid in prmap.items():
                    df = A.load_series(db, ccode, pvcode, prcode)
                    if df.empty:
                        continue
                    s = A.daily_series(df)
                    last = df.iloc[-1]
                    cur, prev = float(s.iloc[-1]), (float(s.iloc[-2]) if len(s) > 1 else None)
                    ts = df["date"].max().to_pydatetime().replace(tzinfo=dt.timezone.utc)
                    stale, why = is_stale(ts, settings.STALE_AFTER_HOURS * 24)
                    dclass = str(last["data_class"])
                    conf = ("HIGH" if dclass == "LIVE" and not stale
                            else "LOW" if dclass == "DEMO_DATA"
                            else "MEDIUM")
                    row = db.scalar(select(LivePrice).where(
                        LivePrice.commodity_id == cid, LivePrice.provider_id == pvid,
                        LivePrice.product_id == prid).limit(1))
                    vals = dict(
                        latest_price=float(last["src_price"]), previous_price=prev,
                        currency=str(last["currency"]), unit=str(last["unit"]),
                        price_inr_mt=cur,
                        day_change=(cur - prev) if prev else None,
                        day_change_pct=((cur - prev) / prev * 100) if prev else None,
                        week_change_pct=A.pct_change_over(s, 7),
                        month_change_pct=A.pct_change_over(s, 30),
                        ytd_change_pct=A.pct_change_over(
                            s, (dt.date.today() - dt.date(dt.date.today().year, 1, 1)).days or 1),
                        source=str(last["source"]), data_class=dclass,
                        confidence=("STALE" if stale else conf),
                        is_stale=stale, stale_reason=why or None,
                        last_updated=ts)
                    if row:
                        for k, v in vals.items():
                            setattr(row, k, v)
                    else:
                        db.add(LivePrice(commodity_id=cid, provider_id=pvid,
                                         product_id=prid, **vals))
                    n += 1
        return n


def daily_job() -> dict[str, Any]:
    """Fetch -> validate -> store -> snapshot -> quotes -> alerts."""
    from backend.services import alerts as AL
    from backend.services import quotes as Q

    log.info("daily job starting")
    fx = refresh_fx()
    prices = refresh_prices()
    live = rebuild_live_snapshot()
    with session_scope() as db:
        nq = Q.recalculate_all(db)
        al = AL.evaluate_all(db)
    invalidate_cache("daily job")
    out = {"fx": fx, "prices": prices, "live_rows": live,
           "quotes_recalculated": nq, "alerts": al,
           "finished_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    log.info("daily job done: %s alerts, %s live rows", al["count"], live)
    return out


def weekly_job() -> dict[str, Any]:
    from backend.services.forecast_service import refresh_forecasts
    log.info("weekly retrain starting")
    fc = refresh_forecasts()
    with session_scope() as db:
        from backend.services import alerts as AL
        al = AL.evaluate_all(db)
    return {"forecasts": fc, "alerts": al}


def monthly_job() -> dict[str, Any]:
    from backend.services.reports import generate_monthly_report
    return generate_monthly_report()
