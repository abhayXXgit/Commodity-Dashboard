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

            out: list[FxQuote] = []
            for b in bases:
                payload = self._request(b, headers)
                rates = payload.get("rates") or payload.get("conversion_rates") or {}
                # some providers answer with success=false rather than an HTTP error
                if payload.get("success") is False or payload.get("result") == "error":
                    err = (payload.get("error") or {}).get("info") or payload.get("error-type")
                    raise RuntimeError(f"provider rejected the request: {err}")
                inr = rates.get("INR")
                if inr:
                    out.append(FxQuote(dt.date.today(), b, "INR", float(inr),
                                       self.source_name, "LIVE"))

            if out:
                return out, STATUS_SUCCESS, None
            return self._fallback(bases), STATUS_UNAVAILABLE, "FX response contained no INR rate"
        except Exception as exc:                                    # noqa: BLE001
            log.warning("FX fetch failed: %s - using configured fallback", exc)
            return self._fallback(bases), STATUS_UNAVAILABLE, f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _request(base: str, headers: dict):
        """
        Fetch one base currency, tolerating the two URL shapes providers use.

        Path style  : https://open.er-api.com/v6/latest/USD
        Query style : https://api.example.com/latest?base=USD&symbols=INR

        Guessing wrong yields a 404 or an empty rate set, so the shape is
        decided by the configured URL rather than assumed.
        """
        url = settings.FX_API_URL.rstrip("/")
        if url.endswith("/latest") or url.endswith("/v6/latest"):
            return http_get_json(f"{url}/{base}", headers=headers)
        return http_get_json(url, params={"base": base, "symbols": "INR"}, headers=headers)

    @staticmethod
    def _fallback(bases) -> list[FxQuote]:
        """Configured constants. Marked ESTIMATED so the UI can flag them."""
        m = {"USD": settings.FALLBACK_USDINR, "EUR": settings.FALLBACK_EURINR}
        return [FxQuote(dt.date.today(), b, "INR", m[b],
                        "Configured fallback (FALLBACK_%sINR)" % b, "ESTIMATED")
                for b in bases if b in m]
