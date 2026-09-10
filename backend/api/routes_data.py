"""Import / export / refresh / settings / meta."""
from __future__ import annotations

import datetime as dt

from fastapi import (APIRouter, BackgroundTasks, Depends, File, HTTPException,
                     Query, UploadFile)
from fastapi.responses import Response
from sqlalchemy import select

from backend.api.schemas import SettingIn
from backend.database import get_db
from backend.models import DataSourceLog, UserSetting
from backend.services import etl
from backend.services import excel_io as X
from backend.utils.cache import invalidate as invalidate_cache
from backend.utils.logging_config import get_logger

log = get_logger(__name__)
router = APIRouter(prefix="/api/data", tags=["data"])

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
ALLOWED_SUFFIX = (".xlsx", ".xls", ".csv")


@router.get("/template")
def template():
    data = X.build_template()
    return Response(content=data,
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition":
                             'attachment; filename="commodity_import_template.xlsx"'})


@router.post("/import")
async def import_prices(file: UploadFile = File(...), dry_run: bool = False,
                        data_class: str = "VERIFIED_HISTORICAL", db=Depends(get_db)):
    name = (file.filename or "upload.xlsx")
    if not name.lower().endswith(ALLOWED_SUFFIX):
        raise HTTPException(400, f"unsupported file type; expected one of {ALLOWED_SUFFIX}")
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"file exceeds {MAX_UPLOAD_BYTES // (1024*1024)} MB limit")
    if data_class not in ("VERIFIED_HISTORICAL", "SUPPLIER_QUOTE", "ESTIMATED", "DEMO_DATA"):
        raise HTTPException(400, "invalid data_class")
    try:
        out = X.import_prices(db, content=content, filename=name,
                              data_class=data_class, dry_run=dry_run)
    except Exception as exc:                                # noqa: BLE001
        log.exception("import failed")
        raise HTTPException(400, f"could not read workbook: {exc}") from exc
    if not out.get("ok"):
        raise HTTPException(400, out.get("error", "import failed"))
    if not dry_run:
        db.commit()
        invalidate_cache("price import")
    return out


@router.get("/export/excel")
def export_excel(commodity: str | None = None, start: str | None = None,
                 end: str | None = None, db=Depends(get_db)):
    data = X.export_workbook(db, commodity.upper() if commodity else None,
                             dt.date.fromisoformat(start) if start else None,
                             dt.date.fromisoformat(end) if end else None)
    stamp = dt.date.today().isoformat()
    return Response(content=data,
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition":
                             f'attachment; filename="commodity_export_{stamp}.xlsx"'})


@router.get("/export/csv")
def export_csv(commodity: str | None = None, db=Depends(get_db)):
    data = X.export_csv(db, commodity.upper() if commodity else None)
    stamp = dt.date.today().isoformat()
    return Response(content=data, media_type="text/csv",
                    headers={"Content-Disposition":
                             f'attachment; filename="commodity_export_{stamp}.csv"'})


@router.get("/export/pdf")
def export_pdf(db=Depends(get_db)):
    from backend.services.reports import generate_pdf
    try:
        data = generate_pdf(db)
    except RuntimeError as exc:
        raise HTTPException(501, str(exc)) from exc
    stamp = dt.date.today().isoformat()
    return Response(content=data, media_type="application/pdf",
                    headers={"Content-Disposition":
                             f'inline; filename="commodity_report_{stamp}.pdf"'})


@router.post("/refresh")
def refresh(background: BackgroundTasks, full: bool = Query(False,
            description="also retrain forecasts (slow)")):
    """The manual Refresh Data button."""
    out = etl.daily_job()
    if full:
        background.add_task(etl.weekly_job)
        out["forecast_retrain"] = "queued in background"
    return out


@router.get("/source-log")
def source_log(limit: int = 40, db=Depends(get_db)):
    rows = db.scalars(select(DataSourceLog)
                      .order_by(DataSourceLog.run_started.desc()).limit(limit))
    return {"runs": [{
        "id": r.id, "source": r.source_name, "adapter": r.adapter, "status": r.status,
        "started": r.run_started.isoformat() if r.run_started else None,
        "finished": r.run_finished.isoformat() if r.run_finished else None,
        "fetched": r.rows_fetched, "inserted": r.rows_inserted,
        "rejected": r.rows_rejected, "duplicates": r.duplicates_found,
        "error": r.error_message,
        "last_success": r.last_success_at.isoformat() if r.last_success_at else None,
    } for r in rows]}


@router.get("/settings")
def get_settings_(db=Depends(get_db)):
    rows = db.scalars(select(UserSetting).order_by(UserSetting.setting_key))
    return {"settings": [{"key": r.setting_key, "value": r.setting_value,
                          "type": r.value_type, "description": r.description,
                          "updated_at": r.updated_at.isoformat() if r.updated_at else None}
                         for r in rows]}


@router.put("/settings")
def put_setting(payload: SettingIn, db=Depends(get_db)):
    row = db.scalar(select(UserSetting).where(UserSetting.setting_key == payload.key))
    if row:
        row.setting_value = payload.value
        row.value_type = payload.value_type
    else:
        db.add(UserSetting(setting_key=payload.key, setting_value=payload.value,
                           value_type=payload.value_type, description="user defined"))
    db.commit()
    return {"ok": True, "key": payload.key, "value": payload.value}
