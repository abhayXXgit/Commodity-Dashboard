"""
Forecast persistence + caching.

Back-testing the full zoo takes seconds, which is fine for a nightly job and
far too slow for a dashboard request. So:
  * `refresh_forecasts()` runs the engine and writes rows into `forecasts`
    (called by the weekly retrain job and by the manual Refresh button);
  * `get_forecast()` serves the newest stored run, and only computes inline
    when nothing is stored yet or the stored run is older than `max_age_hours`.
"""
from __future__ import annotations

import datetime as dt
import json
import threading
import uuid
from typing import Any

from sqlalchemy import select

from backend.config import settings
from backend.database import session_scope
from backend.models import Forecast
from backend.services import analytics as A
from backend.forecasting.engine import forecast_series
from backend.utils.logging_config import get_logger

log = get_logger(__name__)

# series the system forecasts by default: (commodity, provider, product, label)
DEFAULT_SERIES = [
    ("CU", "BME", "CU_CATHODE", "Copper Cathode (India)"),
    ("CU", "LME", "CU_CATHODE", "LME Copper Cash"),
    ("AL", "NALCO", "AL_INGOT", "NALCO Aluminium Ingot"),
    ("AL", "NALCO", "AL_WIREROD", "NALCO Aluminium Wire Rod"),
    ("AL", "BALCO", "AL_INGOT", "BALCO Aluminium Ingot"),
    ("AL", "HINDALCO", "AL_INGOT", "Hindalco Aluminium Ingot"),
    ("CRGO", "MARKET", "CRGO_M4", "CRGO Lamination"),
    ("STEEL", "MARKET", "STEEL_MS", "MS Plate"),
    ("OIL", "MARKET", "OIL_TRF", "Transformer Oil"),
]

_leaderboard_cache: dict[str, Any] = {}
_lock = threading.Lock()

# The back-test leaderboard is what justifies the chosen model, so it has to
# survive a restart - an in-process dict would leave "why this model?" blank
# every time the service is redeployed. It is derived data, so a small JSON
# cache on disk is the right weight for it (no schema migration needed).
_CACHE_DIR = settings.DATA_DIR / "forecast_cache"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _key(c, pv, pr) -> str:
    return f"{c}|{pv or '*'}|{pr or '*'}"


def _cache_path(key: str):
    return _CACHE_DIR / (key.replace("|", "__").replace("*", "ALL") + ".json")


def _cache_put(key: str, payload: dict) -> None:
    with _lock:
        _leaderboard_cache[key] = payload
    try:
        _cache_path(key).write_text(json.dumps(payload, default=str), encoding="utf-8")
    except OSError as exc:                                  # non-fatal
        log.warning("could not persist leaderboard for %s: %s", key, exc)


def _cache_get(key: str) -> dict | None:
    with _lock:
        hit = _leaderboard_cache.get(key)
    if hit:
        return hit
    path = _cache_path(key)
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            with _lock:
                _leaderboard_cache[key] = payload
            return payload
        except (OSError, ValueError) as exc:
            log.warning("could not read leaderboard cache %s: %s", path, exc)
    return None


def refresh_forecasts(series=None, horizons=None, folds: int | None = None) -> dict[str, Any]:
    """Run the engine for each series and persist the winner per horizon."""
    series = series or DEFAULT_SERIES
    horizons = horizons or settings.FORECAST_HORIZONS
    run_id = uuid.uuid4().hex[:16]
    done, failed = [], []

    # One transaction PER SERIES. A nine-series run takes minutes; holding a
    # single write transaction open that long blocks readers on SQLite and
    # would discard every completed series if the last one failed.
    for ccode, pvcode, prcode, label in series:
        try:
            with session_scope() as db:
                ids = A.resolve_ids(db)
                s = A.daily_series(A.load_series(db, ccode, pvcode, prcode))
                out = forecast_series(s, horizons=horizons, folds=folds, run_id=run_id)
                if not out.get("available"):
                    failed.append({"series": label, "reason": out.get("message")})
                    continue
                for r in out["results"]:
                    db.add(Forecast(
                        run_id=run_id, commodity_id=ids["commodity"][ccode],
                        provider_id=ids["provider"].get(pvcode),
                        product_id=ids["product"].get(prcode),
                        horizon_days=r["horizon_days"],
                        target_date=dt.date.fromisoformat(r["target_date"]),
                        forecast_price=r["forecast_price"], lower_ci=r["lower_ci"],
                        upper_ci=r["upper_ci"], model_used=r["model_used"],
                        mae=r["mae"], rmse=r["rmse"], mape=r["mape"],
                        direction=r["direction"], confidence=r["confidence"],
                    ))
            _cache_put(_key(ccode, pvcode, prcode), {
                "run_id": run_id,
                "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "leaderboard": out["leaderboard"], "method": out["method"],
                "paths": {str(r["horizon_days"]): r["path"] for r in out["results"]},
            })
            done.append(label)
            log.info("forecast ok: %s", label)
        except Exception as exc:                            # noqa: BLE001
            log.exception("forecast failed for %s", label)
            failed.append({"series": label, "reason": f"{type(exc).__name__}: {exc}"})

    log.info("forecast run %s: %d ok, %d failed", run_id, len(done), len(failed))
    return {"run_id": run_id, "succeeded": done, "failed": failed,
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat()}


def stored_forecasts(db, ccode: str, pvcode: str | None = None,
                     prcode: str | None = None) -> list[dict]:
    """Newest persisted run for one series."""
    ids = A.resolve_ids(db)
    q = select(Forecast).where(Forecast.commodity_id == ids["commodity"].get(ccode))
    if pvcode:
        q = q.where(Forecast.provider_id == ids["provider"].get(pvcode))
    if prcode:
        q = q.where(Forecast.product_id == ids["product"].get(prcode))
    rows = list(db.scalars(q.order_by(Forecast.generated_at.desc(), Forecast.horizon_days)))
    if not rows:
        return []
    # Rows inside one run are inserted microseconds apart, so the run is
    # identified by run_id - never by an exact generated_at match.
    newest_run = rows[0].run_id
    out = []
    for r in rows:
        if r.run_id != newest_run:
            continue
        out.append({
            "horizon_days": r.horizon_days, "target_date": r.target_date.isoformat(),
            "forecast_price": float(r.forecast_price),
            "lower_ci": float(r.lower_ci) if r.lower_ci is not None else None,
            "upper_ci": float(r.upper_ci) if r.upper_ci is not None else None,
            "model_used": r.model_used,
            "mae": float(r.mae) if r.mae is not None else None,
            "rmse": float(r.rmse) if r.rmse is not None else None,
            "mape": float(r.mape) if r.mape is not None else None,
            "direction": r.direction, "confidence": r.confidence,
            "generated_at": r.generated_at.isoformat() if r.generated_at else None,
            "data_class": "FORECAST",
        })
    return sorted(out, key=lambda x: x["horizon_days"])


def get_forecast(db, ccode: str, pvcode: str | None = None, prcode: str | None = None,
                 max_age_hours: int = 168, compute_if_missing: bool = True) -> dict[str, Any]:
    """Cached read. Computes inline only when nothing usable is stored."""
    rows = stored_forecasts(db, ccode, pvcode, prcode)
    fresh = False
    if rows and rows[0].get("generated_at"):
        gen = dt.datetime.fromisoformat(rows[0]["generated_at"])
        if gen.tzinfo is None:
            gen = gen.replace(tzinfo=dt.timezone.utc)
        age_h = (dt.datetime.now(dt.timezone.utc) - gen).total_seconds() / 3600
        fresh = age_h <= max_age_hours
    if rows and fresh:
        cached = _cache_get(_key(ccode, pvcode, prcode)) or {}
        return {"available": True, "cached": True, "series": _key(ccode, pvcode, prcode),
                "results": rows,
                "leaderboard": cached.get("leaderboard"),
                "method": cached.get("method"),
                "paths": cached.get("paths")}

    if not compute_if_missing:
        return {"available": bool(rows), "cached": True, "stale": True, "results": rows}

    s = A.daily_series(A.load_series(db, ccode, pvcode, prcode))
    out = forecast_series(s)
    if not out.get("available"):
        return {"available": False, "message": out.get("message"), "results": []}
    _cache_put(_key(ccode, pvcode, prcode), {
        "run_id": out["run_id"],
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "leaderboard": out["leaderboard"], "method": out["method"],
        "paths": {str(r["horizon_days"]): r["path"] for r in out["results"]}})
    return {"available": True, "cached": False, "series": _key(ccode, pvcode, prcode),
            "results": out["results"], "leaderboard": out["leaderboard"],
            "paths": {str(r["horizon_days"]): r["path"] for r in out["results"]},
            "method": out["method"]}


def forecast_pct(db, ccode: str, pvcode: str | None = None, prcode: str | None = None,
                 horizon: int = 30) -> float | None:
    """Expected % change at `horizon` - used by the trend and decision engines."""
    fc = get_forecast(db, ccode, pvcode, prcode, compute_if_missing=False)
    for r in fc.get("results", []):
        if r["horizon_days"] == horizon:
            s = A.daily_series(A.load_series(db, ccode, pvcode, prcode))
            if s.empty:
                return None
            cur = float(s.iloc[-1])
            return (r["forecast_price"] - cur) / cur * 100.0
    return None


def forecast_price_at(db, ccode: str, pvcode: str | None = None, prcode: str | None = None,
                      horizon: int = 30) -> dict | None:
    fc = get_forecast(db, ccode, pvcode, prcode, compute_if_missing=False)
    for r in fc.get("results", []):
        if r["horizon_days"] == horizon:
            return r
    return None
