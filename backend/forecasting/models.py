"""
Forecast model zoo.

Every model implements the same tiny contract:

    m = Model(); m.fit(y: np.ndarray); yhat = m.predict(h) -> np.ndarray[h]

so the engine can back-test them uniformly and pick a winner on evidence
rather than on the author's preference (spec §6: "Do NOT blindly select one
model"). Models that need an optional dependency degrade to `available=False`
instead of exploding the request.
"""
from __future__ import annotations

import logging
import warnings

import numpy as np

# statsmodels is chatty about optimiser convergence on short training folds;
# a non-converged fold simply scores badly in the back-test and loses.
warnings.filterwarnings("ignore")
try:
    from statsmodels.tools.sm_exceptions import ConvergenceWarning
    warnings.simplefilter("ignore", ConvergenceWarning)
except Exception:                                          # pragma: no cover
    pass
logging.getLogger("cmdstanpy").setLevel(logging.CRITICAL)
logging.getLogger("prophet").setLevel(logging.CRITICAL)

try:
    from statsmodels.tsa.arima.model import ARIMA as _ARIMA
    from statsmodels.tsa.holtwinters import ExponentialSmoothing as _ETS
    HAS_SM = True
except Exception:                                          # pragma: no cover
    HAS_SM = False

try:
    from sklearn.ensemble import RandomForestRegressor
    HAS_SK = True
except Exception:                                          # pragma: no cover
    HAS_SK = False

try:
    from prophet import Prophet as _Prophet
    HAS_PROPHET = True
except Exception:
    HAS_PROPHET = False


class BaseModel:
    name = "base"
    family = "baseline"
    available = True
    min_points = 20

    def fit(self, y: np.ndarray) -> "BaseModel":
        self.y = np.asarray(y, dtype=float)
        return self

    def predict(self, h: int) -> np.ndarray:
        raise NotImplementedError

    @property
    def fitted_residual_std(self) -> float:
        """Residual scale used for confidence bands. Default: 1-step naive error."""
        y = getattr(self, "y", None)
        if y is None or len(y) < 3:
            return 0.0
        return float(np.std(np.diff(y), ddof=1))


class MovingAverageModel(BaseModel):
    name = "Moving Average"
    family = "MA"

    def __init__(self, window: int = 30):
        self.window = window
        self.name = f"Moving Average ({window}d)"

    def predict(self, h: int) -> np.ndarray:
        w = min(self.window, len(self.y))
        return np.repeat(self.y[-w:].mean(), h)


class EMAModel(BaseModel):
    name = "Exponential Moving Average"
    family = "EMA"

    def __init__(self, span: int = 20):
        self.span = span
        self.name = f"Exponential MA (span {span})"

    def fit(self, y):
        super().fit(y)
        a = 2.0 / (self.span + 1.0)
        lvl = self.y[0]
        for v in self.y[1:]:
            lvl = a * v + (1 - a) * lvl
        self.level = float(lvl)
        return self

    def predict(self, h: int) -> np.ndarray:
        return np.repeat(self.level, h)


class DriftModel(BaseModel):
    """Random walk with drift - the honest baseline any model must beat."""
    name = "Random Walk with Drift"
    family = "NAIVE"

    def predict(self, h: int) -> np.ndarray:
        n = len(self.y)
        drift = (self.y[-1] - self.y[0]) / max(n - 1, 1)
        return self.y[-1] + drift * np.arange(1, h + 1)


class LinearRegressionModel(BaseModel):
    """
    OLS on a time index, fitted on the most recent `lookback` points, with a
    DAMPED extrapolation.

    An undamped fit is the classic way to produce a nonsense long-horizon
    number: a 60-day slope extended over 90+ business days assumes a local
    trend continues unchanged for a quarter, which no metal price does. The
    damping factor phi shrinks each successive step's contribution, so the
    forecast flattens out with distance instead of running away. This is the
    same idea as Holt's damped trend, and it makes the term structure across
    horizons coherent rather than zig-zagging.

        undamped:  y_hat(h) = level + slope * h
        damped:    y_hat(h) = level + slope * sum(phi^i, i = 1..h)
    """
    name = "Linear Regression"
    family = "OLS"
    PHI = 0.98

    def __init__(self, lookback: int = 120, phi: float | None = None):
        self.lookback = lookback
        self.phi = self.PHI if phi is None else phi
        self.name = f"Linear Regression ({lookback}d, damped)"

    def fit(self, y):
        super().fit(y)
        yy = self.y[-self.lookback:]
        x = np.arange(len(yy), dtype=float)
        self.coef = np.polyfit(x, yy, 1)
        self.n_fit = len(yy)
        self.resid = yy - np.polyval(self.coef, x)
        self.level = float(np.polyval(self.coef, len(yy) - 1))
        self.slope = float(self.coef[0])
        return self

    def predict(self, h: int) -> np.ndarray:
        steps = np.arange(1, h + 1)
        # geometric (damped) cumulative trend
        damp = np.cumsum(self.phi ** steps)
        return self.level + self.slope * damp

    @property
    def fitted_residual_std(self) -> float:
        return float(np.std(self.resid, ddof=2)) if len(self.resid) > 3 else 0.0


class ArimaModel(BaseModel):
    name = "ARIMA"
    family = "ARIMA"
    available = HAS_SM
    min_points = 60

    def __init__(self, order=None, candidates=((1, 1, 1), (2, 1, 1), (0, 1, 1), (1, 1, 0))):
        self.order = order
        self.candidates = candidates

    def fit(self, y):
        super().fit(y)
        yy = self.y[-500:]
        best, best_aic = None, np.inf
        orders = [self.order] if self.order else self.candidates
        for o in orders:
            try:
                res = _ARIMA(yy, order=o, enforce_stationarity=False,
                             enforce_invertibility=False).fit()
                if res.aic < best_aic:
                    best, best_aic, self.order = res, res.aic, o
            except Exception:                              # noqa: BLE001
                continue
        if best is None:
            raise RuntimeError("ARIMA failed to converge on every candidate order")
        self.res = best
        self.name = f"ARIMA{self.order}"
        return self

    def predict(self, h: int) -> np.ndarray:
        return np.asarray(self.res.forecast(steps=h), dtype=float)

    def interval(self, h: int, alpha: float = 0.05):
        fc = self.res.get_forecast(steps=h)
        ci = fc.conf_int(alpha=alpha)
        ci = np.asarray(ci, dtype=float)
        return ci[:, 0], ci[:, 1]

    @property
    def fitted_residual_std(self) -> float:
        try:
            return float(np.std(self.res.resid[10:], ddof=1))
        except Exception:                                  # noqa: BLE001
            return super().fitted_residual_std


class HoltWintersModel(BaseModel):
    """
    Damped-trend exponential smoothing. Stands in for Prophet where Prophet is
    not installed: same job (trend + level with damping), no heavyweight
    dependency. If Prophet IS installed, ProphetModel is used as well and both
    compete in the back-test.
    """
    name = "Holt-Winters (damped trend)"
    family = "ETS"
    available = HAS_SM
    min_points = 40

    def fit(self, y):
        super().fit(y)
        yy = self.y[-500:]
        self.res = _ETS(yy, trend="add", damped_trend=True,
                        initialization_method="estimated").fit(optimized=True)
        return self

    def predict(self, h: int) -> np.ndarray:
        return np.asarray(self.res.forecast(h), dtype=float)

    @property
    def fitted_residual_std(self) -> float:
        try:
            return float(np.std(self.res.resid[5:], ddof=1))
        except Exception:                                  # noqa: BLE001
            return super().fitted_residual_std


class ProphetModel(BaseModel):
    name = "Prophet"
    family = "PROPHET"
    available = HAS_PROPHET
    min_points = 90

    def __init__(self, dates=None):
        self.dates = dates

    def fit(self, y):
        import pandas as pd
        super().fit(y)
        n = len(self.y)
        idx = (self.dates[-n:] if self.dates is not None
               else pd.date_range(end=pd.Timestamp.today(), periods=n, freq="B"))
        df = pd.DataFrame({"ds": idx, "y": self.y})
        self.m = _Prophet(daily_seasonality=False, weekly_seasonality=False,
                          yearly_seasonality=True, interval_width=0.95)
        self.m.fit(df)
        self.last_ds = df["ds"].iloc[-1]
        return self

    def predict(self, h: int) -> np.ndarray:
        import pandas as pd
        fut = pd.DataFrame({"ds": pd.date_range(self.last_ds, periods=h + 1, freq="B")[1:]})
        return self.m.predict(fut)["yhat"].to_numpy(dtype=float)


class RandomForestModel(BaseModel):
    """
    Direct multi-step regression on lag + rolling features.
    Only competes when there is enough history to learn from.
    """
    name = "Random Forest (lag features)"
    family = "RF"
    available = HAS_SK
    min_points = 120

    LAGS = (1, 2, 3, 5, 10, 20, 30, 60)

    def __init__(self, n_estimators: int = 200):
        self.n_estimators = n_estimators

    def _features(self, y: np.ndarray, t: int) -> list[float]:
        f = [y[t - l] for l in self.LAGS]
        for w in (5, 10, 30):
            f.append(float(np.mean(y[t - w:t])))
            f.append(float(np.std(y[t - w:t], ddof=1)) if w > 1 else 0.0)
        f.append(float(y[t - 1] - y[t - 5]))
        f.append(float(y[t - 1] / y[t - 30] - 1.0))
        return f

    def fit(self, y):
        super().fit(y)
        m = max(self.LAGS) + 1
        X, Y = [], []
        for t in range(m, len(self.y)):
            X.append(self._features(self.y, t))
            Y.append(self.y[t])
        self.model = RandomForestRegressor(
            n_estimators=self.n_estimators, max_depth=12, min_samples_leaf=3,
            random_state=42, n_jobs=-1).fit(np.array(X), np.array(Y))
        self.resid = np.array(Y) - self.model.predict(np.array(X))
        return self

    def predict(self, h: int) -> np.ndarray:
        y = list(self.y)
        out = []
        for _ in range(h):
            arr = np.array(y)
            p = float(self.model.predict(np.array([self._features(arr, len(arr))]))[0])
            out.append(p)
            y.append(p)
        return np.array(out)

    @property
    def fitted_residual_std(self) -> float:
        return float(np.std(self.resid, ddof=1)) if len(self.resid) > 3 else 0.0


def build_model_zoo(dates=None) -> list[BaseModel]:
    zoo = [
        DriftModel(),
        MovingAverageModel(7), MovingAverageModel(30), MovingAverageModel(90),
        EMAModel(12), EMAModel(30),
        LinearRegressionModel(60), LinearRegressionModel(120), LinearRegressionModel(260),
    ]
    if HAS_SM:
        zoo += [ArimaModel(), HoltWintersModel()]
    if HAS_SK:
        zoo += [RandomForestModel()]
    if HAS_PROPHET:
        zoo += [ProphetModel(dates=dates)]
    return [m for m in zoo if m.available]


def capability_report() -> dict:
    return {
        "statsmodels (ARIMA, Holt-Winters)": HAS_SM,
        "scikit-learn (Random Forest)": HAS_SK,
        "prophet": HAS_PROPHET,
        "note": ("Holt-Winters damped trend substitutes for Prophet when Prophet "
                 "is not installed; install prophet to add it to the back-test."),
    }
