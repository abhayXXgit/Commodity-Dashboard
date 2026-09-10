"""
Unit + currency normalisation.

Golden rule: the source's own (price, currency, unit) triple is never mutated.
These helpers only *derive* the analytics value in INR per metric tonne.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

# multiplier to convert "per <unit>" into "per MT"
_TO_MT = {
    "MT": 1.0, "TON": 1.0, "TONNE": 1.0, "T": 1.0, "MTS": 1.0,
    "KG": 1000.0, "KGS": 1000.0,
    "LB": 2204.622622, "LBS": 2204.622622, "POUND": 2204.622622,
    "QUINTAL": 10.0, "QTL": 10.0,
    "G": 1_000_000.0, "GRAM": 1_000_000.0,
}

VALID_CURRENCIES = {"INR", "USD", "EUR", "GBP", "JPY", "CNY", "AUD"}


def f(x, default: float | None = None) -> float | None:
    """Tolerant float cast (handles Decimal, str with commas/₹, None)."""
    if x is None:
        return default
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, Decimal):
        return float(x)
    s = str(x).strip().replace(",", "").replace("₹", "").replace("$", "").replace("₹", "")
    if not s:
        return default
    try:
        return float(s)
    except (ValueError, InvalidOperation):
        return default


def unit_factor(unit: str | None) -> float:
    """
    How many <unit> make one MT. Unknown units raise rather than guess.

    Real workbooks write the unit column every way imaginable - "MT", "kg",
    "INR/MT", "Rs. per KG", "USD/LB". The separators are split on (not deleted,
    which would fuse "INR/KG" into the unparseable "INRKG") and each token is
    checked, so a currency prefix is simply ignored.
    """
    raw = (unit or "MT").strip().upper()
    if raw in _TO_MT:
        return _TO_MT[raw]
    for sep in ("/", "-", ".", ",", "\\"):
        raw = raw.replace(sep, " ")
    tokens = [t for t in raw.split() if t not in ("PER", "RS", "INR", "USD", "EUR", "GBP")]
    for token in tokens:
        if token in _TO_MT:
            return _TO_MT[token]
    raise ValueError(f"Unsupported unit: {unit!r}")


def to_per_mt(price: float, unit: str | None) -> float:
    """Convert a per-<unit> price into a per-MT price."""
    return float(price) * unit_factor(unit)


def convert_currency(amount: float, currency: str, fx_to_inr: float | None) -> float:
    """Convert into INR. fx_to_inr is <currency>/INR (e.g. USDINR = 88.5)."""
    cur = (currency or "INR").strip().upper()
    if cur == "INR":
        return float(amount)
    if not fx_to_inr or fx_to_inr <= 0:
        raise ValueError(f"No FX rate available for {cur}->INR")
    return float(amount) * float(fx_to_inr)


def normalise_price(price, currency: str, unit: str, fx_to_inr: float | None = None) -> float:
    """(price, currency, unit) -> INR per MT. Derived value only."""
    p = f(price)
    if p is None:
        raise ValueError("price is not numeric")
    return convert_currency(to_per_mt(p, unit), currency, fx_to_inr)


def landed_cost(base_inr_mt: float, premium: float = 0.0, freight: float = 0.0,
                tax_pct: float = 0.0) -> float:
    """Landed cost per MT = (base + premium + freight) grossed up by tax."""
    sub = float(base_inr_mt) + float(premium or 0) + float(freight or 0)
    return sub * (1.0 + float(tax_pct or 0) / 100.0)


def inr_compact(value: float) -> str:
    """Indian-style compact formatting: Cr / Lakh / K."""
    v = f(value, 0.0) or 0.0
    a = abs(v)
    sign = "-" if v < 0 else ""
    if a >= 1e7:
        return f"{sign}₹{a/1e7:,.2f} Cr"
    if a >= 1e5:
        return f"{sign}₹{a/1e5:,.2f} L"
    if a >= 1e3:
        return f"{sign}₹{a/1e3:,.1f} K"
    return f"{sign}₹{a:,.0f}"
