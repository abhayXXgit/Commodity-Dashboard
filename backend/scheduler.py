"""
APScheduler wiring (spec §19).

Daily   : fetch prices + FX, validate, refresh the live snapshot, run alerts
Weekly  : retrain the forecast models and re-evaluate alerts
Monthly : write the management report and archive the workbook

Jobs are wrapped so a failure is logged and the scheduler keeps running - a
broken external source must never stop the rest of the pipeline.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from backend.config import settings
from backend.services import etl
from backend.utils.logging_config import get_logger

log = get_logger(__name__)

_scheduler: BackgroundScheduler | None = None
_last_runs: dict[str, dict[str, Any]] = {}


def _guard(name: str, fn):
    def wrapped():
        started = dt.datetime.now(dt.timezone.utc)
        try:
            result = fn()
            _last_runs[name] = {"status": "SUCCESS", "started": started.isoformat(),
                                "finished": dt.datetime.now(dt.timezone.utc).isoformat(),
                                "summary": _summarise(result)}
            log.info("job %s finished", name)
        except Exception as exc:                            # noqa: BLE001
            _last_runs[name] = {"status": "FAILED", "started": started.isoformat(),
                                "finished": dt.datetime.now(dt.timezone.utc).isoformat(),
                                "error": f"{type(exc).__name__}: {exc}"}
            log.exception("scheduled job %s failed", name)
    wrapped.__name__ = f"job_{name}"
    return wrapped


def _summarise(result) -> Any:
    if isinstance(result, dict):
        return {k: (v if isinstance(v, (int, float, str, bool, type(None))) else "...")
                for k, v in result.items()}
    return str(result)[:200]


def start_scheduler() -> BackgroundScheduler | None:
    global _scheduler
    if not settings.ENABLE_SCHEDULER:
        log.info("scheduler disabled (ENABLE_SCHEDULER=false)")
        return None
    if _scheduler and _scheduler.running:
        return _scheduler

    _scheduler = BackgroundScheduler(timezone="Asia/Kolkata",
                                     job_defaults={"coalesce": True, "max_instances": 1,
                                                   "misfire_grace_time": 3600})
    jobs = [("daily_fetch", settings.DAILY_FETCH_CRON, etl.daily_job),
            ("weekly_retrain", settings.WEEKLY_RETRAIN_CRON, etl.weekly_job),
            ("monthly_report", settings.MONTHLY_REPORT_CRON, etl.monthly_job)]
    for name, cron, fn in jobs:
        try:
            _scheduler.add_job(_guard(name, fn), CronTrigger.from_crontab(cron),
                               id=name, replace_existing=True)
            log.info("scheduled %s at '%s' (Asia/Kolkata)", name, cron)
        except ValueError:
            log.error("invalid cron for %s: %r - job not scheduled", name, cron)
    _scheduler.start()
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        log.info("scheduler stopped")
    _scheduler = None


def job_status() -> dict[str, Any]:
    running = bool(_scheduler and _scheduler.running)
    jobs = []
    if running:
        for j in _scheduler.get_jobs():
            jobs.append({"id": j.id,
                         "next_run": j.next_run_time.isoformat() if j.next_run_time else None,
                         "trigger": str(j.trigger)})
    return {"enabled": settings.ENABLE_SCHEDULER, "running": running,
            "timezone": "Asia/Kolkata", "jobs": jobs, "last_runs": _last_runs}
