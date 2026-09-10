"""
Central configuration. Everything secret comes from the environment.
NOTHING here is ever shipped to the browser (see api/routes_meta.py).
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:      # python-dotenv optional
    pass

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
IMPORT_DIR = DATA_DIR / "imports"
HISTORICAL_DIR = DATA_DIR / "historical"
REPORT_DIR = BASE_DIR / "reports"
FRONTEND_DIR = BASE_DIR / "frontend"

for _d in (DATA_DIR, IMPORT_DIR, HISTORICAL_DIR, REPORT_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def _b(key: str, default: bool = False) -> bool:
    return os.getenv(key, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _f(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, default))
    except (TypeError, ValueError):
        return default


def _i(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, default))
    except (TypeError, ValueError):
        return default


class Settings:
    # ---- paths --------------------------------------------------------
    BASE_DIR = BASE_DIR
    DATA_DIR = DATA_DIR
    IMPORT_DIR = IMPORT_DIR
    HISTORICAL_DIR = HISTORICAL_DIR
    REPORT_DIR = REPORT_DIR
    FRONTEND_DIR = FRONTEND_DIR

    # ---- app ----------------------------------------------------------
    APP_NAME = "TransformerProcure Intelligence"
    APP_VERSION = "2.0.0"
    HOST = os.getenv("APP_HOST", "127.0.0.1")
    PORT = _i("APP_PORT", 8000)
    DEBUG = _b("DEBUG", False)
    CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()]

    # ---- database -----------------------------------------------------
    # Defaults to a local SQLite file so the app runs with zero infrastructure.
    # Set DATABASE_URL=postgresql+psycopg://user:pw@host:5432/db for production.
    DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'data' / 'procure.db'}")
    SQL_ECHO = _b("SQL_ECHO", False)

    # ---- data mode ----------------------------------------------------
    # DEMO       -> seeded, clearly-labelled synthetic series (no live claims)
    # LIVE       -> external adapters used where credentials exist
    DATA_MODE = os.getenv("DATA_MODE", "DEMO").upper()
    STALE_AFTER_HOURS = _i("STALE_AFTER_HOURS", 26)

    # ---- external sources (all optional; absent => adapter reports UNAVAILABLE)
    LME_API_URL = os.getenv("LME_API_URL", "")
    LME_API_KEY = os.getenv("LME_API_KEY", "")
    METALS_API_URL = os.getenv("METALS_API_URL", "")
    METALS_API_KEY = os.getenv("METALS_API_KEY", "")
    FX_API_URL = os.getenv("FX_API_URL", "https://api.exchangerate.host/latest")
    FX_API_KEY = os.getenv("FX_API_KEY", "")
    NALCO_CIRCULAR_URL = os.getenv("NALCO_CIRCULAR_URL", "")
    BALCO_CIRCULAR_URL = os.getenv("BALCO_CIRCULAR_URL", "")
    HINDALCO_CIRCULAR_URL = os.getenv("HINDALCO_CIRCULAR_URL", "")
    HTTP_TIMEOUT = _f("HTTP_TIMEOUT", 15.0)
    HTTP_RETRIES = _i("HTTP_RETRIES", 3)
    HTTP_BACKOFF = _f("HTTP_BACKOFF", 1.6)

    # ---- fx fallback (used only to normalise, always logged as ESTIMATED) ---
    FALLBACK_USDINR = _f("FALLBACK_USDINR", 88.50)
    FALLBACK_EURINR = _f("FALLBACK_EURINR", 96.20)

    # ---- scheduler ----------------------------------------------------
    ENABLE_SCHEDULER = _b("ENABLE_SCHEDULER", True)
    # On a fresh database the forecast tables are empty, so every forecast
    # panel would 404 until the first weekly retrain. Train once in the
    # background at startup instead. Disabled in tests, which train explicitly.
    BOOTSTRAP_FORECASTS = _b("BOOTSTRAP_FORECASTS", True)
    DAILY_FETCH_CRON = os.getenv("DAILY_FETCH_CRON", "30 6 * * *")
    WEEKLY_RETRAIN_CRON = os.getenv("WEEKLY_RETRAIN_CRON", "0 3 * * 1")
    MONTHLY_REPORT_CRON = os.getenv("MONTHLY_REPORT_CRON", "0 4 1 * *")

    # ---- analytics defaults -------------------------------------------
    MA_WINDOWS = (7, 30, 90, 200)
    FORECAST_HORIZONS = (7, 30, 90, 180, 365)
    BACKTEST_FOLDS = _i("BACKTEST_FOLDS", 3)
    MIN_HISTORY_POINTS = _i("MIN_HISTORY_POINTS", 60)

    # ---- procurement policy knobs -------------------------------------
    DEFAULT_TAX_PCT = _f("DEFAULT_TAX_PCT", 18.0)
    ALERT_DAY_MOVE_PCT = _f("ALERT_DAY_MOVE_PCT", 2.0)
    ALERT_FORECAST_MOVE_PCT = _f("ALERT_FORECAST_MOVE_PCT", 5.0)
    ALERT_SUPPLIER_SPREAD_PCT = _f("ALERT_SUPPLIER_SPREAD_PCT", 1.5)

    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
    LOG_FILE = os.getenv("LOG_FILE", str(BASE_DIR / "reports" / "app.log"))

    @property
    def is_demo(self) -> bool:
        return self.DATA_MODE == "DEMO"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
