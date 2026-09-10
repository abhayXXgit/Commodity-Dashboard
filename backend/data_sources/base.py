"""
Adapter contract for every external price source.

Design rules (spec §20, §23):
  * Adapters NEVER invent a price. If the source is unreachable or
    unconfigured they return FetchResult(status=UNAVAILABLE) and the UI shows
    "Live data unavailable - last verified price shown".
  * API keys come from the environment only and never leave the backend.
  * Every attempt is written to data_source_log with timing + error text.
"""
from __future__ import annotations

import abc
import datetime as dt
import time
from dataclasses import dataclass, field
from typing import Any

from backend.config import settings
from backend.utils.logging_config import get_logger

log = get_logger(__name__)

STATUS_SUCCESS = "SUCCESS"
STATUS_PARTIAL = "PARTIAL"
STATUS_FAILED = "FAILED"
STATUS_UNAVAILABLE = "UNAVAILABLE"
STATUS_SKIPPED = "SKIPPED"


@dataclass
class PriceRecord:
    """One observation, exactly as the source published it."""
    price_date: dt.date
    commodity_code: str
    provider_code: str
    price: float
    currency: str
    unit: str
    product_code: str | None = None
    grade: str | None = None
    open_price: float | None = None
    high_price: float | None = None
    low_price: float | None = None
    close_price: float | None = None
    price_basis: str | None = None
    location: str | None = None
    premium: float = 0.0
    freight: float = 0.0
    tax_pct: float = 0.0
    source: str = ""
    source_url: str | None = None
    data_class: str = "VERIFIED_HISTORICAL"
    effective_date: dt.date | None = None


@dataclass
class FetchResult:
    status: str
    adapter: str
    source_name: str
    records: list[PriceRecord] = field(default_factory=list)
    error: str | None = None
    http_status: int | None = None
    elapsed_ms: int = 0
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in (STATUS_SUCCESS, STATUS_PARTIAL)


class BaseAdapter(abc.ABC):
    """Subclasses implement `_fetch`; the base handles retry/timeout/logging."""

    name: str = "base"
    source_name: str = "unknown"
    requires: tuple[str, ...] = ()          # settings attributes that must be truthy

    def is_configured(self) -> bool:
        return all(bool(getattr(settings, r, "")) for r in self.requires)

    def missing_config(self) -> list[str]:
        return [r for r in self.requires if not getattr(settings, r, "")]

    @abc.abstractmethod
    def _fetch(self, **kwargs) -> list[PriceRecord]:
        ...

    def fetch(self, **kwargs) -> FetchResult:
        t0 = time.time()
        if not self.is_configured():
            missing = ", ".join(self.missing_config())
            log.info("%s: not configured (missing %s) -> UNAVAILABLE", self.name, missing)
            return FetchResult(
                status=STATUS_UNAVAILABLE, adapter=self.name, source_name=self.source_name,
                error=f"Source not configured. Missing environment settings: {missing}",
                elapsed_ms=int((time.time() - t0) * 1000),
            )

        last_err: Exception | None = None
        for attempt in range(1, settings.HTTP_RETRIES + 1):
            try:
                recs = self._fetch(**kwargs)
                # No records and no error is "nothing to do", not a partial
                # failure. Calling it PARTIAL makes a clean run look broken in
                # the source log and hides real partial failures.
                return FetchResult(
                    status=STATUS_SUCCESS if recs else STATUS_SKIPPED,
                    adapter=self.name, source_name=self.source_name, records=recs,
                    error=None if recs else "no new records available from this source",
                    elapsed_ms=int((time.time() - t0) * 1000),
                )
            except Exception as exc:                      # noqa: BLE001 - adapters must never crash a job
                last_err = exc
                wait = settings.HTTP_BACKOFF ** attempt
                log.warning("%s attempt %d/%d failed: %s (retry in %.1fs)",
                            self.name, attempt, settings.HTTP_RETRIES, exc, wait)
                if attempt < settings.HTTP_RETRIES:
                    time.sleep(wait)

        return FetchResult(
            status=STATUS_FAILED, adapter=self.name, source_name=self.source_name,
            error=f"{type(last_err).__name__}: {last_err}",
            elapsed_ms=int((time.time() - t0) * 1000),
        )


def http_get_json(url: str, params: dict | None = None, headers: dict | None = None) -> Any:
    """Single place for outbound HTTP so timeouts/headers stay consistent."""
    import json
    import urllib.parse
    import urllib.request

    if params:
        url = f"{url}{'&' if '?' in url else '?'}{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers=headers or {"Accept": "application/json",
                                                          "User-Agent": "TransformerProcure/2.0"})
    with urllib.request.urlopen(req, timeout=settings.HTTP_TIMEOUT) as resp:   # noqa: S310
        return json.loads(resp.read().decode("utf-8"))
