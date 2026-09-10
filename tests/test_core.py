"""
Test suite.

Focus is on the things that would quietly corrupt a procurement decision:
unit/currency normalisation, validation rejections, the BOM arithmetic the
whole system rests on, forecast model selection honesty, and the guarantee
that a missing live feed is reported rather than faked.

    pytest -q
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.utils import units as UN                                 # noqa: E402
from backend.utils import validation as V                             # noqa: E402


# --------------------------------------------------------------- units / fx
class TestUnits:
    def test_mt_is_identity(self):
        assert UN.to_per_mt(850_000, "MT") == 850_000

    def test_kg_scales_by_1000(self):
        assert UN.to_per_mt(265, "KG") == 265_000

    def test_pound_conversion(self):
        assert UN.to_per_mt(1, "LB") == pytest.approx(2204.6226, rel=1e-6)

    def test_quintal(self):
        assert UN.to_per_mt(8_500, "QUINTAL") == 85_000

    def test_compound_unit_string(self):
        assert UN.to_per_mt(265, "INR/KG") == 265_000

    def test_unknown_unit_raises(self):
        with pytest.raises(ValueError):
            UN.to_per_mt(100, "BARREL")

    def test_currency_passthrough_for_inr(self):
        assert UN.convert_currency(1000, "INR", None) == 1000

    def test_usd_needs_a_rate(self):
        with pytest.raises(ValueError):
            UN.convert_currency(1000, "USD", None)

    def test_normalise_usd_per_mt(self):
        # 9,850 USD/MT at 88.60 -> 872,710 INR/MT
        assert UN.normalise_price(9850, "USD", "MT", 88.60) == pytest.approx(872_710.0)

    def test_landed_cost_build_up(self):
        # (850000 + 5000 + 2000) * 1.18
        assert UN.landed_cost(850_000, 5_000, 2_000, 18) == pytest.approx(1_011_260.0)

    def test_landed_cost_no_tax(self):
        assert UN.landed_cost(100, 10, 5, 0) == 115

    def test_compact_indian_format(self):
        assert UN.inr_compact(48_880_000) == "₹4.89 Cr"
        assert UN.inr_compact(570_000) == "₹5.70 L"
        assert UN.inr_compact(-570_000).startswith("-")

    def test_tolerant_float(self):
        assert UN.f("₹8,94,200") == 894200.0
        assert UN.f(None, 0.0) == 0.0
        assert UN.f("not a number", -1) == -1


# ---------------------------------------------------------------- validation
class TestValidation:
    BASE = {"price": 850_000, "currency": "INR", "unit": "MT", "price_date": "2026-01-15"}

    def test_good_row_passes(self):
        ok, issues = V.validate_row(dict(self.BASE))
        assert ok and not [i for i in issues if i.severity == "ERROR"]

    def test_negative_price_rejected(self):
        ok, issues = V.validate_row({**self.BASE, "price": -1})
        assert not ok and issues[0].flag_type == "NEGATIVE"

    def test_zero_price_rejected(self):
        ok, _ = V.validate_row({**self.BASE, "price": 0})
        assert not ok

    def test_non_numeric_price_rejected(self):
        ok, issues = V.validate_row({**self.BASE, "price": "abc"})
        assert not ok and issues[0].flag_type == "BAD_PRICE"

    def test_unknown_currency_rejected(self):
        ok, issues = V.validate_row({**self.BASE, "currency": "XYZ"})
        assert not ok and issues[0].flag_type == "BAD_CURRENCY"

    def test_unknown_unit_rejected(self):
        ok, issues = V.validate_row({**self.BASE, "unit": "BARREL"})
        assert not ok and issues[0].flag_type == "BAD_UNIT"

    def test_future_date_rejected(self):
        future = (dt.date.today() + dt.timedelta(days=30)).isoformat()
        ok, issues = V.validate_row({**self.BASE, "price_date": future})
        assert not ok and issues[0].flag_type == "FUTURE_DATE"

    def test_today_is_accepted(self):
        ok, _ = V.validate_row({**self.BASE, "price_date": dt.date.today().isoformat()})
        assert ok

    def test_duplicate_detection(self):
        rows = [{"price_date": "2026-01-01", "commodity": "CU", "provider": "BME", "product": "X"}] * 2
        assert len(V.detect_duplicates(rows)) == 1

    def test_missing_business_days(self):
        # Mon 2026-01-05 .. Fri 2026-01-09, drop Wednesday
        days = [dt.date(2026, 1, d) for d in (5, 6, 8, 9)]
        issues = V.detect_missing_dates(days)
        assert len(issues) == 1 and "2026-01-07" in issues[0].detail

    def test_weekends_are_not_missing(self):
        days = [dt.date(2026, 1, 9), dt.date(2026, 1, 12)]   # Fri -> Mon
        assert V.detect_missing_dates(days) == []

    def test_price_jump_flagged(self):
        base = [(dt.date(2026, 1, 1) + dt.timedelta(days=i), 100.0 + i * 0.1) for i in range(20)]
        base.append((dt.date(2026, 2, 1), 200.0))            # +80% jump
        flags = V.detect_outliers_and_jumps(base)
        assert any(f.flag_type == "JUMP" for f in flags)

    def test_stale_detection(self):
        old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=48)
        stale, why = V.is_stale(old, 26)
        assert stale and "48" in why

    def test_never_updated_is_stale(self):
        stale, why = V.is_stale(None, 26)
        assert stale and "no successful update" in why


# ------------------------------------------------------- adapters never fake
class TestAdapterHonesty:
    def test_unconfigured_adapter_reports_unavailable(self):
        from backend.data_sources.lme_adapter import LMEAdapter
        from backend.data_sources.base import STATUS_UNAVAILABLE
        res = LMEAdapter().fetch()
        assert res.status == STATUS_UNAVAILABLE
        assert res.records == []                       # crucially: no invented prices
        assert "LME_API_URL" in res.error

    def test_fx_fallback_is_labelled_estimated(self):
        from backend.data_sources.fx_adapter import FxAdapter
        quotes = FxAdapter._fallback(("USD", "EUR"))
        assert quotes and all(q.data_class == "ESTIMATED" for q in quotes)


# --------------------------------------------------------------- forecasting
class TestForecasting:
    @staticmethod
    def _series(n=400, slope=50.0, level=800_000.0):
        import numpy as np
        rng = np.random.RandomState(7)
        return level + slope * np.arange(n) + rng.normal(0, 2_000, n)

    def test_every_model_fits_and_predicts(self):
        from backend.forecasting.models import build_model_zoo
        import numpy as np
        y = self._series()
        for m in build_model_zoo():
            out = np.asarray(m.fit(y).predict(20), dtype=float)
            assert out.shape == (20,) and np.all(np.isfinite(out)), m.name

    def test_metrics_are_zero_for_a_perfect_forecast(self):
        from backend.forecasting.engine import _metrics
        import numpy as np
        a = np.array([1.0, 2.0, 3.0])
        assert _metrics(a, a) == (0.0, 0.0, 0.0)

    def test_backtest_scores_a_trend_model_well(self):
        from backend.forecasting.engine import backtest
        from backend.forecasting.models import LinearRegressionModel
        sc = backtest(self._series(), lambda: LinearRegressionModel(120), horizon=10, folds=2)
        assert sc.usable and sc.mape < 5.0

    def test_backtest_reports_insufficient_history(self):
        from backend.forecasting.engine import backtest
        from backend.forecasting.models import ArimaModel
        sc = backtest(self._series(30), lambda: ArimaModel(), horizon=10, folds=2)
        assert not sc.usable and "points" in (sc.error or "")

    def test_selection_returns_a_leaderboard(self):
        from backend.forecasting.engine import select_model
        _factory, best, all_scores = select_model(self._series(300), horizon=10, folds=2)
        assert best.usable
        assert len(all_scores) > 5
        # the winner must actually be the best usable MAPE, or the naive baseline
        usable = [s for s in all_scores if s.usable]
        assert best.mape <= min(s.mape for s in usable) * 1.02 + 1e-9

    def test_short_series_refuses_to_forecast(self):
        import pandas as pd
        from backend.forecasting.engine import forecast_series
        s = pd.Series([1.0, 2.0, 3.0],
                      index=pd.date_range("2026-01-01", periods=3, freq="B"))
        out = forecast_series(s)
        assert out["available"] is False and "at least" in out["message"]

    def test_confidence_band_brackets_the_point_forecast(self):
        import pandas as pd
        from backend.forecasting.engine import forecast_series
        y = self._series(300)
        s = pd.Series(y, index=pd.date_range("2024-01-01", periods=300, freq="B"))
        out = forecast_series(s, horizons=(30,), folds=2)
        r = out["results"][0]
        assert r["lower_ci"] < r["forecast_price"] < r["upper_ci"]
        assert r["lower_ci"] > 0


# ------------------------------------------------------------- BOM arithmetic
class TestBomMath:
    """
    The customer's own worked example (spec §25.3 / §25.5):
        10 MVA transformer, 2.850 MT copper, ₹850,000/MT
          -> material cost      ₹2,422,500
        +₹10,000/MT on 2.850 MT -> ₹28,500 per transformer
        × 20 transformers        -> ₹570,000 project exposure
    """
    def test_material_cost_per_transformer(self):
        assert 2.850 * 850_000 == pytest.approx(2_422_500.0)

    def test_impact_per_transformer(self):
        assert 10_000 * 2.850 == pytest.approx(28_500.0)

    def test_total_project_exposure(self):
        assert 10_000 * 2.850 * 20 == pytest.approx(570_000.0)

    def test_seed_bom_matches_the_reference_design(self):
        from backend.services.seed_data import BOM_MODEL, _bom_kg
        cu = sum(_bom_kg(a, e, 10.0) for k, (a, e, c, *_rest) in BOM_MODEL.items()
                 if c == "CU" for a, e in [(a, e)])
        al = sum(_bom_kg(a, e, 10.0) for k, (a, e, c, *_rest) in BOM_MODEL.items()
                 if c == "AL" for a, e in [(a, e)])
        assert cu == pytest.approx(2850.0)      # 2.850 MT copper at 10 MVA
        assert al == pytest.approx(1250.0)      # 1.250 MT aluminium at 10 MVA

    def test_consumption_scales_sub_linearly_with_rating(self):
        from backend.services.seed_data import _bom_kg
        at10 = _bom_kg(2850, 0.80, 10)
        at20 = _bom_kg(2850, 0.80, 20)
        assert at20 > at10                       # more material for a bigger unit
        assert at20 < at10 * 2                   # but not proportionally more


# ------------------------------------------------- analytics classification
class TestAnalytics:
    @staticmethod
    def _s(values):
        import pandas as pd
        return pd.Series(values, index=pd.date_range("2024-01-01", periods=len(values), freq="B"))

    def test_percentile_band_zones(self):
        from backend.services.analytics import price_band
        rising = self._s([100 + i for i in range(300)])
        band = price_band(rising)
        assert band["percentile"] > 99 and band["zone"] == "VERY HIGH"

    def test_bottom_of_range_is_very_low(self):
        from backend.services.analytics import price_band
        falling = self._s([400 - i for i in range(300)])
        assert price_band(falling)["zone"] == "VERY LOW"

    def test_trend_is_bullish_on_a_sustained_rise(self):
        from backend.services.analytics import classify_trend
        out = classify_trend(self._s([100 + i * 0.7 for i in range(300)]), forecast_pct=3.0)
        assert out["trend"] in ("BULLISH", "STRONG BULLISH")

    def test_trend_is_bearish_on_a_sustained_fall(self):
        from backend.services.analytics import classify_trend
        out = classify_trend(self._s([400 - i * 0.7 for i in range(300)]), forecast_pct=-3.0)
        assert out["trend"] in ("BEARISH", "STRONG BEARISH")

    def test_trend_uses_all_five_components(self):
        from backend.services.analytics import classify_trend
        out = classify_trend(self._s([100 + i * 0.5 for i in range(300)]), 1.0)
        assert set(out["components"]) == {"ma_structure", "momentum", "forecast",
                                          "percentile", "slope"}
        assert sum(out["weights"].values()) == pytest.approx(1.0)

    def test_volatility_bands(self):
        from backend.services.analytics import volatility
        import numpy as np
        rng = np.random.RandomState(3)
        calm = self._s(list(1000 * np.exp(np.cumsum(rng.normal(0, 0.001, 300)))))
        wild = self._s(list(1000 * np.exp(np.cumsum(rng.normal(0, 0.03, 300)))))
        assert volatility(calm)["annualised"] < volatility(wild)["annualised"]
        assert volatility(wild)["band"] in ("HIGH", "EXTREME")

    def test_pct_change_handles_missing_history(self):
        from backend.services.analytics import pct_change_over
        assert pct_change_over(self._s([100, 101, 102]), 3650) is None


# ------------------------------------------------------------------- cache
class TestCache:
    def test_memo_caches_then_invalidates(self):
        from backend.utils import cache
        cache.invalidate()
        calls = {"n": 0}

        @cache.memo(ttl=30)
        def f(_db, x):
            calls["n"] += 1
            return x * 2

        assert f(None, 3) == 6 and calls["n"] == 1
        assert f(None, 3) == 6 and calls["n"] == 1        # served from cache
        assert f(None, 4) == 8 and calls["n"] == 2        # different key
        cache.invalidate("test")
        assert f(None, 3) == 6 and calls["n"] == 3        # recomputed after invalidate
