"""InSAR kinematics: GP-smoothed LOS displacement, velocity, acceleration and Fukuzono trends.

Pipeline for one facility:

1. **Inputs.** Inside CONUS, a stack of ``OPERA_L3_DISP-S1_V1`` NetCDF granules
   is clipped to the bbox, reprojected to UTM and chained into one
   displacement time series (mm) relative to the first epoch. The chaining is
   the running sum used by OPERA's ``align_to_common_reference_date.py``.
   Outside CONUS (e.g. the Brumadinho backtest) no Earthdata query is made:
   the series is read from ``DATA_DIR/<facility>_historic.csv``.
2. **Coherence.** Pixels whose minimum ``temporal_coherence`` over the stack is
   below 0.65 (or unknown) are flagged ``unobservable`` and never enter the GP
   or any predictive step.
3. **GP.** Per series, a Gaussian process with a Matern-5/2 kernel plus a
   linear-trend (DotProduct) kernel and white noise is fitted with
   scikit-learn. Smoothed displacement, velocity and acceleration (with
   +-2 sigma bands) come from the analytic derivatives of the posterior, which
   exclude the white-noise term.
4. **Trend instability.** Only where |v| >= 2 mm/yr and the acceleration in
   the direction of motion exceeds 2 sigma at the last epoch: inverse velocity
   Lambda = 1/|v| and a Bayesian linear regression on its last 5 points,
   Lambda(t) = Lambda_0 - (t - t_0)/A, giving t_instability = t_0 + A Lambda_0
   +- 2 sigma.
5. **Outputs.** A Zarr store (``DATA_DIR/cache/<Facility>_kinematics.zarr``)
   with all series, 95 % bounds, per-pixel status and the disclaimer below.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import warnings
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np
import pandas as pd
import rioxarray  # noqa: F401  (registers the .rio accessor)
import xarray as xr
from scipy.linalg import cho_factor, cho_solve, solve_triangular

from app.config import Settings

logger = logging.getLogger("lithocreep.kinematics")

DISP_DATASET_ID = "OPERA_L3_DISP-S1_V1"
COHERENCE_THRESHOLD = 0.65
VELOCITY_THRESHOLD_MM_YR = 2.0
ACCELERATION_SIGMA = 2.0
FUKUZONO_POINTS = 5
MIN_EPOCHS = 6
DAYS_PER_YEAR = 365.25
AUTO_PER_PIXEL_LIMIT = 500  # above this many observable pixels, "auto" switches to "hybrid"
SHARED_SAMPLE_SIZE = 64
SCREEN_VELOCITY_MM_YR = 1.0  # hybrid: series with |OLS velocity| >= this get their own GP fit

DISCLAIMER = (
    "Trend instability timelines are statistical extrapolations of satellite line-of-sight inverse-velocity "
    "trends. They indicate a potential change in deformation trend, NOT a guaranteed or predicted collapse or "
    "failure date, and do not replace in-situ geotechnical monitoring (inclinometers, piezometers, surveys)."
)

# Per-pixel kinematic status (CF flag_values / flag_meanings).
STABLE, LINEAR, ACCEL_FORECAST, ACCEL_NO_TREND, UNOBSERVABLE, NO_DATA = 0, 1, 2, 3, 4, 5
STATUS_MEANINGS = (
    "stable-below-2mm-yr moving-linear accelerating-forecast accelerating-no-converging-trend "
    "unobservable-low-coherence no-data"
)

DISP_FILENAME_RE = re.compile(
    r"OPERA_L3_DISP-S1_IW_F(?P<frame>\d{5})_VV_(?P<ref>\d{8}T\d{6}Z)_(?P<sec>\d{8}T\d{6}Z)_v(?P<version>[\d.]+)_(?P<gen>\d{8}T\d{6}Z)"
)


class KinematicsError(ValueError):
    """Invalid kinematic inputs (stack, CSV, series)."""


# --------------------------------------------------------------------------- #
# 1. Inputs: DISP-S1 stack and historical CSV
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DispFile:
    path: Path
    frame: int
    reference: datetime
    secondary: datetime
    version: str
    generated: datetime


def parse_disp_filename(path: Path) -> DispFile:
    match = DISP_FILENAME_RE.search(Path(path).name)
    if not match:
        raise KinematicsError(f"Not an OPERA DISP-S1 filename: {Path(path).name}")
    parse = lambda s: datetime.strptime(s, "%Y%m%dT%H%M%SZ")  # noqa: E731
    return DispFile(Path(path), int(match["frame"]), parse(match["ref"]), parse(match["sec"]), match["version"], parse(match["gen"]))


def select_disp_files(paths: Sequence[Path]) -> list[DispFile]:
    """Parse, keep the newest production of each (reference, secondary) pair, require one frame."""
    files = [parse_disp_filename(p) for p in paths]
    if not files:
        raise KinematicsError("No OPERA DISP-S1 granules supplied")
    frames = {f.frame for f in files}
    if len(frames) > 1:
        raise KinematicsError(f"DISP-S1 granules span several frames {sorted(frames)}; pass one frame's stack")
    latest: dict[tuple, DispFile] = {}
    for f in files:
        key = (f.reference.date(), f.secondary.date())
        if key not in latest or f.generated > latest[key].generated:
            latest[key] = f
    ordered = sorted(latest.values(), key=lambda f: (f.secondary, f.reference))
    epochs = {ordered[0].reference.date()} if ordered else set()
    for f in sorted(ordered, key=lambda f: (f.reference, f.secondary)):
        if f.reference.date() not in epochs:
            raise KinematicsError(
                f"{f.path.name}: reference date {f.reference.date()} is not an epoch of the earlier granules (disconnected stack)"
            )
        epochs.add(f.secondary.date())
    return ordered


def load_disp_stack(paths: Sequence[Path], bbox: Sequence[float], target_crs: Optional[str] = None) -> xr.Dataset:
    """Clip, reproject and chain DISP-S1 granules into a single-reference series.

    Returns ``displacement (time, y, x)`` in mm relative to the first epoch and
    ``temporal_coherence (y, x)``, the minimum over all granules.
    """
    from app.pipelines import ingestion
    from rasterio.enums import Resampling

    target_crs = target_crs or ingestion.TARGET_CRS
    files = select_disp_files(paths)
    spec = ingestion.PRODUCTS[DISP_DATASET_ID]
    coh_spec = dataclasses.replace(spec, variable="temporal_coherence")

    template: Optional[xr.DataArray] = None
    cumulative: dict[Any, np.ndarray] = {}
    epochs: dict[Any, datetime] = {}
    coherence: Optional[np.ndarray] = None
    for f in files:
        layers = []
        for layer_spec in (spec, coh_spec):
            da = ingestion.load_granule_array(layer_spec, f.path, bbox)
            da = ingestion.reproject_and_clip(da, bbox, target_crs, Resampling.bilinear)
            if template is None:
                template = da
            else:
                da = da.rio.reproject_match(template, resampling=Resampling.bilinear)
            layers.append(da.values.astype(np.float64))
        disp_mm, coh = layers[0] * 1000.0, layers[1]  # DISP-S1 displacement is in metres

        ref_key, sec_key = f.reference.date(), f.secondary.date()
        if not cumulative:
            cumulative[ref_key] = np.zeros_like(disp_mm)
            epochs[ref_key] = f.reference
        if ref_key not in cumulative:
            raise KinematicsError(
                f"{f.path.name}: reference date {ref_key} is not an epoch of the earlier granules (disconnected stack)"
            )
        cumulative[sec_key] = cumulative[ref_key] + disp_mm
        epochs[sec_key] = f.secondary
        coherence = coh if coherence is None else np.fmin(coherence, coh)
        # A pixel missing coherence in any granule is unknown, hence unobservable.
        coherence[~np.isfinite(coh)] = np.nan

    keys = sorted(cumulative)
    ds = xr.Dataset(
        {
            "displacement": (("time", "y", "x"), np.stack([cumulative[k] for k in keys]).astype(np.float32)),
            "temporal_coherence": (("y", "x"), coherence.astype(np.float32)),
        },
        coords={"time": pd.to_datetime([epochs[k] for k in keys]), "y": template["y"].values, "x": template["x"].values},
        attrs={
            "source": DISP_DATASET_ID,
            "frame": files[0].frame,
            "n_granules": len(files),
            "reference_epoch": str(epochs[keys[0]]),
            "displacement_units": "mm (LOS, relative to the first epoch)",
        },
    )
    return ds.rio.write_crs(template.rio.crs)


def load_historic_csv(path: Path) -> xr.Dataset:
    """Read a point displacement history CSV.

    Required columns: ``date``, ``displacement_mm`` (cumulative LOS).
    Optional: ``point_id`` (default one point), ``temporal_coherence``,
    ``longitude``, ``latitude``, ``source``. Without a coherence column the
    points are taken as pre-filtered by the source study (recorded in attrs).
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"Historical displacement file {path} not found. Supply the published series "
            "(columns: date, displacement_mm[, point_id, temporal_coherence, longitude, latitude, source])."
        )
    table = pd.read_csv(path)
    missing = {"date", "displacement_mm"} - set(table.columns)
    if missing:
        raise KinematicsError(f"{path.name} is missing columns {sorted(missing)}")
    table["date"] = pd.to_datetime(table["date"], utc=True).dt.tz_localize(None)
    if "point_id" not in table:
        table["point_id"] = "P1"
    table["point_id"] = table["point_id"].astype(str)

    disp = table.pivot_table(index="date", columns="point_id", values="displacement_mm", aggfunc="mean").sort_index()
    points = list(disp.columns)
    per_point = table.groupby("point_id")
    has_coherence = "temporal_coherence" in table
    coherence = (
        per_point["temporal_coherence"].min().reindex(points).to_numpy(np.float64)
        if has_coherence
        else np.ones(len(points))
    )
    data_vars: dict[str, Any] = {
        "displacement": (("time", "point"), disp.to_numpy(np.float32)),
        "temporal_coherence": (("point",), coherence.astype(np.float32)),
    }
    for col in ("longitude", "latitude"):
        if col in table:
            data_vars[col] = (("point",), per_point[col].first().reindex(points).to_numpy(np.float64))
    sources = sorted(table["source"].dropna().astype(str).unique()) if "source" in table else []
    return xr.Dataset(
        data_vars,
        coords={"time": disp.index.values, "point": points},
        attrs={
            "source": f"historical backup: {path.name}",
            "source_citation": "; ".join(sources) or "not given in file",
            "coherence_source": "temporal_coherence column" if has_coherence else "not provided; assumed pre-filtered by source study",
            "displacement_units": "mm (LOS)",
        },
    )


# --------------------------------------------------------------------------- #
# 3. Gaussian process: Matern-5/2 + linear trend, analytic derivatives
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class GPHyper:
    """Hyperparameters in standardised-y units, time in years."""

    amplitude: float  # Matern variance s
    length_scale: float  # years
    linear_scale: float  # DotProduct multiplier c
    sigma_0: float  # DotProduct inhomogeneity
    noise: float  # white-noise variance

    def as_dict(self) -> dict[str, float]:
        return dataclasses.asdict(self)


def _sklearn_kernel():
    from sklearn.gaussian_process.kernels import ConstantKernel, DotProduct, Matern, WhiteKernel

    return (
        ConstantKernel(1.0, (1e-3, 1e3)) * Matern(length_scale=0.5, length_scale_bounds=(0.1, 20.0), nu=2.5)
        + ConstantKernel(0.1, (1e-5, 1e3)) * DotProduct(sigma_0=1.0, sigma_0_bounds=(1e-3, 1e2))
        + WhiteKernel(0.1, (1e-6, 10.0))
    )


def fit_hyperparameters(t_years: np.ndarray, y_std: np.ndarray) -> GPHyper:
    """Maximum-marginal-likelihood hyperparameters (scikit-learn) for one standardised series."""
    from sklearn.gaussian_process import GaussianProcessRegressor

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # bound-hitting ConvergenceWarnings are expected on flat series
        gp = GaussianProcessRegressor(_sklearn_kernel(), normalize_y=False, n_restarts_optimizer=1, random_state=0)
        gp.fit(np.asarray(t_years)[:, None], y_std)
    p = gp.kernel_.get_params()
    return GPHyper(
        amplitude=float(p["k1__k1__k1__constant_value"]),
        length_scale=float(p["k1__k1__k2__length_scale"]),
        linear_scale=float(p["k1__k2__k1__constant_value"]),
        sigma_0=float(p["k1__k2__k2__sigma_0"]),
        noise=float(p["k2__noise_level"]),
    )


def matern52(tau: np.ndarray, s: float, ell: float, derivative: int = 0) -> np.ndarray:
    """Matern-5/2 covariance k(tau) and its 1st/2nd derivatives with respect to tau = t* - t."""
    q = np.sqrt(5.0) / ell
    r = np.abs(tau)
    e = np.exp(-q * r)
    if derivative == 0:
        return s * (1.0 + q * r + (q * r) ** 2 / 3.0) * e
    if derivative == 1:
        return -s * q**2 / 3.0 * tau * (1.0 + q * r) * e
    if derivative == 2:
        return -s * q**2 / 3.0 * (1.0 + q * r - (q * r) ** 2) * e
    raise ValueError("derivative must be 0, 1 or 2")


def _kernel(t_star: np.ndarray, t: np.ndarray, h: GPHyper, derivative: int) -> np.ndarray:
    """Cov(f^(d)(t*), f(t)) for the latent function (no white noise)."""
    tau = t_star[:, None] - t[None, :]
    k = matern52(tau, h.amplitude, h.length_scale, derivative)
    if derivative == 0:
        k = k + h.linear_scale * (h.sigma_0**2 + t_star[:, None] * t[None, :])
    elif derivative == 1:
        k = k + h.linear_scale * np.broadcast_to(t[None, :], tau.shape)
    return k


def _prior_variance(t_star: np.ndarray, h: GPHyper, derivative: int) -> np.ndarray:
    q = np.sqrt(5.0) / h.length_scale
    if derivative == 0:
        return h.amplitude + h.linear_scale * (h.sigma_0**2 + t_star**2)
    if derivative == 1:
        return np.full_like(t_star, h.amplitude * q**2 / 3.0 + h.linear_scale)
    return np.full_like(t_star, h.amplitude * q**4)


def gp_posterior(t_train: np.ndarray, Y: np.ndarray, t_pred: np.ndarray, h: GPHyper) -> dict[str, np.ndarray]:
    """Posterior mean and variance of f, f', f'' at ``t_pred`` for every column of ``Y``.

    ``Y`` is ``(n_train, n_series)`` in standardised units; all series share
    ``t_train`` and ``h``. Variances are those of the latent (noise-free) function.
    """
    K = _kernel(t_train, t_train, h, 0) + h.noise * np.eye(t_train.size)
    factor = cho_factor(K, lower=True)
    alpha = cho_solve(factor, Y)
    L = np.tril(factor[0])
    out: dict[str, np.ndarray] = {}
    for d, name in enumerate(("f", "df", "d2f")):
        C = _kernel(t_pred, t_train, h, d)  # (n_pred, n_train)
        out[f"{name}_mean"] = C @ alpha  # (n_pred, n_series)
        V = solve_triangular(L, C.T, lower=True)
        out[f"{name}_var"] = np.maximum(_prior_variance(t_pred, h, d) - np.sum(V**2, axis=0), 0.0)  # (n_pred,)
    return out


def _standardise(y: np.ndarray) -> tuple[np.ndarray, float, float]:
    mean = float(np.mean(y))
    scale = float(np.std(y))
    scale = scale if scale > 0 else 1.0
    return (y - mean) / scale, mean, scale


# --------------------------------------------------------------------------- #
# 4. Fukuzono inverse velocity and Bayesian trend regression
# --------------------------------------------------------------------------- #


@dataclass
class Forecast:
    lambda_0: float  # yr/mm at t_0 (last epoch)
    slope: float  # d Lambda / dt (yr/mm per yr) = -1/A
    slope_std: float
    A: float  # mm^-1 ... in Lambda = Lambda_0 - (t - t_0)/A
    years_after_t0: float
    years_after_t0_std: float
    slope_significant: bool


def bayesian_linear_fit(x: np.ndarray, y: np.ndarray, y_std: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Bayesian linear regression y = b0 + b1 x with Gaussian noise y_std and a flat prior.

    The posterior is Gaussian with mean = weighted least squares and covariance
    (X^T W X)^-1. The covariance is inflated by the reduced chi-square when the
    residuals exceed the stated uncertainties (Birge ratio), so overly tight GP
    error bars do not give overconfident timelines.
    """
    X = np.column_stack([np.ones_like(x), x])
    w = 1.0 / np.maximum(y_std, 1e-12) ** 2
    XtW = X.T * w
    cov = np.linalg.inv(XtW @ X)
    beta = cov @ (XtW @ y)
    dof = max(x.size - 2, 1)
    chi2_red = float(np.sum(w * (y - X @ beta) ** 2) / dof)
    return beta, cov * max(1.0, chi2_red)


def fukuzono_forecast(t_years: np.ndarray, velocity: np.ndarray, velocity_std: np.ndarray) -> Optional[Forecast]:
    """Fit Lambda = 1/|v| over the last ``FUKUZONO_POINTS`` epochs; None unless Lambda is decreasing."""
    t = t_years[-FUKUZONO_POINTS:]
    v = np.abs(velocity[-FUKUZONO_POINTS:])
    if t.size < FUKUZONO_POINTS or np.any(v <= 0) or not np.all(np.isfinite(v)):
        return None
    lam = 1.0 / v
    lam_std = velocity_std[-FUKUZONO_POINTS:] / v**2  # delta method
    t0 = t[-1]
    beta, cov = bayesian_linear_fit(t - t0, lam, lam_std)
    b0, b1 = beta
    if b1 >= 0:
        return None  # inverse velocity not converging towards zero
    gradient = np.array([-1.0 / b1, b0 / b1**2])  # d(-b0/b1)/d(b0, b1)
    slope_std = float(np.sqrt(cov[1, 1]))
    return Forecast(
        lambda_0=float(b0),
        slope=float(b1),
        slope_std=slope_std,
        A=float(-1.0 / b1),
        years_after_t0=float(-b0 / b1),
        years_after_t0_std=float(np.sqrt(gradient @ cov @ gradient)),
        slope_significant=bool(b1 + ACCELERATION_SIGMA * slope_std < 0),
    )


# --------------------------------------------------------------------------- #
# Series-level analysis
# --------------------------------------------------------------------------- #

SERIES_VARS = (
    "displacement_smooth",
    "displacement_std",
    "velocity",
    "velocity_std",
    "acceleration",
    "acceleration_std",
    "inverse_velocity",
    "inverse_velocity_std",
)
SCALAR_VARS = (
    "fukuzono_lambda_0",
    "fukuzono_A",
    "fukuzono_slope",
    "fukuzono_slope_std",
    "instability_years_after_last_epoch",
    "instability_years_std",
)


def _resolve_mode(mode: str, n_observable: int) -> str:
    if mode not in {"auto", "per_pixel", "hybrid"}:
        raise KinematicsError(f"hyperparameter mode must be auto, per_pixel or hybrid, got {mode!r}")
    if mode == "auto":
        return "per_pixel" if n_observable <= AUTO_PER_PIXEL_LIMIT else "hybrid"
    return mode


def _screen_series(t: np.ndarray, disp: np.ndarray, valid: np.ndarray, idx: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Least-squares screen: linear velocity and the t-statistic of a quadratic term, per series."""
    velocity = np.full(idx.size, np.nan)
    curvature_t = np.full(idx.size, np.nan)
    groups: dict[bytes, list[int]] = {}
    for pos, i in enumerate(idx):
        groups.setdefault(valid[:, i].tobytes(), []).append(pos)
    for key, positions in groups.items():
        mask = np.frombuffer(key, dtype=bool)
        tt = t[mask] - t[mask].mean()
        Y = disp[np.ix_(mask, idx[positions])]
        X1 = np.column_stack([np.ones_like(tt), tt])
        velocity[positions] = np.linalg.lstsq(X1, Y, rcond=None)[0][1]
        X2 = np.column_stack([X1, tt**2])
        beta, *_ = np.linalg.lstsq(X2, Y, rcond=None)
        dof = max(tt.size - 3, 1)
        sigma2 = np.sum((Y - X2 @ beta) ** 2, axis=0) / dof
        var_c = sigma2 * np.linalg.inv(X2.T @ X2)[2, 2]
        with np.errstate(divide="ignore", invalid="ignore"):
            curvature_t[positions] = np.where(var_c > 0, beta[2] / np.sqrt(var_c), 0.0)
    return velocity, curvature_t


def _shared_hyperparameters(t: np.ndarray, Y: np.ndarray) -> GPHyper:
    """Median (in log space) of per-series fits over the columns of ``Y``."""
    fits = [fit_hyperparameters(t, _standardise(Y[:, c])[0]) for c in range(Y.shape[1])]
    med = {k: float(np.exp(np.median(np.log([max(getattr(f, k), 1e-12) for f in fits])))) for k in GPHyper.__annotations__}
    return GPHyper(**med)


def analyze_series(
    times: Sequence[Any],
    displacement: np.ndarray,
    coherence: np.ndarray,
    *,
    hyperparameters: str = "auto",
    seed: int = 0,
    n_jobs: int = 1,
) -> dict[str, Any]:
    """Run the kinematic analysis on ``displacement (time, n)`` with ``coherence (n,)``.

    ``hyperparameters``: ``per_pixel`` fits every series; ``hybrid`` shares
    hyperparameters within quiet and within moving ground and refits alone every
    series that could yield a forecast (shared fit >= 2 mm/yr and accelerating
    beyond 1 sigma, or a moving series with a significant quadratic term); ``auto`` picks ``per_pixel`` up to 500
    observable series. ``n_jobs`` parallelises per-series fits (joblib).

    Returns arrays ``(time, n)`` for the series variables, ``(n,)`` for status
    and forecast scalars, plus run metadata.
    """
    times = pd.to_datetime(np.asarray(times))
    if not np.all(np.diff(times.values).astype(np.int64) > 0):
        raise KinematicsError("epochs must be strictly increasing")
    disp = np.asarray(displacement, dtype=np.float64)
    n_t, n = disp.shape
    t = ((times - times[0]).total_seconds() / 86400.0 / DAYS_PER_YEAR).to_numpy()
    t_c = t - t.mean()  # centred time for the DotProduct trend kernel

    out = {name: np.full((n_t, n), np.nan) for name in SERIES_VARS}
    out.update({name: np.full(n, np.nan) for name in SCALAR_VARS})
    status = np.full(n, NO_DATA, dtype=np.int8)
    slope_significant = np.zeros(n, dtype=bool)

    coherent = np.isfinite(coherence) & (coherence >= COHERENCE_THRESHOLD)
    status[~coherent] = UNOBSERVABLE  # never smoothed, never forecast
    valid_epochs = np.isfinite(disp)
    enough = valid_epochs.sum(axis=0) >= MIN_EPOCHS
    candidates = np.flatnonzero(coherent & enough)
    mode = _resolve_mode(hyperparameters, candidates.size)
    rng = np.random.default_rng(seed)

    def store(members: list[int], h: Optional[GPHyper]) -> None:
        """GP posterior for ``members``: one shared ``h``, or a per-series fit when ``h`` is None."""
        groups: dict[bytes, list[int]] = {}
        for i in members:
            groups.setdefault(valid_epochs[:, i].tobytes(), []).append(i)
        for key, idx in groups.items():
            mask = np.frombuffer(key, dtype=bool)
            tt = t_c[mask]
            Y = disp[np.ix_(mask, idx)]
            means, scales = Y.mean(axis=0), Y.std(axis=0)
            scales[scales == 0] = 1.0
            Ys = (Y - means) / scales
            if h is not None:
                batches = [(h, list(range(len(idx))))]
            elif n_jobs == 1 or len(idx) < 8:
                batches = [(fit_hyperparameters(tt, Ys[:, j]), [j]) for j in range(len(idx))]
            else:
                from joblib import Parallel, delayed

                fitted = Parallel(n_jobs=n_jobs)(delayed(fit_hyperparameters)(tt, Ys[:, j]) for j in range(len(idx)))
                batches = [(hyper, [j]) for j, hyper in enumerate(fitted)]
            for hyper, cols in batches:
                if len(hypers_used) < 50:
                    hypers_used.append(hyper.as_dict())
                post = gp_posterior(tt, Ys[:, cols], t_c, hyper)
                cols_idx = [idx[j] for j in cols]
                sc = scales[cols]
                out["displacement_smooth"][:, cols_idx] = post["f_mean"] * sc + means[cols]
                out["displacement_std"][:, cols_idx] = np.sqrt(post["f_var"])[:, None] * sc
                out["velocity"][:, cols_idx] = post["df_mean"] * sc
                out["velocity_std"][:, cols_idx] = np.sqrt(post["df_var"])[:, None] * sc
                out["acceleration"][:, cols_idx] = post["d2f_mean"] * sc
                out["acceleration_std"][:, cols_idx] = np.sqrt(post["d2f_var"])[:, None] * sc

    hypers_used: list[dict[str, float]] = []
    if mode == "per_pixel":
        store([int(i) for i in candidates], None)
    elif candidates.size:
        # Hybrid: quiet and moving ground each share hyperparameters fitted on a sample of
        # their own kind; every series that could yield a forecast is then refitted alone.
        ols_v, curvature_t = _screen_series(t, disp, valid_epochs, candidates)
        for members in (candidates[np.abs(ols_v) < SCREEN_VELOCITY_MM_YR], candidates[np.abs(ols_v) >= SCREEN_VELOCITY_MM_YR]):
            members = [int(i) for i in members]
            if not members:
                continue
            complete = [i for i in members if valid_epochs[:, i].all()] or members
            sample = np.array(complete)[rng.choice(len(complete), size=min(SHARED_SAMPLE_SIZE, len(complete)), replace=False)]
            mask0 = valid_epochs[:, sample[0]]
            same = [int(i) for i in sample if np.array_equal(valid_epochs[:, i], mask0)]
            store(members, _shared_hyperparameters(t_c[mask0], disp[np.ix_(mask0, same)]))
        v_last, a_last, a_std = out["velocity"][-1], out["acceleration"][-1], out["acceleration_std"][-1]
        curved = dict(zip(candidates.tolist(), (np.abs(ols_v) >= SCREEN_VELOCITY_MM_YR) & (np.abs(curvature_t) > 2.0)))
        recheck = [
            int(i) for i in candidates
            if curved[int(i)] or (abs(v_last[i]) >= VELOCITY_THRESHOLD_MM_YR and np.sign(v_last[i]) * a_last[i] > a_std[i])
        ]
        logger.info("Hybrid GP: %d series share hyperparameters, %d refitted individually", candidates.size, len(recheck))
        store(recheck, None)

    # Classification and forecasts (observable series only).
    for i in candidates:
        v, v_std = out["velocity"][:, i], out["velocity_std"][:, i]
        a, a_std = out["acceleration"][-1, i], out["acceleration_std"][-1, i]
        if abs(v[-1]) < VELOCITY_THRESHOLD_MM_YR:
            status[i] = STABLE  # no predictive modelling below 2 mm/yr
            continue
        accelerating = np.sign(v[-1]) * a > ACCELERATION_SIGMA * a_std
        if not accelerating:
            status[i] = LINEAR
            continue
        with np.errstate(divide="ignore"):
            out["inverse_velocity"][:, i] = 1.0 / np.abs(v)
            out["inverse_velocity_std"][:, i] = v_std / v**2
        forecast = fukuzono_forecast(t, v, v_std)
        if forecast is None:
            status[i] = ACCEL_NO_TREND
            continue
        status[i] = ACCEL_FORECAST
        out["fukuzono_lambda_0"][i] = forecast.lambda_0
        out["fukuzono_A"][i] = forecast.A
        out["fukuzono_slope"][i] = forecast.slope
        out["fukuzono_slope_std"][i] = forecast.slope_std
        out["instability_years_after_last_epoch"][i] = forecast.years_after_t0
        out["instability_years_std"][i] = forecast.years_after_t0_std
        slope_significant[i] = forecast.slope_significant

    out["status"] = status
    out["slope_significant"] = slope_significant
    out["meta"] = {"hyperparameter_mode": mode, "n_series": n, "n_observable": int(candidates.size), "hyperparameters_sample": hypers_used}
    return out


# --------------------------------------------------------------------------- #
# Dataset assembly, I/O and orchestration
# --------------------------------------------------------------------------- #


def kinematics_dataset(inputs: xr.Dataset, *, hyperparameters: str = "auto", n_jobs: int = 1) -> xr.Dataset:
    """Analyse a raster ``(time, y, x)`` or point ``(time, point)`` displacement dataset."""
    disp = inputs["displacement"]
    space_dims = tuple(d for d in disp.dims if d != "time")
    flat = disp.transpose("time", *space_dims).values.reshape(disp.sizes["time"], -1)
    coherence = inputs["temporal_coherence"].transpose(*space_dims).values.reshape(-1)
    res = analyze_series(inputs["time"].values, flat, coherence, hyperparameters=hyperparameters, n_jobs=n_jobs)

    space_shape = tuple(disp.sizes[d] for d in space_dims)
    series_dims, scalar_dims = ("time", *space_dims), space_dims
    reshape_series = lambda a: a.reshape((disp.sizes["time"], *space_shape)).astype(np.float32)  # noqa: E731
    reshape_scalar = lambda a: a.reshape(space_shape)  # noqa: E731

    data: dict[str, Any] = {"displacement": (series_dims, reshape_series(flat))}
    for base, unit in (("displacement_smooth", "mm"), ("velocity", "mm/yr"), ("acceleration", "mm/yr^2"), ("inverse_velocity", "yr/mm")):
        std_name = "displacement_std" if base == "displacement_smooth" else f"{base}_std"
        mean, std = res[base], res[std_name]
        data[base] = (series_dims, reshape_series(mean), {"units": unit})
        data[f"{base}_lower"] = (series_dims, reshape_series(mean - 2 * std), {"units": unit, "long_name": f"{base} - 2 sigma (95% bound)"})
        data[f"{base}_upper"] = (series_dims, reshape_series(mean + 2 * std), {"units": unit, "long_name": f"{base} + 2 sigma (95% bound)"})
    data["temporal_coherence"] = (scalar_dims, reshape_scalar(coherence).astype(np.float32))
    data["kinematic_status"] = (
        scalar_dims,
        reshape_scalar(res["status"]),
        {"flag_values": [STABLE, LINEAR, ACCEL_FORECAST, ACCEL_NO_TREND, UNOBSERVABLE, NO_DATA], "flag_meanings": STATUS_MEANINGS},
    )
    for name in ("fukuzono_lambda_0", "fukuzono_A", "fukuzono_slope", "fukuzono_slope_std"):
        data[name] = (scalar_dims, reshape_scalar(res[name]).astype(np.float32))
    data["fukuzono_slope_significant"] = (scalar_dims, reshape_scalar(res["slope_significant"]))

    last = pd.Timestamp(inputs["time"].values[-1])
    yrs, yrs_std = res["instability_years_after_last_epoch"], res["instability_years_std"]
    days, days_std = yrs * DAYS_PER_YEAR, yrs_std * DAYS_PER_YEAR
    data["instability_days_after_last_epoch"] = (scalar_dims, reshape_scalar(days).astype(np.float32), {"units": "days"})
    data["instability_days_2sigma"] = (scalar_dims, reshape_scalar(2 * days_std).astype(np.float32), {"units": "days"})
    to_time = lambda d: np.where(np.isfinite(d), last.to_datetime64() + (np.nan_to_num(d) * 86400e9).astype("timedelta64[ns]"), np.datetime64("NaT"))  # noqa: E731
    data["instability_time"] = (scalar_dims, reshape_scalar(to_time(days)))
    data["instability_time_lower"] = (scalar_dims, reshape_scalar(to_time(days - 2 * days_std)))
    data["instability_time_upper"] = (scalar_dims, reshape_scalar(to_time(days + 2 * days_std)))

    coords = {k: v for k, v in inputs.coords.items() if k in ("time", *space_dims)}
    ds = xr.Dataset(data, coords=coords)
    for extra in ("longitude", "latitude"):
        if extra in inputs:
            ds[extra] = inputs[extra]
    ds.attrs.update({k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in inputs.attrs.items()})
    ds.attrs.update(
        {
            "disclaimer": DISCLAIMER,
            "coherence_threshold": COHERENCE_THRESHOLD,
            "velocity_threshold_mm_yr": VELOCITY_THRESHOLD_MM_YR,
            "acceleration_sigma": ACCELERATION_SIGMA,
            "fukuzono_points": FUKUZONO_POINTS,
            "gp_kernel": "Matern(nu=2.5) + linear trend (DotProduct) + white noise; analytic posterior derivatives",
            "hyperparameter_mode": res["meta"]["hyperparameter_mode"],
            "gp_hyperparameters_sample": json.dumps(res["meta"]["hyperparameters_sample"][:10]),
            "last_epoch": str(last),
        }
    )
    if inputs.rio.crs is not None:
        ds = ds.rio.write_crs(inputs.rio.crs)
        for name in ds.data_vars:
            ds[name].attrs["grid_mapping"] = "spatial_ref"
    return ds


def write_kinematics_zarr(ds: xr.Dataset, output_path: Path) -> None:
    import zarr

    output_path.parent.mkdir(parents=True, exist_ok=True)
    compressor = zarr.Blosc(cname="zstd", clevel=5, shuffle=zarr.Blosc.BITSHUFFLE)
    encoding = {name: {"compressor": compressor} for name in ds.data_vars}
    ds.to_zarr(output_path, mode="w", consolidated=True, encoding=encoding)


def summarize(ds: xr.Dataset) -> dict[str, Any]:
    status = ds["kinematic_status"].values.reshape(-1)
    names = STATUS_MEANINGS.split()
    summary: dict[str, Any] = {"status_counts": {names[k]: int((status == k).sum()) for k in range(len(names))}}
    forecast = status == ACCEL_FORECAST
    if forecast.any():
        days = ds["instability_days_after_last_epoch"].values.reshape(-1)[forecast]
        summary["instability_days_after_last_epoch"] = {"min": float(np.nanmin(days)), "median": float(np.nanmedian(days))}
    summary["disclaimer"] = DISCLAIMER
    return summary


def run_kinematics(
    facility: str,
    bbox: Sequence[float],
    output_path: Path,
    *,
    settings: Settings,
    stack_paths: Optional[Sequence[Path]] = None,
    historic_csv: Optional[Path] = None,
    hyperparameters: str = "auto",
    n_jobs: int = 1,
) -> dict[str, Any]:
    """Load inputs (DISP-S1 stack in CONUS, historical CSV elsewhere), analyse and write Zarr."""
    from app.pipelines.ingestion import bbox_within_conus, validate_bbox

    bbox = validate_bbox(bbox)
    slug = re.sub(r"[^a-z0-9]+", "_", facility.lower()).strip("_")
    if not bbox_within_conus(bbox):
        csv = Path(historic_csv) if historic_csv else settings.data_path / f"{slug}_historic.csv"
        logger.warning(
            "%s bbox %s is outside CONUS: OPERA DISP-S1 has no coverage, no Earthdata query is made; loading %s",
            facility, bbox, csv,
        )
        inputs = load_historic_csv(csv)
    else:
        if stack_paths is None:
            stack_paths = sorted((settings.raw_dir / DISP_DATASET_ID).glob("OPERA_L3_DISP-S1_*.nc"))
        if not stack_paths:
            raise FileNotFoundError(
                f"No DISP-S1 granules in {settings.raw_dir / DISP_DATASET_ID}; download a stack first (kinematics --download)"
            )
        inputs = load_disp_stack(stack_paths, bbox)

    ds = kinematics_dataset(inputs, hyperparameters=hyperparameters, n_jobs=n_jobs)
    ds.attrs["facility"] = facility
    ds.attrs["bbox_epsg4326"] = json.dumps(list(bbox))
    write_kinematics_zarr(ds, Path(output_path))
    summary = {"facility": facility, "output": str(output_path), "source": inputs.attrs.get("source"), **summarize(ds)}
    logger.info("Kinematics %s -> %s: %s", facility, output_path, summary["status_counts"])
    return summary
