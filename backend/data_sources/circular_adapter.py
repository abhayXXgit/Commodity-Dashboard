"""
Indian primary-producer price circulars: NALCO, BALCO/Vedanta, Hindalco.

These producers publish price circulars as PDF/Excel on their own portals and
generally do not offer an open machine API. Automated scraping can breach their
terms, so the supported paths are:

  1. CIRCULAR_IMPORT - operator drops the official circular workbook into
     data/imports/ and it is parsed here (default, always legal);
  2. API            - if the operator has an entitled endpoint, set
     NALCO_CIRCULAR_URL / BALCO_CIRCULAR_URL / HINDALCO_CIRCULAR_URL.

Anything else returns UNAVAILABLE. No price is ever synthesised.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from backend.config import settings
from backend.data_sources.base import BaseAdapter, PriceRecord, http_get_json
from backend.utils.logging_config import get_logger

log = get_logger(__name__)

# Column aliases accepted in an uploaded circular workbook
COL_ALIASES = {
    "date": ("date", "price_date", "effective_date", "w.e.f", "wef", "effective"),
    "product": ("product", "item", "grade_product", "description"),
    "price": ("price", "rate", "basic_price", "amount", "value"),
    "unit": ("unit", "uom"),
    "currency": ("currency", "curr"),
    "grade": ("grade", "spec", "specification"),
    "location": ("location", "depot", "plant", "smelter"),
    "basis": ("basis", "price_basis", "terms"),
}


def _norm(s: str) -> str:
    return str(s).strip().lower().replace(" ", "_").replace("-", "_").replace(".", "")


# Filenames that are format demonstrations, not published circulars. Ingesting
# one would stamp invented figures as VERIFIED_HISTORICAL under a producer's
# name - and, because verified data outranks demo data, those figures would then
# be trusted over everything else. A sample must never become a price.
SAMPLE_TOKENS = ("sample", "example", "template", "demo", "dummy", "test", "mock")


def looks_like_a_sample(name: str) -> bool:
    stem = _norm(Path(name).stem)
    return any(tok in stem for tok in SAMPLE_TOKENS)


class ProducerCircularAdapter(BaseAdapter):
    """One instance per producer."""

    def __init__(self, provider_code: str, url_setting: str, display: str):
        self.provider_code = provider_code
        self.url_setting = url_setting
        self.name = f"circular_{provider_code.lower()}"
        self.source_name = f"{display} official price circular"
        self.requires = ()          # file import path is always available

    # ---- API path ---------------------------------------------------------
    def _fetch_api(self) -> list[PriceRecord]:
        url = getattr(settings, self.url_setting, "")
        if not url:
            return []
        payload = http_get_json(url)
        rows = payload.get("data", payload) if isinstance(payload, dict) else payload
        out = []
        for r in rows or []:
            out.append(PriceRecord(
                price_date=dt.date.fromisoformat(str(r["date"])[:10]),
                commodity_code="AL", provider_code=self.provider_code,
                product_code=r.get("product_code", "AL_INGOT"),
                grade=r.get("grade", "P1020A"),
                price=float(r["price"]), currency=r.get("currency", "INR"),
                unit=r.get("unit", "MT"), price_basis=r.get("basis", "EX_PLANT"),
                location=r.get("location"), source=self.source_name,
                source_url=url, data_class="LIVE",
            ))
        return out

    # ---- circular workbook path ------------------------------------------
    def _fetch_files(self) -> list[PriceRecord]:
        import pandas as pd

        out: list[PriceRecord] = []
        pattern = self.provider_code.lower()
        for path in sorted(Path(settings.IMPORT_DIR).glob("*")):
            if path.suffix.lower() not in (".xlsx", ".xls", ".csv"):
                continue
            if pattern not in path.name.lower():
                continue
            if looks_like_a_sample(path.name):
                log.warning("skipping %s - it looks like a format sample, not a "
                            "published circular. Rename it if it is genuine.", path.name)
                continue
            df = pd.read_csv(path) if path.suffix.lower() == ".csv" else pd.read_excel(path)
            df.columns = [_norm(c) for c in df.columns]
            colmap = {}
            for canon, aliases in COL_ALIASES.items():
                for a in aliases:
                    if a in df.columns:
                        colmap[canon] = a
                        break
            if "price" not in colmap or "date" not in colmap:
                continue
            for _, row in df.iterrows():
                try:
                    d = pd.to_datetime(row[colmap["date"]]).date()
                    price = float(str(row[colmap["price"]]).replace(",", ""))
                except Exception:                                   # noqa: BLE001
                    continue
                prod = str(row.get(colmap.get("product", ""), "Ingot")).strip()
                pc = ("AL_WIREROD" if "rod" in prod.lower()
                      else "AL_BILLET" if "billet" in prod.lower() else "AL_INGOT")
                out.append(PriceRecord(
                    price_date=d, commodity_code="AL", provider_code=self.provider_code,
                    product_code=pc, grade=str(row.get(colmap.get("grade", ""), "P1020A")),
                    price=price, currency=str(row.get(colmap.get("currency", ""), "INR")).upper(),
                    unit=str(row.get(colmap.get("unit", ""), "MT")).upper(),
                    price_basis=str(row.get(colmap.get("basis", ""), "EX_PLANT")),
                    location=str(row.get(colmap.get("location", ""), "") or "") or None,
                    source=f"{self.source_name} [{path.name}]",
                    source_url=str(path), data_class="VERIFIED_HISTORICAL",
                    effective_date=d,
                ))
        return out

    def _fetch(self, **_) -> list[PriceRecord]:
        recs = self._fetch_api()
        recs.extend(self._fetch_files())
        return recs


def build_producer_adapters() -> list[ProducerCircularAdapter]:
    return [
        ProducerCircularAdapter("NALCO", "NALCO_CIRCULAR_URL", "NALCO"),
        ProducerCircularAdapter("BALCO", "BALCO_CIRCULAR_URL", "BALCO / Vedanta"),
        ProducerCircularAdapter("HINDALCO", "HINDALCO_CIRCULAR_URL", "Hindalco / Aditya Birla"),
    ]
