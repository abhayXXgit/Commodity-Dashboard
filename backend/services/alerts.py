"""
Alert engine (spec §11).

Rules live in `alert_rules` so thresholds are configurable without a code
change. Evaluation is idempotent per day: re-running the daily job does not
duplicate an alert that already fired for the same rule and commodity today.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import select

from backend.database import db_since
from backend.models import (Alert, AlertRule, Commodity, DataSourceLog,
                            LivePrice, SupplierQuote)
from backend.services import analytics as A
from backend.services import bom_engine as B
from backend.services import forecast_service as F
from backend.utils.logging_config import get_logger
from backend.utils.validation import is_stale

log = get_logger(__name__)


# De-duplication window. A rolling window rather than a calendar day, because
# "today" is ambiguous when the app runs in IST and stores timestamps in UTC -
# a calendar-day check silently fails either side of midnight. 20 hours lets a
# once-a-day job re-fire on schedule while a manual re-run minutes later does
# not duplicate anything.
DEDUP_WINDOW_HOURS = 20


def _already_fired(db, code: str, commodity_id: int | None, day=None) -> bool:
    cutoff = db_since(DEDUP_WINDOW_HOURS)
    q = select(Alert).where(Alert.alert_code == code, Alert.triggered_at >= cutoff)
    if commodity_id is not None:
        q = q.where(Alert.commodity_id == commodity_id)
    else:
        q = q.where(Alert.commodity_id.is_(None))
    return db.scalar(q.limit(1)) is not None


def _fire(db, code, severity, title, message, commodity_id=None,
          metric=None, threshold=None, day=None) -> bool:
    if _already_fired(db, code, commodity_id):
        return False
    db.add(Alert(alert_code=code, severity=severity, commodity_id=commodity_id,
                 title=title, message=message, metric_value=metric,
                 threshold_value=threshold))
    # Flush so the next _already_fired inside this same evaluation sees it -
    # the session runs with autoflush disabled, so without this a single pass
    # could emit the same alert twice for two rules that share a code.
    db.flush()
    return True


def evaluate_all(db) -> dict[str, Any]:
    """Run every enabled rule. Returns what fired."""
    fired: list[dict] = []
    rules = list(db.scalars(select(AlertRule).where(AlertRule.is_enabled.is_(True))))
    commodities = {c.code: c for c in db.scalars(select(Commodity))}

    for rule in rules:
        codes = [rule.commodity_code] if rule.commodity_code else list(B.HEADLINE)
        for ccode in codes:
            c = commodities.get(ccode)
            if not c:
                continue
            pv, pr = B.headline_of(ccode)
            s = A.daily_series(A.load_series(db, ccode, pv, pr))
            thr = float(rule.threshold)

            if rule.rule_type == "DAY_MOVE" and len(s) >= 2:
                move = (float(s.iloc[-1]) - float(s.iloc[-2])) / float(s.iloc[-2]) * 100
                hit = (move >= thr if rule.direction == "UP"
                       else move <= -thr if rule.direction == "DOWN"
                       else abs(move) >= thr)
                if hit and _fire(db, rule.alert_code, rule.severity,
                                 f"{c.name} moved {move:+.2f}% in one day",
                                 f"{c.name} ({pv}/{pr}) closed at {float(s.iloc[-1]):,.0f} INR/MT, "
                                 f"a {move:+.2f}% move against a {thr:.1f}% threshold.",
                                 c.id, round(move, 4), thr):
                    fired.append({"code": rule.alert_code, "commodity": ccode, "value": move})

            elif rule.rule_type == "PERCENTILE" and not s.empty:
                band = A.price_band(s)
                p = band.get("percentile")
                if p is None:
                    continue
                hit = p >= thr if rule.direction == "UP" else p <= thr
                if hit and _fire(db, rule.alert_code, rule.severity,
                                 f"{c.name} at the {p:.0f}th percentile ({band['zone']})",
                                 f"{c.name} is {float(s.iloc[-1]):,.0f} INR/MT, the {p:.0f}th "
                                 f"percentile of its 3-year range "
                                 f"({band['low']:,.0f} - {band['high']:,.0f}).",
                                 c.id, round(p, 2), thr):
                    fired.append({"code": rule.alert_code, "commodity": ccode, "value": p})

            elif rule.rule_type == "FORECAST_MOVE":
                fp = F.forecast_pct(db, ccode, pv, pr, 30)
                if fp is None:
                    continue
                hit = fp >= thr if rule.direction == "UP" else fp <= -thr
                if hit:
                    cov = next((x for x in B.coverage(db, ccode)), None)
                    exposure = ""
                    if cov and cov["uncovered_mt"]:
                        val = cov["uncovered_mt"] * float(s.iloc[-1]) * fp / 100
                        exposure = (f" On {cov['uncovered_mt']:.2f} MT uncovered that is "
                                    f"{val/1e5:+,.1f} Lakh.")
                    if _fire(db, rule.alert_code, rule.severity,
                             f"{c.name} 30-day forecast {fp:+.2f}%",
                             f"The selected model projects {c.name} to move {fp:+.2f}% "
                             f"over 30 days against a {thr:.1f}% threshold.{exposure}",
                             c.id, round(fp, 4), thr):
                        fired.append({"code": rule.alert_code, "commodity": ccode, "value": fp})

            elif rule.rule_type == "SUPPLIER_SPREAD":
                # Benchmark each quote against ITS OWN product (an aluminium
                # wire-rod quote against the ingot reference would read ~6% high
                # purely because of the conversion charge), and compare basic
                # ex-plant to basic ex-plant - never landed cost to ex-plant.
                from backend.services.quotes import list_quotes
                for q in list_quotes(db, commodity=ccode, include_expired=False):
                    var = q.get("variance_pct")
                    if var is None or abs(var) < thr:
                        continue
                    if _fire(db, rule.alert_code, rule.severity,
                             f"{q['supplier']} {q['product'] or ccode} quote is "
                             f"{var:+.2f}% vs benchmark",
                             f"{q['supplier']} quoted {q['effective_price']:,.0f} INR/MT "
                             f"(ex-plant) for {q['product'] or c.name} against a "
                             f"{q['benchmark']:,.0f} benchmark - {q['benchmark_source']}. "
                             f"Landed cost is {q['landed_cost']:,.0f} INR/MT.",
                             c.id, round(var, 4), thr):
                        fired.append({"code": rule.alert_code, "commodity": ccode,
                                      "supplier": q["supplier"], "value": var})

        if rule.rule_type == "STALE":
            for lp in db.scalars(select(LivePrice)):
                stale, why = is_stale(lp.last_updated, int(thr))
                if stale and _fire(db, rule.alert_code, rule.severity,
                                   "Live data unavailable - last verified price shown",
                                   f"Live feed for commodity id {lp.commodity_id}: {why}. "
                                   f"The dashboard is showing the last verified price.",
                                   lp.commodity_id, None, thr):
                    fired.append({"code": rule.alert_code, "reason": why})
            last_ok = db.scalar(select(DataSourceLog)
                                .where(DataSourceLog.status == "SUCCESS")
                                .order_by(DataSourceLog.run_started.desc()).limit(1))
            if last_ok is None and _fire(
                    db, "NO_LIVE_SOURCE", "WARNING",
                    "No live source has ever succeeded",
                    "No external price source is configured or reachable. The dashboard "
                    "is running on stored data only; live values are not being claimed.",
                    None, None, thr):
                fired.append({"code": "NO_LIVE_SOURCE"})

        elif rule.rule_type == "QUOTE_EXPIRY":
            today = dt.date.today()
            for q in db.scalars(select(SupplierQuote)):
                if not q.valid_until:
                    continue
                days = (q.valid_until - today).days
                if days < 0:
                    title = f"{q.supplier_name} quotation expired {abs(days)} day(s) ago"
                    sev = "WARNING"
                elif days <= thr:
                    title = f"{q.supplier_name} quotation expires in {days} day(s)"
                    sev = "INFO"
                else:
                    continue
                if _fire(db, rule.alert_code, sev, title,
                         f"Quote {q.quote_ref or q.id} for {q.price:,.0f} "
                         f"{q.currency}/{q.unit} valid until {q.valid_until}.",
                         q.commodity_id, float(days), thr):
                    fired.append({"code": rule.alert_code, "supplier": q.supplier_name,
                                  "days": days})

    db.flush()
    log.info("alert evaluation: %d new alert(s)", len(fired))
    return {"fired": fired, "count": len(fired),
            "evaluated_rules": len(rules),
            "evaluated_at": dt.datetime.now(dt.timezone.utc).isoformat()}


def recent_alerts(db, limit: int = 60, include_ack: bool = False) -> list[dict]:
    q = select(Alert).order_by(Alert.triggered_at.desc()).limit(limit)
    if not include_ack:
        q = select(Alert).where(Alert.acknowledged.is_(False)) \
                         .order_by(Alert.triggered_at.desc()).limit(limit)
    cmap = {c.id: c.code for c in db.scalars(select(Commodity))}
    out = []
    for a in db.scalars(q):
        out.append({
            "id": a.id, "code": a.alert_code, "severity": a.severity,
            "commodity": cmap.get(a.commodity_id), "title": a.title,
            "message": a.message,
            "metric_value": float(a.metric_value) if a.metric_value is not None else None,
            "threshold": float(a.threshold_value) if a.threshold_value is not None else None,
            "triggered_at": a.triggered_at.isoformat() if a.triggered_at else None,
            "acknowledged": a.acknowledged,
        })
    return out
