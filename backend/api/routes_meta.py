"""Health, config disclosure (safe subset only) and scheduler status."""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from backend.config import settings
from backend.database import get_db
from backend.forecasting.models import capability_report
from backend.models import (Alert, Forecast, HistoricalPrice, SupplierQuote,
                            Transformer)

router = APIRouter(prefix="/api", tags=["meta"])

_STARTED = dt.datetime.now(dt.timezone.utc)


@router.get("/health")
def health(db=Depends(get_db)):
    try:
        db.execute(select(func.count()).select_from(HistoricalPrice)).scalar_one()
        db_ok = True
    except Exception:                                       # noqa: BLE001
        db_ok = False
    return {"status": "ok" if db_ok else "degraded",
            "database": "ok" if db_ok else "unreachable",
            "uptime_seconds": int((dt.datetime.now(dt.timezone.utc) - _STARTED).total_seconds()),
            "version": settings.APP_VERSION}


@router.get("/config")
def config():
    """
    Deliberately partial: exposes behaviour, never secrets. API keys are read
    from the environment inside the backend and are not serialised anywhere the
    browser can reach (spec §20).
    """
    def configured(v):
        return bool(v)
    return {
        "app": settings.APP_NAME, "version": settings.APP_VERSION,
        "data_mode": settings.DATA_MODE,
        "database": ("sqlite" if settings.DATABASE_URL.startswith("sqlite")
                     else "postgresql" if "postgres" in settings.DATABASE_URL else "other"),
        "scheduler_enabled": settings.ENABLE_SCHEDULER,
        "schedules": {"daily": settings.DAILY_FETCH_CRON,
                      "weekly": settings.WEEKLY_RETRAIN_CRON,
                      "monthly": settings.MONTHLY_REPORT_CRON},
        "forecast_horizons": list(settings.FORECAST_HORIZONS),
        "ma_windows": list(settings.MA_WINDOWS),
        "stale_after_hours": settings.STALE_AFTER_HOURS,
        "model_capabilities": capability_report(),
        "sources_configured": {
            "lme_api": configured(settings.LME_API_URL),
            "metals_feed": configured(settings.METALS_API_URL),
            "fx_api": configured(settings.FX_API_URL),
            "nalco_endpoint": configured(settings.NALCO_CIRCULAR_URL),
            "balco_endpoint": configured(settings.BALCO_CIRCULAR_URL),
            "hindalco_endpoint": configured(settings.HINDALCO_CIRCULAR_URL),
        },
        "note": ("Producers without an endpoint are fed by dropping their official "
                 "circular into data/imports/. No credentials are ever sent to the browser."),
    }


@router.get("/stats")
def stats(db=Depends(get_db)):
    def n(model):
        return db.execute(select(func.count()).select_from(model)).scalar_one()
    first, last = db.execute(select(func.min(HistoricalPrice.price_date),
                                    func.max(HistoricalPrice.price_date))).one()
    classes = dict(db.execute(
        select(HistoricalPrice.data_class, func.count())
        .group_by(HistoricalPrice.data_class)).all())
    return {"price_rows": n(HistoricalPrice), "forecast_rows": n(Forecast),
            "quotes": n(SupplierQuote), "transformers": n(Transformer),
            "alerts": n(Alert),
            "history_from": first.isoformat() if first else None,
            "history_to": last.isoformat() if last else None,
            "rows_by_data_class": classes}


@router.get("/scheduler")
def scheduler_status():
    from backend.scheduler import job_status
    return job_status()
