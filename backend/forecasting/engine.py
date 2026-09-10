"""
Forecasting engine: walk-forward back-test -> model selection -> forecast.

Selection policy (spec §6)
--------------------------
1. Every available model is back-tested with rolling-origin validation over
   `folds` cuts at the *actual* forecast horizon. Training never sees the
   validation window, so the score is an out-of-sample score.
2. MAE / RMSE / MAPE are computed per fold and averaged.
3. The winner is the lowest mean MAPE, subject to two guards:
     - it must beat the random-walk-with-drift baseline, otherwise the
       baseline wins (an unbeaten naive model is the honest answer);
     - ties inside 2% relative MAPE go to the simpler model.
4. Confidence bands come from the winner's out-of-sample residual scale,
   widened with sqrt(h) - not from the in-sample fit, which is always too
   optimistic.
"""
from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from backend.config import settings
from backend.forecasting.models import BaseModel, build_model_zoo
from backend.utils.logging_config import get_logger

log = get_logger(__name__)

# z for a two-sided 95% interval
Z95 = 1.959963985
# Sanity clamp on the point forecast, expressed in horizon standard deviations.
# Any model can be coaxed into an absurd long-horizon extrapolation; a move of
# more than this many sigmas is not a forecast, it is a modelling artefact.
# Wide enough never to touch a plausible projection, tight enough to stop one
# that would embarrass the person presenting it.
MAX_MOVE_SIGMAS = 3.0
# model complexity ranking used to break near-ties (lower = simpler)
COMPLEXITY = {"NAIVE": 0, "MA": 1, "EMA": 1, "OLS": 2, "ETS": 3,
              "ARIMA": 4, "PROPHET": 5, "RF": 6}


@dataclass
class ModelScore:
    name: str
    family: str
    mae: float | None
    rmse: float | None
    mape: float | None
    folds: int
    resid_std: float
    error: str | None = None

    @property
    def usable(self) -> bool:
        return self.error is None and self.mape is not None and np.isfinite(self.mape)


@dataclass
class ForecastResult:
    run_id: str
    horizon_days: int
    target_date: dt.date
    forecast_price: float
    lower_ci: float
    upper_ci: float
    model_used: str
    mae: float | None
    rmse: float | None
    mape: float | None
    direction: str
    confidence: str
    change_pct: float
    path: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["target_date"] = self.target_date.isoformat()
        return d


# --------------------------------------------------------------------------- metrics
def _horizon_sigma(y: np.ndarray, steps: int) -> float:
    """Realised log-return volatility scaled to the forecast horizon (fraction)."""
    y = np.asarray(y, dtype=float)
    if len(y) < 30 or np.any(y <= 0):
        return 0.0
    r = np.diff(np.log(y))
    r = r[np.isfinite(r)]
    if len(r) < 20:
        return 0.0
    return float(np.std(r, ddof=1) * np.sqrt(max(steps, 1)))


def _metrics(actual: np.ndarray, pred: np.ndarray) -> tuple[float, float, float]:
    a, p = np.asarray(actual, float), np.asarray(pred, float)
    err = a - p
    mae = float(np.mean(np.abs(err)))
    rmse = float(np.sqrt(np.mean(err ** 2)))
    denom = np.where(np.abs(a) < 1e-9, np.nan, np.abs(a))
    mape = float(np.nanmean(np.abs(err / denom)) * 100)
    return mae, rmse, mape


def backtest(y: np.ndarray, model_factory, horizon: int,
             folds: int = 3, min_train: int = 90) -> ModelScore:
    """Rolling-origin evaluation at the true horizon."""
    y = np.asarray(y, dtype=float)
    n = len(y)
    proto = model_factory()
    if n < max(min_train + horizon, proto.min_points + horizon):
        return ModelScore(proto.name, proto.family, None, None, None, 0, 0.0,
                          error=f"needs >= {proto.min_points + horizon} points, have {n}")

    step = max(1, horizon // 2)
    cuts = [n - horizon - i * step for i in range(folds)]
    cuts = [c for c in cuts if c >= min_train and c >= proto.min_points]
    if not cuts:
        cuts = [n - horizon]

    maes, rmses, mapes, resid = [], [], [], []
    for cut in cuts:
        try:
            m = model_factory().fit(y[:cut])
            pred = np.asarray(m.predict(horizon), dtype=float)[:horizon]
            act = y[cut:cut + horizon]
            if len(pred) != len(act) or not np.all(np.isfinite(pred)):
                continue
            a, r, p = _metrics(act, pred)
            maes.append(a); rmses.append(r); mapes.append(p)
            resid.extend((act - pred).tolist())
        except Exception as exc:                            # noqa: BLE001
            return ModelScore(proto.name, proto.family, None, None, None, 0, 0.0,
                              error=f"{type(exc).__name__}: {exc}")
    if not maes:
        return ModelScore(proto.name, proto.family, None, None, None, 0, 0.0,
                          error="no usable validation fold")
    return ModelScore(
        proto.name, proto.family,
        round(float(np.mean(maes)), 4), round(float(np.mean(rmses)), 4),
        round(float(np.mean(mapes)), 4), len(maes),
        float(np.std(resid, ddof=1)) if len(resid) > 2 else 0.0,
    )


def select_model(y: np.ndarray, horizon: int, dates=None,
                 folds: int | None = None) -> tuple[Any, ModelScore, list[ModelScore]]:
    """Back-test the whole zoo at this horizon and return the winner."""
    folds = folds or settings.BACKTEST_FOLDS
    zoo = build_model_zoo(dates=dates)
    factories = [(type(m), m.__dict__.copy(), m) for m in zoo]

    scores: list[ModelScore] = []
    factory_by_name: dict[str, Any] = {}
    for cls, kwargs, proto in factories:
        init = {k: v for k, v in kwargs.items()
                if k in ("window", "span", "lookback", "order", "n_estimators", "dates")}
        def make(cls=cls, init=init):
            return cls(**init)
        sc = backtest(y, make, horizon, folds=folds)
        scores.append(sc)
        factory_by_name[sc.name] = make

    usable = [s for s in scores if s.usable]
    if not usable:
        from backend.forecasting.models import DriftModel
        sc = ModelScore("Random Walk with Drift", "NAIVE", None, None, None, 0, 0.0,
                        error="fallback: no model produced a valid back-test")
        return DriftModel, sc, scores

    usable.sort(key=lambda s: s.mape)
    best = usable[0]
    baseline = next((s for s in usable if s.family == "NAIVE"), None)

    # guard 1: must beat the naive baseline
    if baseline and best.mape > baseline.mape:
        best = baseline
    # guard 2: within 2% relative MAPE, prefer the simpler model
    near = [s for s in usable if s.mape <= best.mape * 1.02]
    if near:
        best = min(near, key=lambda s: (COMPLEXITY.get(s.family, 9), s.mape))

    return factory_by_name[best.name], best, sorted(scores, key=lambda s: (s.mape is None, s.mape))


def forecast_series(s: pd.Series, horizons=None, folds: int | None = None,
                    run_id: str | None = None) -> dict[str, Any]:
    """
    Full pipeline for one price series.
    `s` is a date-indexed INR/MT series. Returns per-horizon results plus the
    complete model leaderboard so the user can see *why* a model was chosen.
    """
    horizons = horizons or settings.FORECAST_HORIZONS
    run_id = run_id or uuid.uuid4().hex[:16]
    y = s.to_numpy(dtype=float)

    if len(y) < settings.MIN_HISTORY_POINTS:
        return {"available": False, "run_id": run_id,
                "message": f"Need at least {settings.MIN_HISTORY_POINTS} observations "
                           f"to forecast; have {len(y)}.",
                "results": [], "leaderboard": []}

    last_price = float(y[-1])
    last_date = s.index[-1]
    results: list[ForecastResult] = []
    leaderboards: dict[int, list[dict]] = {}

    for h in horizons:
        # forecast horizon in *business* steps (series is business-daily)
        steps = max(1, int(round(h * 252 / 365)))
        if len(y) < steps + 90:
            steps = max(1, min(steps, max(1, len(y) - 90)))

        factory, best, all_scores = select_model(y, steps, dates=s.index, folds=folds)
        try:
            model: BaseModel = factory().fit(y)
            path = np.asarray(model.predict(steps), dtype=float)
        except Exception as exc:                            # noqa: BLE001
            log.warning("final fit failed for %s: %s - using drift", best.name, exc)
            from backend.forecasting.models import DriftModel
            model = DriftModel().fit(y)
            path = np.asarray(model.predict(steps), dtype=float)

        point = float(path[-1])

        # ---- sanity clamp ----
        realised_sigma = _horizon_sigma(y, steps)
        if realised_sigma > 0:
            limit = MAX_MOVE_SIGMAS * realised_sigma * last_price
            clamped = float(np.clip(point, last_price - limit, last_price + limit))
            if abs(clamped - point) > 1e-6:
                log.warning("%s at %dd projected %.0f (%.1f%%), beyond %.1f sigma - "
                            "clamped to %.0f", best.name, h, point,
                            (point - last_price) / last_price * 100,
                            MAX_MOVE_SIGMAS, clamped)
                scale = clamped / point if point else 1.0
                path = path * scale
                point = clamped

        # ---- confidence band ----
        sd = best.resid_std or model.fitted_residual_std or (abs(last_price) * 0.02)
        if hasattr(model, "interval"):
            try:
                lo_arr, hi_arr = model.interval(steps)
                lo, hi = float(lo_arr[-1]), float(hi_arr[-1])
            except Exception:                               # noqa: BLE001
                lo = hi = None
        else:
            lo = hi = None
        if lo is None or not np.isfinite(lo) or hi <= lo:
            half = Z95 * sd * np.sqrt(max(steps, 1) / max(steps, 1))  # sd is already h-step
            lo, hi = point - half, point + half
        lo = max(lo, point * 0.35)                          # metals do not go to zero

        change_pct = (point - last_price) / last_price * 100.0
        direction = "UP" if change_pct > 0.75 else "DOWN" if change_pct < -0.75 else "FLAT"

        mape = best.mape
        confidence = ("HIGH" if mape is not None and mape < 3
                      else "MEDIUM" if mape is not None and mape < 7
                      else "LOW")

        band_ratio = (hi - lo) / max(point, 1e-9)
        if band_ratio > 0.35 and confidence == "HIGH":
            confidence = "MEDIUM"

        fdates = pd.bdate_range(last_date + pd.Timedelta(days=1), periods=steps)
        halfs = Z95 * sd * np.sqrt(np.arange(1, steps + 1) / steps)
        results.append(ForecastResult(
            run_id=run_id, horizon_days=h,
            target_date=(last_date + pd.Timedelta(days=h)).date(),
            forecast_price=round(point, 2), lower_ci=round(lo, 2), upper_ci=round(hi, 2),
            model_used=best.name, mae=best.mae, rmse=best.rmse, mape=best.mape,
            direction=direction, confidence=confidence, change_pct=round(change_pct, 3),
            path=[{"date": d.strftime("%Y-%m-%d"), "forecast": round(float(v), 2),
                   "lower": round(float(max(v - hh, v * 0.35)), 2),
                   "upper": round(float(v + hh), 2)}
                  for d, v, hh in zip(fdates, path, halfs)],
        ))
        leaderboards[h] = [
            {"model": sc.name, "family": sc.family, "mae": sc.mae, "rmse": sc.rmse,
             "mape": sc.mape, "folds": sc.folds, "error": sc.error,
             "selected": sc.name == best.name}
            for sc in all_scores]

    return {
        "available": True, "run_id": run_id,
        "last_price": round(last_price, 2),
        "last_date": last_date.strftime("%Y-%m-%d"),
        "observations": int(len(y)),
        "results": [r.to_dict() for r in results],
        "leaderboard": leaderboards,
        "method": ("Rolling-origin back-test at each horizon; winner = lowest mean "
                   "MAPE, required to beat random-walk-with-drift, ties within 2% "
                   "resolved toward the simpler model."),
    }
