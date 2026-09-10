"""USD/INR and EUR/INR. Falls back to a configured constant, always labelled."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from backend.config import settings
from backend.data_sources.base import (STATUS_SUCCESS, STATUS_UNAVAILABLE,
                                       BaseAdapter, http_get_json)
from backend.utils.logging_config import get_logger

log = get_logger(__name__)


@dataclass
class FxQuote:
    rate_date: dt.date
    base: str
    quote: str
    rate: float
    source: str
    data_class: str        # LIVE / VERIFIED_HISTORICAL / ESTIMATED


class FxAdapter(BaseAdapter):
    name = "fx_adapter"
    source_name = "Exchange rate API"
    requires = ("FX_API_URL",)

    def _fetch(self, bases=("USD", "EUR"), **_):
        raise NotImplementedError  # FX returns FxQuote, use fetch_rates()

    def fetch_rates(self, bases=("USD", "EUR")) -> tuple[list[FxQuote], str, str | None]:
        """Returns (quotes, status, error). Never raises."""
        if not self.is_configured():
            return self._fallback(bases), STATUS_UNAVAILABLE, "FX_API_URL not configured"
        try:
            headers = {"Accept": "application/json"}
            if settings.FX_API_KEY:
                headers["apikey"] = settings.FX_API_KEY
            payload = http_get_json(settings.FX_API_URL,
                                    params={"base": bases[0], "symbols": "INR"}, headers=headers)
            rates = payload.get("rates") or {}
            out: list[FxQuote] = []
            if "INR" in rates:
                out.append(FxQuote(dt.date.today(), bases[0], "INR", float(rates["INR"]),
                                   self.source_name, "LIVE"))
            for b in bases[1:]:
                p2 = http_get_json(settings.FX_API_URL,
                                   params={"base": b, "symbols": "INR"}, headers=headers)
                r2 = (p2.get("rates") or {}).get("INR")
                if r2:
                    out.append(FxQuote(dt.date.today(), b, "INR", float(r2),
                                       self.source_name, "LIVE"))
            if out:
                return out, STATUS_SUCCESS, None
            return self._fallback(bases), STATUS_UNAVAILABLE, "FX response contained no INR rate"
        except Exception as exc:                                    # noqa: BLE001
            log.warning("FX fetch failed: %s - using configured fallback", exc)
            return self._fallback(bases), STATUS_UNAVAILABLE, f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _fallback(bases) -> list[FxQuote]:
        """Configured constants. Marked ESTIMATED so the UI can flag them."""
        m = {"USD": settings.FALLBACK_USDINR, "EUR": settings.FALLBACK_EURINR}
        return [FxQuote(dt.date.today(), b, "INR", m[b],
                        "Configured fallback (FALLBACK_%sINR)" % b, "ESTIMATED")
                for b in bases if b in m]
