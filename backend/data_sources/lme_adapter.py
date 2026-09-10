"""
LME / international copper reference adapter.

LME data is licensed. This adapter therefore talks to whatever endpoint the
operator is entitled to use (LME_API_URL) or a licensed redistributor
(METALS_API_URL). With no entitlement configured it reports UNAVAILABLE
rather than scraping a site whose terms forbid it (spec §23).
"""
from __future__ import annotations

import datetime as dt

from backend.config import settings
from backend.data_sources.base import BaseAdapter, PriceRecord, http_get_json


class LMEAdapter(BaseAdapter):
    name = "lme_adapter"
    source_name = "LME Cash (licensed feed)"
    requires = ("LME_API_URL",)

    def _fetch(self, symbol: str = "CU", days: int = 1, **_) -> list[PriceRecord]:
        headers = {"Accept": "application/json"}
        if settings.LME_API_KEY:
            headers["Authorization"] = f"Bearer {settings.LME_API_KEY}"
        payload = http_get_json(settings.LME_API_URL,
                                params={"symbol": symbol, "days": days}, headers=headers)
        rows = payload.get("data", payload) if isinstance(payload, dict) else payload
        out: list[PriceRecord] = []
        for r in rows or []:
            d = r.get("date") or r.get("price_date")
            price = r.get("price") or r.get("close") or r.get("cash")
            if not d or price is None:
                continue
            out.append(PriceRecord(
                price_date=dt.date.fromisoformat(str(d)[:10]),
                commodity_code="CU", provider_code="LME",
                product_code="CU_CATHODE", grade="Grade A",
                price=float(price), currency=r.get("currency", "USD"),
                unit=r.get("unit", "MT"),
                open_price=r.get("open"), high_price=r.get("high"),
                low_price=r.get("low"), close_price=r.get("close"),
                price_basis="CASH", source=self.source_name,
                source_url=settings.LME_API_URL, data_class="LIVE",
            ))
        return out


class MetalsFeedAdapter(BaseAdapter):
    """Generic licensed metals redistributor (copper + aluminium)."""
    name = "metals_feed"
    source_name = "Licensed metals data feed"
    requires = ("METALS_API_URL",)

    def _fetch(self, symbols: tuple[str, ...] = ("CU", "AL"), **_) -> list[PriceRecord]:
        headers = {"Accept": "application/json"}
        if settings.METALS_API_KEY:
            headers["x-api-key"] = settings.METALS_API_KEY
        payload = http_get_json(settings.METALS_API_URL,
                                params={"symbols": ",".join(symbols)}, headers=headers)
        out: list[PriceRecord] = []
        for sym, r in (payload.get("rates", {}) or {}).items():
            code = "CU" if sym.upper().startswith("CU") else "AL"
            out.append(PriceRecord(
                price_date=dt.date.today(), commodity_code=code, provider_code="LME",
                price=float(r), currency=payload.get("base", "USD"), unit="MT",
                price_basis="CASH", source=self.source_name,
                source_url=settings.METALS_API_URL, data_class="LIVE",
            ))
        return out
