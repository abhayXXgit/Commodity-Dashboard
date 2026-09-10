"""
Data-quality control (spec §18).

Detects: missing dates, duplicates, extreme outliers, sudden jumps,
incorrect units, invalid currency, negative prices, stale live data.
Nothing is silently dropped — every rejection is reported with a reason.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Iterable

from backend.utils.units import VALID_CURRENCIES, f, unit_factor

MAX_DAILY_JUMP_PCT = 15.0     # metal spot moves beyond this need a human
OUTLIER_Z = 4.0


@dataclass
class Issue:
    flag_type: str
    detail: str
    severity: str = "WARNING"
    row_index: int | None = None


@dataclass
class ValidationReport:
    passed: list[dict] = field(default_factory=list)
    rejected: list[tuple[dict, str]] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)

    @property
    def summary(self) -> dict[str, Any]:
        by_type: dict[str, int] = {}
        for i in self.issues:
            by_type[i.flag_type] = by_type.get(i.flag_type, 0) + 1
        return {
            "rows_passed": len(self.passed),
            "rows_rejected": len(self.rejected),
            "issue_count": len(self.issues),
            "issues_by_type": by_type,
        }


def validate_row(row: dict, idx: int | None = None) -> tuple[bool, list[Issue]]:
    """Hard validation of a single price row. Returns (is_valid, issues)."""
    issues: list[Issue] = []
    price = f(row.get("price"))

    if price is None:
        issues.append(Issue("BAD_PRICE", "price is missing or non-numeric", "ERROR", idx))
        return False, issues
    if price <= 0:
        issues.append(Issue("NEGATIVE", f"non-positive price {price}", "ERROR", idx))
        return False, issues

    cur = str(row.get("currency") or "INR").strip().upper()
    if cur not in VALID_CURRENCIES:
        issues.append(Issue("BAD_CURRENCY", f"unknown currency {cur!r}", "ERROR", idx))
        return False, issues

    try:
        unit_factor(row.get("unit") or "MT")
    except ValueError as exc:
        issues.append(Issue("BAD_UNIT", str(exc), "ERROR", idx))
        return False, issues

    d = row.get("price_date") or row.get("date")
    if isinstance(d, str):
        try:
            d = dt.date.fromisoformat(d[:10])
        except ValueError:
            issues.append(Issue("BAD_DATE", f"unparseable date {row.get('price_date')!r}", "ERROR", idx))
            return False, issues
    if isinstance(d, dt.datetime):
        d = d.date()
    if not isinstance(d, dt.date):
        issues.append(Issue("BAD_DATE", "missing price_date", "ERROR", idx))
        return False, issues
    if d > dt.date.today() + dt.timedelta(days=1):
        issues.append(Issue("FUTURE_DATE", f"price dated in the future: {d}", "ERROR", idx))
        return False, issues

    return True, issues


def detect_duplicates(rows: Iterable[dict], keys=("price_date", "commodity", "provider", "product")) -> list[Issue]:
    seen: dict[tuple, int] = {}
    out: list[Issue] = []
    for i, r in enumerate(rows):
        k = tuple(str(r.get(k, "")) for k in keys)
        if k in seen:
            out.append(Issue("DUPLICATE", f"row {i} duplicates row {seen[k]} on {k}", "WARNING", i))
        else:
            seen[k] = i
    return out


def detect_missing_dates(dates: list[dt.date], business_days_only: bool = True) -> list[Issue]:
    """Gaps in a daily series. Weekends excluded when business_days_only."""
    if len(dates) < 2:
        return []
    ds = sorted(set(dates))
    out: list[Issue] = []
    cur = ds[0]
    have = set(ds)
    while cur < ds[-1]:
        cur += dt.timedelta(days=1)
        if business_days_only and cur.weekday() >= 5:
            continue
        if cur not in have:
            out.append(Issue("MISSING_DATE", f"no observation for {cur.isoformat()}", "INFO"))
    return out


def detect_outliers_and_jumps(series: list[tuple[dt.date, float]]) -> list[Issue]:
    """z-score outliers on log-returns + absolute day-on-day jump guard."""
    if len(series) < 10:
        return []
    s = sorted(series, key=lambda x: x[0])
    out: list[Issue] = []
    rets, pairs = [], []
    for (d0, p0), (d1, p1) in zip(s, s[1:]):
        if p0 > 0 and p1 > 0:
            r = (p1 - p0) / p0
            rets.append(r)
            pairs.append((d1, r))
    if not rets:
        return out
    n = len(rets)
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / max(n - 1, 1)
    sd = var ** 0.5
    for d, r in pairs:
        if abs(r) * 100 > MAX_DAILY_JUMP_PCT:
            out.append(Issue("JUMP", f"{d.isoformat()}: day move {r*100:+.2f}% exceeds "
                                    f"{MAX_DAILY_JUMP_PCT}%", "WARNING"))
        elif sd > 0 and abs(r - mean) / sd > OUTLIER_Z:
            out.append(Issue("OUTLIER", f"{d.isoformat()}: return {r*100:+.2f}% is "
                                       f"{abs(r-mean)/sd:.1f} sd from mean", "INFO"))
    return out


def is_stale(last_updated: dt.datetime | None, max_age_hours: int) -> tuple[bool, str]:
    if last_updated is None:
        return True, "no successful update recorded"
    now = dt.datetime.now(dt.timezone.utc)
    if last_updated.tzinfo is None:
        last_updated = last_updated.replace(tzinfo=dt.timezone.utc)
    age = (now - last_updated).total_seconds() / 3600.0
    if age > max_age_hours:
        return True, f"last update {age:.1f}h ago (limit {max_age_hours}h)"
    return False, ""
