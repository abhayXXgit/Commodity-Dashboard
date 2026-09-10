"""
Masters + DEMO dataset generator.

IMPORTANT / spec §23:
    Every price row created here is written with data_class='DEMO_DATA'.
    It is a *simulated* series produced by a seeded stochastic model. It is
    NOT market data and is never presented as live or verified. The UI shows a
    persistent DEMO DATA banner whenever these rows are in play.

The generator is deterministic (fixed seed) so the dashboard, the forecast
back-tests and the documentation all agree run-to-run.
"""
from __future__ import annotations

import datetime as dt
import math
import random

from sqlalchemy import select

from backend.config import settings
from backend.database import session_scope
from backend.models import (AlertRule, BomItem, Commodity, ExchangeRate,
                            HistoricalPrice, ProcurementCoverage, Product,
                            Provider, PurchaseHistory, SupplierQuote,
                            Transformer, UserSetting)
from backend.utils.logging_config import get_logger
from backend.utils.units import normalise_price

log = get_logger(__name__)

DEMO_CLASS = "DEMO_DATA"
HISTORY_YEARS = 3
SEED = 20260909

# --------------------------------------------------------------------------- masters
COMMODITIES = [
    ("CU",    "Copper",             "BASE_METAL", "LME Cash / BME India"),
    ("AL",    "Aluminium",          "BASE_METAL", "NALCO / BALCO / Hindalco circular"),
    ("CRGO",  "CRGO Electrical Steel", "CORE",    "Import parity / mill offer"),
    ("STEEL", "Mild Steel (Tank)",  "STRUCTURAL", "Domestic HR plate index"),
    ("OIL",   "Transformer Oil",    "FLUID",      "Domestic refiner offer"),
]

PROVIDERS = [
    ("LME",      "London Metal Exchange",      "EXCHANGE", "United Kingdom", "USD", "MT", "CASH",     "API"),
    ("BME",      "BME / Indian Metal Reference","EXCHANGE", "India",          "INR", "MT", "EX_PLANT", "API"),
    ("NALCO",    "National Aluminium Company", "PRODUCER", "India",          "INR", "MT", "EX_PLANT", "CIRCULAR_IMPORT"),
    ("BALCO",    "BALCO / Vedanta",            "PRODUCER", "India",          "INR", "MT", "EX_PLANT", "CIRCULAR_IMPORT"),
    ("HINDALCO", "Hindalco / Aditya Birla",    "PRODUCER", "India",          "INR", "MT", "EX_PLANT", "CIRCULAR_IMPORT"),
    ("MARKET",   "Domestic Market Reference",  "EXCHANGE", "India",          "INR", "MT", "EX_PLANT", "MANUAL"),
    ("INTERNAL", "Internal Purchase (ERP)",    "INTERNAL", "India",          "INR", "MT", "DELIVERED","MANUAL"),
]

PRODUCTS = [
    ("CU", "CU_CATHODE",  "Copper Cathode",        "Grade A / ETP", "LME Grade A cathode, 99.99% Cu"),
    ("CU", "CU_WIREROD",  "Copper Wire Rod",       "ETP 8mm",       "8 mm ETP copper wire rod"),
    ("AL", "AL_INGOT",    "Aluminium Ingot",       "P1020A",        "Primary aluminium ingot 99.70%"),
    ("AL", "AL_WIREROD",  "Aluminium Wire Rod",    "EC Grade",      "9.5 mm EC grade aluminium wire rod"),
    ("AL", "AL_BILLET",   "Aluminium Billet",      "6063",          "Extrusion billet 6063"),
    ("CRGO","CRGO_M4",    "CRGO Lamination",       "M4 / 0.27mm",   "Grain oriented electrical steel"),
    ("STEEL","STEEL_MS",  "MS Plate",              "IS 2062 E250",  "Hot rolled mild steel plate"),
    ("OIL", "OIL_TRF",    "Transformer Oil",       "IS 335 / IEC 60296", "Inhibited mineral insulating oil"),
]

# code -> (start level, long-run mean, annual drift, annual vol, mean-reversion, end anchor)
#
# `end anchor` pins the *last* observation of the simulated path to a plausible
# present-day level. Without it a random walk ends wherever it likes and the
# dashboard opens on an absurd number. The anchor is applied as a smooth
# log-space tilt (see _ou_path) so the path keeps its texture and volatility
# while finishing on a defensible figure.
SERIES_DRIVERS = {
    "LME_CU": (8_450.0,  9_900.0, 0.045, 0.170, 0.85,  9_850.0),   # USD/MT
    "LME_AL": (2_280.0,  2_640.0, 0.038, 0.150, 0.80,  2_650.0),   # USD/MT
    "USDINR": (82.90,      88.60, 0.020, 0.042, 0.55,     88.60),
    "EURINR": (89.40,      96.40, 0.018, 0.055, 0.55,     96.40),
    "CRGO":  (215_000.0, 248_000.0, 0.030, 0.115, 0.70, 248_000.0), # INR/MT
    "STEEL": (58_000.0,   64_500.0, 0.025, 0.135, 0.75,  64_500.0),
    "OIL":   (92_000.0,  104_000.0, 0.028, 0.105, 0.70, 104_000.0),
}

# Indian producer premia over LME-parity aluminium (INR/MT), incl. duty + local premium
PRODUCER_PREMIUM = {"NALCO": 27_500.0, "BALCO": 25_800.0, "HINDALCO": 29_400.0}
# product uplift over ingot (INR/MT) - conversion charge
PRODUCT_UPLIFT = {"AL_INGOT": 0.0, "AL_WIREROD": 14_500.0, "AL_BILLET": 9_200.0}
# copper: Indian cathode premium over LME parity (INR/MT)
CU_INDIA_PREMIUM = 21_500.0
CU_ROD_UPLIFT = 26_000.0


def _business_days(start: dt.date, end: dt.date) -> list[dt.date]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def _ou_path(days: list[dt.date], p0: float, mean: float, drift: float,
             vol: float, kappa: float, end_anchor: float | None = None,
             rng: random.Random | None = None) -> list[float]:
    """
    Ornstein-Uhlenbeck in log-space with drift and rare jumps.
    Produces a mean-reverting series that still trends - the behaviour metal
    spot prices actually show, and enough structure that model selection in the
    forecasting engine is a meaningful exercise rather than a formality.
    """
    n = len(days)
    dtu = 1.0 / 252.0
    log_mean = math.log(mean)
    x = math.log(p0)
    out = []
    for i in range(n):
        # seasonal component: mild Q1/Q4 restocking bias
        seas = 0.012 * math.sin(2 * math.pi * (days[i].timetuple().tm_yday / 365.25))
        shock = rng.gauss(0.0, 1.0) * vol * math.sqrt(dtu)
        if rng.random() < 0.006:                       # ~1.5 jumps/year
            shock += rng.choice([-1, 1]) * rng.uniform(0.020, 0.055)
        x += kappa * (log_mean - x) * dtu + drift * dtu + shock + seas * dtu
        out.append(math.exp(x))

    if end_anchor and out:
        # Smooth log-space tilt: 0 at the first point, full correction at the
        # last. Preserves every return's shape in the early history and leaves
        # realised volatility essentially unchanged.
        gap = math.log(end_anchor) - math.log(out[-1])
        out = [v * math.exp(gap * (i / (n - 1)) ** 1.15) for i, v in enumerate(out)]
    return out


def _stepwise(values: list[float], days: list[dt.date], rng: random.Random,
              min_gap: int = 3, max_gap: int = 8) -> list[float]:
    """
    Indian primary producers publish price *circulars*, not a continuous tick.
    Hold the last published level for 3-8 business days, then re-publish at the
    prevailing parity rounded to the nearest ₹50/MT.
    """
    out, held, next_rev = [], values[0], 0
    for i, v in enumerate(values):
        if i >= next_rev:
            held = round(v / 50.0) * 50.0
            next_rev = i + rng.randint(min_gap, max_gap)
        out.append(held)
    return out


def seed_masters(db) -> dict:
    """Idempotent master data load. Returns lookup dicts."""
    for code, name, cat, bench in COMMODITIES:
        if not db.scalar(select(Commodity).where(Commodity.code == code)):
            db.add(Commodity(code=code, name=name, category=cat, benchmark_source=bench))
    for code, name, ptype, country, cur, unit, basis, mode in PROVIDERS:
        if not db.scalar(select(Provider).where(Provider.code == code)):
            db.add(Provider(code=code, name=name, provider_type=ptype, country=country,
                            default_currency=cur, default_unit=unit, default_basis=basis,
                            ingestion_mode=mode))
    db.flush()
    cm = {c.code: c for c in db.scalars(select(Commodity))}
    for ccode, pcode, name, grade, spec in PRODUCTS:
        if not db.scalar(select(Product).where(Product.code == pcode)):
            db.add(Product(commodity_id=cm[ccode].id, code=pcode, name=name,
                           grade=grade, specification=spec))
    db.flush()
    return {
        "commodity": {c.code: c.id for c in db.scalars(select(Commodity))},
        "provider": {p.code: p.id for p in db.scalars(select(Provider))},
        "product": {p.code: p.id for p in db.scalars(select(Product))},
    }


def seed_alert_rules(db) -> None:
    rules = [
        ("CU_DAY_UP",   "Copper rises more than 2% in one day",        "DAY_MOVE",      "CU", 2.0, "UP",   "WARNING"),
        ("CU_DAY_DOWN", "Copper falls more than 2% in one day",        "DAY_MOVE",      "CU", 2.0, "DOWN", "INFO"),
        ("AL_DAY_UP",   "Aluminium rises more than 2% in one day",     "DAY_MOVE",      "AL", 2.0, "UP",   "WARNING"),
        ("AL_DAY_DOWN", "Aluminium falls more than 2% in one day",     "DAY_MOVE",      "AL", 2.0, "DOWN", "INFO"),
        ("PCTL_HIGH",   "Price reaches the 90th historical percentile","PERCENTILE",    None, 90.0, "UP",   "WARNING"),
        ("PCTL_LOW",    "Price reaches the 10th historical percentile","PERCENTILE",    None, 10.0, "DOWN", "INFO"),
        ("FC_UP",       "Forecast increase greater than 5%",           "FORECAST_MOVE", None, 5.0, "UP",   "CRITICAL"),
        ("FC_DOWN",     "Forecast decrease greater than 5%",           "FORECAST_MOVE", None, 5.0, "DOWN", "INFO"),
        ("SUP_SPREAD",  "Supplier price differs >1.5% from benchmark", "SUPPLIER_SPREAD",None,1.5, "BOTH", "WARNING"),
        ("STALE_LIVE",  "Live data unavailable or stale",              "STALE",         None, 26.0, "BOTH", "CRITICAL"),
        ("QUOTE_EXP",   "Supplier quotation expired or expiring",      "QUOTE_EXPIRY",  None, 7.0, "BOTH", "WARNING"),
    ]
    for code, desc, rtype, ccode, thr, direction, sev in rules:
        if not db.scalar(select(AlertRule).where(AlertRule.alert_code == code)):
            db.add(AlertRule(alert_code=code, description=desc, rule_type=rtype,
                             commodity_code=ccode, threshold=thr, direction=direction,
                             severity=sev))


def seed_settings(db) -> None:
    defaults = [
        ("data_mode", settings.DATA_MODE, "string", "DEMO or LIVE"),
        ("base_currency", "INR", "string", "Reporting currency"),
        ("base_unit", "MT", "string", "Reporting unit"),
        ("default_tax_pct", str(settings.DEFAULT_TAX_PCT), "float", "GST used in landed cost"),
        ("target_cover_pct", "70", "float", "Policy coverage target before delivery"),
        ("budget_price_CU", "890000", "float", "Approved budget rate, copper INR/MT"),
        ("budget_price_AL", "292000", "float", "Approved budget rate, aluminium INR/MT"),
        ("risk_appetite", "MEDIUM", "string", "LOW / MEDIUM / HIGH"),
    ]
    for k, v, t, d in defaults:
        if not db.scalar(select(UserSetting).where(UserSetting.setting_key == k)):
            db.add(UserSetting(setting_key=k, setting_value=v, value_type=t, description=d))


# --------------------------------------------------------------------------- price history
def generate_price_history(db, ids: dict, end: dt.date | None = None) -> int:
    end = end or dt.date.today()
    start = end - dt.timedelta(days=int(365.25 * HISTORY_YEARS))
    days = _business_days(start, end)
    # one independent stream per series -> reproducible and order-independent
    paths = {k: _ou_path(days, *v, rng=random.Random(SEED + i * 101))
             for i, (k, v) in enumerate(SERIES_DRIVERS.items())}

    # ---- FX first (needed to normalise USD series) ----
    n_fx = 0
    for i, d in enumerate(days):
        for base, key in (("USD", "USDINR"), ("EUR", "EURINR")):
            db.add(ExchangeRate(rate_date=d, base_currency=base, quote_currency="INR",
                                rate=round(paths[key][i], 4),
                                source="Seeded demo FX path", data_class=DEMO_CLASS))
            n_fx += 1
    db.flush()

    usdinr = paths["USDINR"]
    rows: list[HistoricalPrice] = []

    def add(d, ccode, pvcode, prcode, price, currency, unit, basis, fx=None,
            grade=None, premium=0.0, location=None, ohlc=None):
        inr_mt = normalise_price(price, currency, unit, fx)
        o, h, l, c = ohlc or (None, None, None, None)
        rows.append(HistoricalPrice(
            price_date=d, commodity_id=ids["commodity"][ccode],
            provider_id=ids["provider"][pvcode],
            product_id=ids["product"].get(prcode), grade=grade,
            price=round(price, 4), currency=currency, unit=unit,
            open_price=o, high_price=h, low_price=l, close_price=c,
            price_basis=basis, location=location, premium=premium,
            price_inr_mt=round(inr_mt, 2), fx_rate_used=fx,
            effective_date=d, source=f"DEMO simulator (seed {SEED})",
            source_url=None, data_class=DEMO_CLASS, validation_status="PASS",
            data_timestamp=dt.datetime.now(dt.timezone.utc),
        ))

    lme_cu, lme_al = paths["LME_CU"], paths["LME_AL"]

    # producer circular levels: LME parity + producer premium, published stepwise.
    # The premium itself drifts (freight, duty, smelter allocation, local demand)
    # so the inter-producer spread is a live quantity, not a constant.
    parity_al = [lme_al[i] * usdinr[i] for i in range(len(days))]
    producer_steps = {}
    for pi, p in enumerate(("NALCO", "BALCO", "HINDALCO")):
        prng = random.Random(SEED + 313 * (pi + 1))
        phase = prng.uniform(0, 2 * math.pi)
        cycle = prng.uniform(70, 150)          # business days per premium cycle
        amp = PRODUCER_PREMIUM[p] * prng.uniform(0.10, 0.18)
        wobble, w = [], 0.0
        for i in range(len(days)):
            w = 0.92 * w + prng.gauss(0, 1) * 900.0      # AR(1) idiosyncratic drift
            prem = (PRODUCER_PREMIUM[p]
                    + amp * math.sin(2 * math.pi * i / cycle + phase)
                    + w)
            wobble.append(max(prem, PRODUCER_PREMIUM[p] * 0.45))
        producer_steps[p] = _stepwise([parity_al[i] + wobble[i] for i in range(len(days))],
                                      days, random.Random(SEED + hash(p) % 1000))
    cu_india = _stepwise([lme_cu[i] * usdinr[i] + CU_INDIA_PREMIUM for i in range(len(days))],
                         days, random.Random(SEED + 7), min_gap=1, max_gap=2)

    for i, d in enumerate(days):
        fx = round(usdinr[i], 4)

        # --- copper: LME cash in USD (source currency preserved) ---
        cu = lme_cu[i]
        rngd = random.Random(SEED * 31 + i)
        hi = cu * (1 + abs(rngd.gauss(0, 0.004)))
        lo = cu * (1 - abs(rngd.gauss(0, 0.004)))
        op = lo + (hi - lo) * rngd.random()
        add(d, "CU", "LME", "CU_CATHODE", cu, "USD", "MT", "CASH", fx,
            grade="Grade A", ohlc=(round(op, 2), round(hi, 2), round(lo, 2), round(cu, 2)))

        # --- copper: Indian reference + wire rod (INR) ---
        add(d, "CU", "BME", "CU_CATHODE", cu_india[i], "INR", "MT", "EX_PLANT", None,
            grade="ETP", premium=CU_INDIA_PREMIUM, location="Mumbai")
        add(d, "CU", "BME", "CU_WIREROD", cu_india[i] + CU_ROD_UPLIFT, "INR", "MT",
            "EX_PLANT", None, grade="ETP 8mm", premium=CU_INDIA_PREMIUM + CU_ROD_UPLIFT,
            location="Mumbai")

        # --- aluminium: LME cash (USD) ---
        add(d, "AL", "LME", "AL_INGOT", lme_al[i], "USD", "MT", "CASH", fx, grade="P1020A")

        # --- aluminium: three Indian producers x three products (INR) ---
        for pv in ("NALCO", "BALCO", "HINDALCO"):
            base = producer_steps[pv][i]
            loc = {"NALCO": "Angul", "BALCO": "Korba", "HINDALCO": "Hirakud"}[pv]
            for prod, uplift in PRODUCT_UPLIFT.items():
                grade = {"AL_INGOT": "P1020A", "AL_WIREROD": "EC Grade",
                         "AL_BILLET": "6063"}[prod]
                add(d, "AL", pv, prod, base + uplift, "INR", "MT", "EX_PLANT", None,
                    grade=grade, premium=PRODUCER_PREMIUM[pv] + uplift, location=loc)

        # --- other BOM commodities (INR) ---
        add(d, "CRGO", "MARKET", "CRGO_M4", paths["CRGO"][i], "INR", "MT", "DELIVERED",
            None, grade="M4 0.27mm")
        add(d, "STEEL", "MARKET", "STEEL_MS", paths["STEEL"][i], "INR", "MT", "DELIVERED",
            None, grade="IS 2062 E250")
        add(d, "OIL", "MARKET", "OIL_TRF", paths["OIL"][i], "INR", "MT", "DELIVERED",
            None, grade="IS 335")

    db.add_all(rows)
    db.flush()
    log.info("DEMO history: %d price rows, %d fx rows over %d business days",
             len(rows), n_fx, len(days))
    return len(rows)


# --------------------------------------------------------------------------- transformers & BOM
# Consumption model. Anchored on the customer's own reference point:
#   10 MVA power transformer -> 2,850 kg copper, 1,250 kg aluminium.
# Scaled by rating with the usual sub-linear exponents (active material grows
# roughly with rating^0.75-0.85, tank/oil with rating^0.70-0.72).
BOM_ANCHOR_MVA = 10.0
BOM_MODEL = {
    # category: (kg at 10 MVA, scaling exponent, commodity, provider, product, basis)
    "COPPER_HV": (1_950.0, 0.80, "CU", "BME", "CU_WIREROD", "Copper HV winding conductor"),
    "COPPER_LV": (900.0,   0.80, "CU", "BME", "CU_CATHODE", "Copper LV winding / leads"),
    "ALUMINIUM": (1_250.0, 0.80, "AL", "NALCO", "AL_WIREROD", "Aluminium winding & structural"),
    "CRGO":      (3_200.0, 0.75, "CRGO", "MARKET", "CRGO_M4", "CRGO core lamination"),
    "STEEL":     (4_500.0, 0.70, "STEEL", "MARKET", "STEEL_MS", "Tank, radiator & structural steel"),
    "OIL":       (3_000.0, 0.72, "OIL", "MARKET", "OIL_TRF", "Transformer insulating oil"),
}

PROJECTS = [
    # job,     customer,                      type,     MVA, ratio,      qty, months_out, status
    ("PT-001", "MSEDCL",                      "POWER",  10.0, "33/11 kV",   5, 2, "WIP"),
    ("PT-002", "MSETCL",                      "POWER",  25.0, "132/33 kV",  3, 4, "DESIGN"),
    ("PT-003", "PGCIL",                       "SOLAR",  63.0, "220/33 kV",  4, 7, "PLANNED"),
    ("PT-004", "GETCO",                       "POWER",  16.0, "66/11 kV",   6, 3, "WIP"),
    ("PT-005", "NTPC Renewable Energy Ltd",   "SOLAR",  50.0, "220/33 kV",  8, 9, "PLANNED"),
]

VECTOR_GROUPS = {"POWER": "YNyn0", "SOLAR": "Dyn11"}


def _bom_kg(anchor_kg: float, exponent: float, mva: float) -> float:
    return anchor_kg * (mva / BOM_ANCHOR_MVA) ** exponent


def seed_transformers(db, ids: dict, latest_rates: dict[str, float]) -> int:
    """Five live jobs with a full commodity-linked BOM each."""
    if db.scalar(select(Transformer).limit(1)):
        return 0
    today = dt.date.today()
    made = 0
    for job, cust, ttype, mva, ratio, qty, months, status in PROJECTS:
        hv, lv = (float(x) for x in ratio.replace(" kV", "").split("/"))
        # base cost per transformer excluding the commodity revision, ~₹32 L per 10 MVA
        base_cost = 3_200_000.0 * (mva / BOM_ANCHOR_MVA) ** 0.78
        t = Transformer(
            job_no=job, customer=cust, transformer_type=ttype, rating_mva=mva,
            voltage_ratio=ratio, hv_kv=hv, lv_kv=lv, frequency_hz=50,
            vector_group=VECTOR_GROUPS[ttype], quantity=qty,
            delivery_date=today + dt.timedelta(days=30 * months),
            mfg_status=status, costing_version="CV-2026.1", bom_version="BOM-2.0",
            base_cost_inr=round(base_cost, 2),
        )
        db.add(t)
        db.flush()

        for key, (anchor, expo, ccode, pvcode, prcode, label) in BOM_MODEL.items():
            kg = _bom_kg(anchor, expo, mva)
            mt = kg / 1000.0
            # BOM rate = the rate frozen at costing time; deliberately a few
            # percent away from today so BOM-vs-market variance is meaningful.
            mkt = latest_rates.get(prcode) or latest_rates.get(ccode) or 0.0
            bom_rate = mkt * (0.955 if ccode in ("CU", "AL") else 0.975)
            cat = "COPPER" if ccode == "CU" else "ALUMINIUM" if ccode == "AL" else ccode
            db.add(BomItem(
                transformer_id=t.id, bom_item=label, material_category=cat,
                material=label, grade={"CU": "ETP", "AL": "EC Grade"}.get(ccode, ""),
                specification=f"{label} for {mva} MVA {ratio}",
                quantity=round(kg, 2), unit="KG", unit_weight_kg=round(kg, 2),
                total_weight_mt=round(mt, 6),
                commodity_id=ids["commodity"][ccode],
                provider_id=ids["provider"][pvcode],
                product_id=ids["product"][prcode],
                price_basis="EX_PLANT", bom_rate_inr_mt=round(bom_rate, 2),
                premium_inr_mt=0, currency="INR",
                supplier={"CU": "Vedanta Ltd", "AL": "NALCO"}.get(ccode, "Approved vendor"),
                po_number=None,
                remarks="Consumption scaled from 10 MVA reference design",
            ))
        made += 1
    db.flush()
    log.info("Seeded %d transformer jobs with commodity-linked BOM", made)
    return made


def seed_coverage_and_purchases(db, ids: dict, latest_rates: dict[str, float]) -> None:
    """Stock / open PO / confirmed supply, plus 18 months of PO history."""
    if db.scalar(select(ProcurementCoverage).limit(1)):
        return
    cu_req = al_req = 0.0
    for t in db.scalars(select(Transformer)):
        for b in t.bom_items:
            if b.material_category == "COPPER":
                cu_req += float(b.total_weight_mt) * t.quantity
            elif b.material_category == "ALUMINIUM":
                al_req += float(b.total_weight_mt) * t.quantity

    today = dt.date.today()
    db.add(ProcurementCoverage(
        commodity_id=ids["commodity"]["CU"], as_of_date=today,
        stock_mt=round(cu_req * 0.17, 3), open_po_mt=round(cu_req * 0.38, 3),
        confirmed_mt=round(cu_req * 0.09, 3),
        avg_po_rate_inr_mt=round(latest_rates.get("CU_CATHODE", 890000) * 0.968, 2),
        supplier_lead_days=45, remarks="Cathode + rod pooled"))
    db.add(ProcurementCoverage(
        commodity_id=ids["commodity"]["AL"], as_of_date=today,
        stock_mt=round(al_req * 0.24, 3), open_po_mt=round(al_req * 0.31, 3),
        confirmed_mt=round(al_req * 0.12, 3),
        avg_po_rate_inr_mt=round(latest_rates.get("AL_WIREROD", 276000) * 0.981, 2),
        supplier_lead_days=30, remarks="Wire rod, NALCO allocation"))

    # Remaining BOM materials are procured too - track them so the coverage
    # view is complete rather than silently blank.
    other = {"CRGO": (0.21, 0.34, 0.07, 60), "STEEL": (0.30, 0.28, 0.10, 25),
             "OIL": (0.26, 0.30, 0.08, 21)}
    for ccode, (st, po, cf, lead) in other.items():
        req = 0.0
        for t in db.scalars(select(Transformer)):
            for b in t.bom_items:
                if b.commodity_id == ids["commodity"][ccode]:
                    req += float(b.total_weight_mt) * t.quantity
        if req <= 0:
            continue
        prod = {"CRGO": "CRGO_M4", "STEEL": "STEEL_MS", "OIL": "OIL_TRF"}[ccode]
        db.add(ProcurementCoverage(
            commodity_id=ids["commodity"][ccode], as_of_date=today,
            stock_mt=round(req * st, 3), open_po_mt=round(req * po, 3),
            confirmed_mt=round(req * cf, 3),
            avg_po_rate_inr_mt=round(latest_rates.get(prod, 0) * 0.975, 2) or None,
            supplier_lead_days=lead, remarks="Seeded demonstration coverage"))

    rng = random.Random(SEED + 99)
    for m in range(18, 0, -1):
        d = today - dt.timedelta(days=30 * m)
        for ccode, prcode, base, supplier in (
                ("CU", "CU_CATHODE", latest_rates.get("CU_CATHODE", 890000), "Vedanta Ltd"),
                ("AL", "AL_WIREROD", latest_rates.get("AL_WIREROD", 276000), "NALCO")):
            db.add(PurchaseHistory(
                po_number=f"PO/{ccode}/{d:%Y%m}/{rng.randint(100,999)}", po_date=d,
                commodity_id=ids["commodity"][ccode], product_id=ids["product"][prcode],
                supplier_name=supplier,
                quantity_mt=round(rng.uniform(2.5, 9.0), 3),
                rate_inr_mt=round(base * rng.uniform(0.86, 1.03), 2),
                basis="DELIVERED", data_class=DEMO_CLASS))


def seed_supplier_quotes(db, ids: dict, latest_rates: dict[str, float]) -> None:
    if db.scalar(select(SupplierQuote).limit(1)):
        return
    today = dt.date.today()
    rng = random.Random(SEED + 5)
    quotes = [
        # supplier,             ccode, product,      spread vs market, validity days, freight, moq, terms, lead
        ("Vedanta Ltd",          "CU", "CU_CATHODE",  -0.008, 12, 3_200, 5.0,  "30 days credit",  40),
        ("Hindalco Industries",  "CU", "CU_WIREROD",   0.014, 20, 4_100, 3.0,  "Advance",         35),
        ("Sterlite Copper",      "CU", "CU_CATHODE",   0.021, -4, 2_900, 10.0, "45 days credit",  55),
        ("NALCO",                "AL", "AL_WIREROD",  -0.011, 25, 2_400, 5.0,  "Advance",         28),
        ("BALCO / Vedanta",      "AL", "AL_INGOT",     0.006, 18, 2_650, 8.0,  "15 days credit",  32),
        ("Hindalco / Aditya Birla","AL","AL_WIREROD",  0.018, 30, 2_200, 4.0,  "30 days credit",  30),
        ("Bharat Aluminium Trd.", "AL", "AL_BILLET",  -0.004, -2, 3_050, 2.0,  "Advance",         25),
    ]
    for sup, ccode, prcode, spread, valid_days, freight, moq, terms, lead in quotes:
        mkt = latest_rates.get(prcode, 0.0)
        price = mkt * (1 + spread)
        qd = today - dt.timedelta(days=rng.randint(1, 9))
        db.add(SupplierQuote(
            quote_ref=f"Q/{ccode}/{qd:%y%m%d}/{rng.randint(10,99)}",
            supplier_name=sup, commodity_id=ids["commodity"][ccode],
            product_id=ids["product"][prcode], quote_date=qd,
            valid_until=qd + dt.timedelta(days=abs(valid_days)) if valid_days > 0
            else today + dt.timedelta(days=valid_days),
            price=round(price, 2), currency="INR", unit="MT",
            freight=freight, premium=0, tax_pct=settings.DEFAULT_TAX_PCT,
            moq_mt=moq, payment_terms=terms, delivery_days=lead,
            price_basis="EX_PLANT", data_class=DEMO_CLASS,
            remarks="Seeded demonstration quotation"))


def latest_rate_map(db, ids: dict) -> dict[str, float]:
    """Most recent normalised INR/MT per product code (and per commodity)."""
    from sqlalchemy import func
    out: dict[str, float] = {}
    rev = {v: k for k, v in ids["product"].items()}
    crev = {v: k for k, v in ids["commodity"].items()}
    sub = (select(HistoricalPrice.product_id,
                  func.max(HistoricalPrice.price_date).label("d"))
           .group_by(HistoricalPrice.product_id).subquery())
    q = (select(HistoricalPrice)
         .join(sub, (HistoricalPrice.product_id == sub.c.product_id) &
                    (HistoricalPrice.price_date == sub.c.d)))
    for row in db.scalars(q):
        code = rev.get(row.product_id)
        if code and row.price_inr_mt:
            # prefer the Indian INR quotation over the USD exchange print
            if code not in out or row.currency == "INR":
                out[code] = float(row.price_inr_mt)
        ccode = crev.get(row.commodity_id)
        if ccode and row.price_inr_mt and ccode not in out:
            out[ccode] = float(row.price_inr_mt)
    return out


def run_seed(force: bool = False) -> dict:
    """Full idempotent bootstrap. Safe to call on every startup."""
    from backend.database import init_db
    init_db()
    with session_scope() as db:
        ids = seed_masters(db)
        seed_alert_rules(db)
        seed_settings(db)
        existing = db.scalar(select(HistoricalPrice).limit(1))
        n_prices = 0
        if force or not existing:
            if force:
                db.query(HistoricalPrice).delete()
                db.query(ExchangeRate).delete()
                db.flush()
            n_prices = generate_price_history(db, ids)
        rates = latest_rate_map(db, ids)
        n_trf = seed_transformers(db, ids, rates)
        seed_coverage_and_purchases(db, ids, rates)
        seed_supplier_quotes(db, ids, rates)
        return {"prices": n_prices, "transformers": n_trf,
                "latest_rates": {k: round(v, 2) for k, v in sorted(rates.items())}}


if __name__ == "__main__":
    import json
    print(json.dumps(run_seed(force="--force" in __import__("sys").argv), indent=2, default=str))
