"""
Excel / CSV import and export (spec §13).

Import is validated, not trusted: every row goes through backend.utils.validation
before it can reach the database, duplicates are detected against what is
already stored, and a rejected row is reported back with the reason rather than
being silently dropped.
"""
from __future__ import annotations

import datetime as dt
import io
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import select

from backend.config import settings
from backend.models import (DataQualityFlag, HistoricalPrice)
from backend.services import analytics as A
from backend.utils.logging_config import get_logger
from backend.utils.units import normalise_price
from backend.utils.validation import (detect_duplicates, validate_row)

log = get_logger(__name__)

REQUIRED_COLUMNS = ["date", "commodity", "provider", "product", "grade", "price",
                    "currency", "unit", "basis", "premium", "freight", "source", "remarks"]
MANDATORY = {"date", "commodity", "provider", "price"}

# How much we trust each kind of number. A more trustworthy observation is
# allowed to supersede a less trustworthy one for the same series and date -
# otherwise the very first real circular an operator imports is discarded as a
# "duplicate" of the simulated row already sitting there, and the dashboard
# keeps showing the demo price with no indication anything went wrong.
TRUST = {"DEMO_DATA": 0, "ESTIMATED": 1, "SUPPLIER_QUOTE": 2,
         "VERIFIED_HISTORICAL": 3, "LIVE": 4}

ALIASES = {
    "date": ("date", "price_date", "effective_date", "dt", "trade_date"),
    "commodity": ("commodity", "commodity_code", "metal", "material"),
    "provider": ("provider", "supplier", "source_provider", "producer", "exchange"),
    "product": ("product", "product_code", "item", "description"),
    "grade": ("grade", "spec", "quality"),
    "price": ("price", "rate", "value", "amount", "basic_price"),
    "currency": ("currency", "curr", "ccy"),
    "unit": ("unit", "uom", "units"),
    "basis": ("basis", "price_basis", "terms", "incoterm"),
    "premium": ("premium", "prem"),
    "freight": ("freight", "frt", "transport"),
    "location": ("location", "depot", "plant", "city"),
    "source": ("source", "reference", "origin"),
    "remarks": ("remarks", "notes", "comment", "comments"),
}


def _norm(c: str) -> str:
    return str(c).strip().lower().replace(" ", "_").replace("-", "_").replace(".", "")


# A producer's own circular never carries a "Commodity" column - NALCO only
# sells aluminium, so the sheet does not restate it on every row. Rather than
# rejecting the exact file the operator downloaded from the producer, infer the
# missing identifiers from the filename and say so in the response.
FILENAME_HINTS = {
    "nalco": ("AL", "NALCO"), "balco": ("AL", "BALCO"), "vedanta": ("AL", "BALCO"),
    "hindalco": ("AL", "HINDALCO"), "aditya": ("AL", "HINDALCO"),
    "lme": ("CU", "LME"), "copper": ("CU", "BME"), "cathode": ("CU", "BME"),
    "aluminium": ("AL", "NALCO"), "aluminum": ("AL", "NALCO"),
    "crgo": ("CRGO", "MARKET"), "steel": ("STEEL", "MARKET"), "oil": ("OIL", "MARKET"),
}

# Free-text product names as producers actually write them -> product codes.
PRODUCT_HINTS = [
    (("wire rod", "wirerod", "wire_rod", "rod"), {"AL": "AL_WIREROD", "CU": "CU_WIREROD"}),
    (("billet",), {"AL": "AL_BILLET"}),
    (("ingot", "p1020"), {"AL": "AL_INGOT"}),
    (("cathode",), {"CU": "CU_CATHODE"}),
    (("lamination", "crgo", "m4"), {"CRGO": "CRGO_M4"}),
    (("plate", "ms"), {"STEEL": "STEEL_MS"}),
    (("oil",), {"OIL": "OIL_TRF"}),
]


def infer_from_filename(filename: str) -> tuple[str | None, str | None]:
    name = (filename or "").lower()
    for token, (ccode, pvcode) in FILENAME_HINTS.items():
        if token in name:
            return ccode, pvcode
    return None, None


def infer_product(text: str | None, ccode: str) -> str | None:
    if not text:
        return None
    t = str(text).strip().lower()
    for tokens, mapping in PRODUCT_HINTS:
        if any(tok in t for tok in tokens) and ccode in mapping:
            return mapping[ccode]
    return None


def _map_columns(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, str], list[str]]:
    df = df.copy()
    df.columns = [_norm(c) for c in df.columns]
    mapping, missing = {}, []
    for canon, aliases in ALIASES.items():
        hit = next((a for a in aliases if a in df.columns), None)
        if hit:
            mapping[canon] = hit
        elif canon in MANDATORY:
            missing.append(canon)
    return df, mapping, missing


def build_template(path: str | Path | None = None) -> bytes:
    """Blank import workbook with the exact columns the importer expects."""
    sample = pd.DataFrame([{
        "Date": dt.date.today().isoformat(), "Commodity": "AL", "Provider": "NALCO",
        "Product": "AL_INGOT", "Grade": "P1020A", "Price": 262000, "Currency": "INR",
        "Unit": "MT", "Basis": "EX_PLANT", "Premium": 0, "Freight": 0,
        "Source": "NALCO circular dated ...", "Remarks": "",
    }])
    guide = pd.DataFrame({
        "Column": REQUIRED_COLUMNS,
        "Mandatory": ["YES" if c in MANDATORY else "no" for c in REQUIRED_COLUMNS],
        "Notes": [
            "ISO date or Excel date. Future dates are rejected.",
            "Commodity code: CU, AL, CRGO, STEEL, OIL",
            "Provider code: LME, BME, NALCO, BALCO, HINDALCO, MARKET, INTERNAL",
            "Product code: CU_CATHODE, AL_INGOT, AL_WIREROD, AL_BILLET, ...",
            "Free text grade, e.g. P1020A / ETP / M4",
            "Numeric, greater than zero. Stored exactly as supplied.",
            "INR, USD, EUR ... converted for analytics, never overwritten.",
            "MT, KG, LB, QUINTAL - normalised to INR/MT for analytics.",
            "EX_PLANT / CIF / FOB / DELIVERED / CASH",
            "Optional INR/MT premium over the benchmark.",
            "Optional INR/MT freight.",
            "Where the number came from. Recorded for audit.",
            "Free text.",
        ]})
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        sample.to_excel(xw, sheet_name="Prices", index=False)
        guide.to_excel(xw, sheet_name="Column Guide", index=False)
    data = buf.getvalue()
    if path:
        Path(path).write_bytes(data)
    return data


def import_prices(db, content: bytes | None = None, path: str | Path | None = None,
                  filename: str = "upload.xlsx", data_class: str = "VERIFIED_HISTORICAL",
                  dry_run: bool = False) -> dict[str, Any]:
    """Validate and load a price workbook. Returns a full reconciliation report."""
    if content is not None:
        bio = io.BytesIO(content)
        df = (pd.read_csv(bio) if filename.lower().endswith(".csv")
              else pd.read_excel(bio))
    elif path:
        p = Path(path)
        df = pd.read_csv(p) if p.suffix.lower() == ".csv" else pd.read_excel(p)
        filename = p.name
    else:
        raise ValueError("import_prices needs either content or path")

    df, colmap, missing = _map_columns(df)

    # Fill in what the sheet does not state, from the filename.
    inferred: dict[str, str] = {}
    if missing:
        ic, ip = infer_from_filename(filename)
        if "commodity" in missing and ic:
            inferred["commodity"] = ic
            missing.remove("commodity")
        if "provider" in missing and ip:
            inferred["provider"] = ip
            missing.remove("provider")

    if missing:
        hint = ""
        if "commodity" in missing or "provider" in missing:
            hint = (" Either add these columns, or name the file after the producer "
                    "(e.g. nalco_2026-09.xlsx) and they will be inferred.")
        return {"ok": False, "filename": filename,
                "error": f"Missing mandatory column(s): {', '.join(missing)}.{hint}",
                "expected_columns": REQUIRED_COLUMNS,
                "found_columns": list(df.columns)}

    ids = A.resolve_ids(db)
    rows, rejected, issues = [], [], []

    for i, raw in df.iterrows():
        rec = {canon: raw.get(src) for canon, src in colmap.items()}
        try:
            d = pd.to_datetime(rec.get("date")).date()
        except Exception:                                       # noqa: BLE001
            rejected.append({"row": int(i) + 2, "reason": f"unparseable date {rec.get('date')!r}"})
            continue
        rec["price_date"] = d
        ok, iss = validate_row(rec, int(i) + 2)
        issues.extend(iss)
        if not ok:
            rejected.append({"row": int(i) + 2,
                             "reason": "; ".join(x.detail for x in iss) or "failed validation"})
            continue

        ccode = str(rec.get("commodity") or inferred.get("commodity") or "").strip().upper()
        pvcode = str(rec.get("provider") or inferred.get("provider") or "").strip().upper()

        raw_product = rec.get("product")
        prcode = None
        if raw_product is not None and not pd.isna(raw_product):
            candidate = str(raw_product).strip().upper()
            if candidate in ids["product"]:
                prcode = candidate                      # already a product code
            else:
                prcode = infer_product(raw_product, ccode)   # free text, e.g. "Wire Rod"
                if prcode is None:
                    rejected.append({"row": int(i) + 2,
                                     "reason": f"could not map product {raw_product!r} "
                                               f"to a known {ccode} product"})
                    continue
        if ccode not in ids["commodity"]:
            rejected.append({"row": int(i) + 2, "reason": f"unknown commodity code {ccode!r}"})
            continue
        if pvcode not in ids["provider"]:
            rejected.append({"row": int(i) + 2, "reason": f"unknown provider code {pvcode!r}"})
            continue
        if prcode and prcode not in ids["product"]:
            rejected.append({"row": int(i) + 2, "reason": f"unknown product code {prcode!r}"})
            continue

        rec["commodity"], rec["provider"], rec["product"] = ccode, pvcode, prcode
        rows.append(rec)

    dupes_in_file = detect_duplicates(rows)
    issues.extend(dupes_in_file)

    inserted, dupes_in_db, superseded = 0, 0, 0
    if not dry_run:
        for rec in rows:
            cid = ids["commodity"][rec["commodity"]]
            pid = ids["provider"][rec["provider"]]
            prid = ids["product"].get(rec["product"]) if rec.get("product") else None
            exists = db.scalar(select(HistoricalPrice).where(
                HistoricalPrice.price_date == rec["price_date"],
                HistoricalPrice.commodity_id == cid,
                HistoricalPrice.provider_id == pid,
                HistoricalPrice.product_id == prid)
                .order_by(HistoricalPrice.revision.desc()).limit(1))
            if exists:
                incoming = TRUST.get(data_class, 0)
                stored = TRUST.get(str(exists.data_class), 0)
                if incoming > stored:
                    # Better data for a slot we already hold. Retire the old row
                    # rather than deleting it, so the revision history stays auditable.
                    db.delete(exists)
                    db.flush()
                    superseded += 1
                else:
                    dupes_in_db += 1
                    continue
            currency = str(rec.get("currency") or "INR").strip().upper()
            unit = str(rec.get("unit") or "MT").strip().upper()
            fx = None
            if currency != "INR":
                fx, _, _ = A.latest_fx(db, currency)
            price = float(str(rec["price"]).replace(",", ""))
            db.add(HistoricalPrice(
                price_date=rec["price_date"], commodity_id=cid, provider_id=pid,
                product_id=prid, grade=_s(rec.get("grade")),
                price=price, currency=currency, unit=unit,
                price_basis=_s(rec.get("basis")) or "EX_PLANT",
                location=_s(rec.get("location")),
                premium=_f(rec.get("premium")), freight=_f(rec.get("freight")),
                price_inr_mt=round(normalise_price(price, currency, unit, fx), 4),
                fx_rate_used=fx, effective_date=rec["price_date"],
                source=_s(rec.get("source")) or f"Excel import [{filename}]",
                source_url=filename, data_class=data_class,
                validation_status="PASS",
                quality_flags=None,
            ))
            inserted += 1

        for iss in issues:
            if iss.flag_type in ("DUPLICATE", "OUTLIER", "JUMP"):
                db.add(DataQualityFlag(table_name="historical_prices",
                                       flag_type=iss.flag_type, detail=iss.detail,
                                       severity=iss.severity))
        db.flush()

    return {
        "ok": True, "filename": filename, "dry_run": dry_run,
        "inferred_from_filename": inferred or None,
        "rows_read": int(len(df)), "rows_valid": len(rows),
        "rows_inserted": inserted,
        "rows_superseded": superseded,
        "duplicates_in_file": len([i for i in dupes_in_file]),
        "duplicates_already_stored": dupes_in_db,
        "rows_rejected": len(rejected),
        "rejected": rejected[:100],
        "issues": [{"type": i.flag_type, "detail": i.detail, "severity": i.severity}
                   for i in issues[:100]],
        "data_class": data_class,
    }


def _s(v):
    return None if v is None or (isinstance(v, float) and pd.isna(v)) else str(v).strip() or None


def _f(v, default=0.0):
    try:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return default
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- export
def export_workbook(db, commodity: str | None = None, start: dt.date | None = None,
                    end: dt.date | None = None) -> bytes:
    """Multi-sheet analyst workbook: prices, quotes, BOM, exposure, forecasts."""
    from backend.services import bom_engine as B
    from backend.services import forecast_service as F
    from backend.services import quotes as Q

    prices = A.load_series(db, commodity=commodity, start=start, end=end)
    if not prices.empty:
        prices = prices[["date", "commodity", "provider", "product", "src_price",
                         "currency", "unit", "price", "source", "data_class"]]
        prices.columns = ["Date", "Commodity", "Provider", "Product", "Source Price",
                          "Currency", "Unit", "INR per MT", "Source", "Data Class"]

    roll = B.bom_rollup(db)
    bom_rows = []
    for t in roll["transformers"]:
        for i in t["items"]:
            bom_rows.append({
                "Job No": t["job_no"], "Customer": t["customer"],
                "Rating MVA": t["rating_mva"], "Qty": t["quantity"],
                "BOM Item": i["bom_item"], "Category": i["category"],
                "Commodity": i["commodity"], "Product": i["product"],
                "Consumption MT/unit": i["consumption_mt"],
                "Market Rate INR/MT": i["market_rate"], "BOM Rate INR/MT": i["bom_rate"],
                "Rate Variance %": i["rate_variance_pct"],
                "Material Cost/unit": i["material_cost"],
                "Project Material Cost": i["project_material_cost"],
            })

    exposure = [{"Commodity": k, **{kk.replace("_", " ").title(): vv for kk, vv in v.items()}}
                for k, v in roll["by_commodity"].items()]
    cov = B.coverage(db)
    quote_rows = Q.list_quotes(db)

    fc_rows = []
    for ccode, pv, pr, label in F.DEFAULT_SERIES:
        for r in F.stored_forecasts(db, ccode, pv, pr):
            fc_rows.append({"Series": label, "Commodity": ccode, "Provider": pv,
                            "Product": pr, "Horizon (days)": r["horizon_days"],
                            "Target Date": r["target_date"],
                            "Forecast INR/MT": r["forecast_price"],
                            "Lower 95%": r["lower_ci"], "Upper 95%": r["upper_ci"],
                            "Model": r["model_used"], "MAPE %": r["mape"],
                            "Direction": r["direction"], "Confidence": r["confidence"]})

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        meta = pd.DataFrame([
            {"Field": "Generated", "Value": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
            {"Field": "Application", "Value": f"{settings.APP_NAME} {settings.APP_VERSION}"},
            {"Field": "Data mode", "Value": settings.DATA_MODE},
            {"Field": "Filter - commodity", "Value": commodity or "ALL"},
            {"Field": "Note", "Value": ("Rows marked DEMO_DATA are simulated and are not "
                                        "market data. Check the Data Class column.")},
        ])
        meta.to_excel(xw, sheet_name="About", index=False)
        if not prices.empty:
            prices.to_excel(xw, sheet_name="Price History", index=False)
        pd.DataFrame(quote_rows).to_excel(xw, sheet_name="Supplier Quotes", index=False)
        pd.DataFrame(bom_rows).to_excel(xw, sheet_name="Transformer BOM", index=False)
        pd.DataFrame(exposure).to_excel(xw, sheet_name="Commodity Exposure", index=False)
        pd.DataFrame(cov).to_excel(xw, sheet_name="Coverage", index=False)
        if fc_rows:
            pd.DataFrame(fc_rows).to_excel(xw, sheet_name="Forecasts", index=False)
    return buf.getvalue()


def export_csv(db, commodity: str | None = None, start=None, end=None) -> bytes:
    df = A.load_series(db, commodity=commodity, start=start, end=end)
    return df.to_csv(index=False).encode("utf-8")
