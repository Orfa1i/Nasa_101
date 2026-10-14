"""Inverse-velocity (Fukuzono) regression re-evaluation shared by the pixel, backtest and stress-test routes."""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd
import xarray as xr

from app.api import layers as L
from app.api.schemas import FukuzonoFit


def fukuzono_fit(cell: xr.Dataset) -> Optional[FukuzonoFit]:
    """Re-evaluate the Phase 3 inverse-velocity regression of one cell/point with its full covariance.

    The regression inputs are the stored Lambda = 1/|v| over the last ``FUKUZONO_POINTS`` epochs with
    sigma = (upper - lower) / 4, exactly as in ``kinematics.fukuzono_forecast``; the zero crossing and
    its 95 % bounds are the stored Phase 3 values.
    """
    from app.pipelines import kinematics as kin

    needed = {"inverse_velocity", "inverse_velocity_lower", "inverse_velocity_upper", "fukuzono_slope", "fukuzono_lambda_0"}
    if not needed <= set(cell.data_vars) or not np.isfinite(float(cell["fukuzono_slope"].values)):
        return None
    times = pd.to_datetime(cell["time"].values)
    n = kin.FUKUZONO_POINTS
    lam = cell["inverse_velocity"].values[-n:].astype(np.float64)
    lo = cell["inverse_velocity_lower"].values[-n:].astype(np.float64)
    hi = cell["inverse_velocity_upper"].values[-n:].astype(np.float64)
    if lam.size < n or not (np.isfinite(lam).all() and np.isfinite(lo).all() and np.isfinite(hi).all()):
        return None
    t0 = times[-1]
    x = ((times[-n:] - t0).total_seconds() / 86400.0 / kin.DAYS_PER_YEAR).to_numpy()
    (b0, b1), cov = kin.bayesian_linear_fit(x, lam, (hi - lo) / 4.0)
    if b1 >= 0:
        return None

    crossing = L.json_time(cell["instability_time"].values) if "instability_time" in cell else None
    crossing_lo = L.json_time(cell["instability_time_lower"].values) if "instability_time_lower" in cell else None
    crossing_hi = L.json_time(cell["instability_time_upper"].values) if "instability_time_upper" in cell else None
    years = lambda when: (pd.Timestamp(when) - t0).total_seconds() / 86400.0 / kin.DAYS_PER_YEAR  # noqa: E731
    end = years(crossing_hi) if crossing_hi is not None else -b0 / b1
    end = min(max(end, 0.0) * 1.15 + 1e-3, 5.0)  # a little past the latest crossing bound, at most 5 years
    grid = np.linspace(x[0], end, 60)
    mean = b0 + b1 * grid
    sd = np.sqrt(np.maximum(cov[0, 0] + 2 * grid * cov[0, 1] + grid**2 * cov[1, 1], 0.0))
    line_times = [(t0 + pd.Timedelta(days=float(g) * kin.DAYS_PER_YEAR)).floor("s").to_pydatetime() for g in grid]
    significant = bool(cell["fukuzono_slope_significant"].values) if "fukuzono_slope_significant" in cell else bool(b1 + 2 * np.sqrt(cov[1, 1]) < 0)
    return FukuzonoFit(
        window_times=[t.to_pydatetime() for t in times[-n:]],
        inverse_velocity_yr_mm=L.json_list(lam),
        inverse_velocity_lower_yr_mm=L.json_list(lo),
        inverse_velocity_upper_yr_mm=L.json_list(hi),
        t0=t0.to_pydatetime(),
        lambda_0_yr_mm=float(b0),
        slope_yr_mm_per_yr=float(b1),
        slope_std=float(np.sqrt(cov[1, 1])),
        slope_significant=significant,
        line_times=line_times,
        line_yr_mm=[float(v) for v in mean],
        line_lower_yr_mm=[float(v) for v in mean - 2 * sd],
        line_upper_yr_mm=[float(v) for v in mean + 2 * sd],
        crossing_estimate=crossing,
        crossing_lower=crossing_lo,
        crossing_upper=crossing_hi,
    )
