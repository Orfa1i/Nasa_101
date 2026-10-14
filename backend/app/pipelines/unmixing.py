"""EMIT hyperspectral processing: band mapping, NNLS unmixing, spectral indices and SAI.

Pipeline for one EMIT L2A reflectance cube ``(band, y, x)``:

1. **Band mapping.** EMIT distributes 285 channels (381-2493 nm, ~7.4 nm
   spacing). Channels inside the water-vapour windows 1340-1460 nm and
   1790-1970 nm are dropped. On the EMIT wavelength calibration this leaves
   **245** active channels (EMIT's own ``good_wavelengths`` flag keeps 244),
   not 242: see ``IMPLEMENTATION_STATUS.md``. The granule's own wavelengths are
   used when present, otherwise the bundled calibration
   ``data/reference/emit_wavelengths_285.csv``.
2. **Endmember matrix.** Seven reference spectra are read from the USGS library
   extract and linearly interpolated onto the active channels, giving ``M`` of
   shape ``(n_active, 7)`` in reflectance units (clipped to [0, 1]; not
   rescaled, so fractions keep their areal meaning).
3. **Unmixing.** Per pixel, ``f = argmin ||r - M f||^2`` subject to ``f >= 0``
   and ``sum(f) <= 1`` via ``scipy.optimize.nnls``; ``rmse`` is stored.
4. **Indices.** NDVI (R860, R640) and NDWI_SWIR (R860, R1240) from the nearest
   channel within 15 nm (no interpolation), and
   ``SAI = 0.40 f_jar + 0.30 f_smectite + 0.20 f_goethite + 0.10 NDWI_SWIR``.
   Pixels with NDVI > 0.35 are labelled ``unobservable-vegetated``: their SAI
   is masked and their confidence set to zero.
5. **Outputs.** A Zarr store on the input grid plus an RMSE/validation JSON
   log; optional comparison with an EMITL2BMIN granule (spatial R^2).
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import numpy as np
import pandas as pd
import rioxarray  # noqa: F401  (registers the .rio accessor)
import xarray as xr
from rasterio.enums import Resampling
from scipy.optimize import nnls

from app.config import BACKEND_DIR, Settings

logger = logging.getLogger("lithocreep.unmixing")

REFERENCE_DIR = BACKEND_DIR.parent / "data" / "reference"
EMIT_WAVELENGTHS_FILE = REFERENCE_DIR / "emit_wavelengths_285.csv"
SPLIB07_EXTRACT_NAME = "usgs_splib07_extract.csv"
SPLIB06_EXTRACT_FILE = REFERENCE_DIR / "usgs_splib06a_extract.csv"
L2BMIN_GROUPING_MATRIX_FILE = REFERENCE_DIR / "emit_l2b_mineral_grouping_matrix_20230503.csv"

EMIT_N_BANDS = 285
WATER_VAPOR_WINDOWS_NM: tuple[tuple[float, float], ...] = ((1340.0, 1460.0), (1790.0, 1970.0))

ENDMEMBERS: tuple[str, ...] = (
    "Jarosite",  # iron-sulfate seepage marker
    "Goethite",  # iron-oxide alteration marker
    "Ferrihydrite",  # iron-hydroxide marker
    "Montmorillonite",  # swelling smectite clay
    "Gypsum",  # evaporite
    "Quartz-Feldspar",  # barren rock background
    "Water-Shade",  # dark calibration background
)

SAI_FRACTION_WEIGHTS = {"Jarosite": 0.40, "Montmorillonite": 0.30, "Goethite": 0.20}
SAI_NDWI_WEIGHT = 0.10
INDEX_BANDS_NM = {"R640": 640.0, "R860": 860.0, "R1240": 1240.0}
BAND_MATCH_TOLERANCE_NM = 15.0
NDVI_VEGETATION_THRESHOLD = 0.35
RMSE_REJECT_THRESHOLD = 0.05  # unmixing fits worse than this get zero confidence
SCALED_REFLECTANCE_THRESHOLD = 1.5  # max reflectance above this => integer-scaled input
REFLECTANCE_SCALE_FACTOR = 10000.0

# Observability flags (CF flag_values / flag_meanings).
OBSERVABLE, VEGETATED, NO_DATA, POOR_FIT = 0, 1, 2, 3
FLAG_MEANINGS = "observable unobservable-vegetated no-data poor-fit"

# Endmembers that the EMITL2BMIN product can validate, and how to find them in
# the L2B mineral grouping matrix: an EMIT-10 aggregation column when the
# mineral is one of the ten EMIT classes, otherwise a library filename pattern.
L2BMIN_COLUMN = {"Goethite": "Goethite", "Gypsum": "Gypsum", "Montmorillonite": "Montmorillonite"}
L2BMIN_FILENAME_PATTERN = {"Jarosite": r"jarosite", "Ferrihydrite": r"ferrihydrite"}


class SpectralError(ValueError):
    """Invalid spectral inputs (wavelength grid, library, cube)."""


# --------------------------------------------------------------------------- #
# 1. EMIT band mapping
# --------------------------------------------------------------------------- #


def load_emit_wavelengths(path: Optional[Path] = None) -> tuple[np.ndarray, np.ndarray]:
    """Return the 285 EMIT band centres and FWHMs (nm) from the bundled calibration table."""
    table = pd.read_csv(path or EMIT_WAVELENGTHS_FILE)
    wavelengths = table["wavelength_nm"].to_numpy(dtype=np.float64)
    fwhm = table["fwhm_nm"].to_numpy(dtype=np.float64)
    if wavelengths.size != EMIT_N_BANDS or not np.all(np.diff(wavelengths) > 0):
        raise SpectralError(f"{path or EMIT_WAVELENGTHS_FILE} must list {EMIT_N_BANDS} increasing wavelengths")
    return wavelengths, fwhm


def water_vapor_mask(
    wavelengths: Sequence[float], windows: Sequence[tuple[float, float]] = WATER_VAPOR_WINDOWS_NM
) -> np.ndarray:
    """True for channels inside any (inclusive) water-vapour window."""
    wl = np.asarray(wavelengths, dtype=np.float64)
    mask = np.zeros(wl.shape, dtype=bool)
    for lo, hi in windows:
        mask |= (wl >= lo) & (wl <= hi)
    return mask


def active_band_mask(wavelengths: Sequence[float]) -> np.ndarray:
    """True for channels used in unmixing (finite and outside the vapour windows)."""
    wl = np.asarray(wavelengths, dtype=np.float64)
    return np.isfinite(wl) & ~water_vapor_mask(wl)


def nearest_band(wavelengths: Sequence[float], target_nm: float, tolerance_nm: float = BAND_MATCH_TOLERANCE_NM) -> int:
    """Index of the channel closest to ``target_nm``; error if none lies within ``tolerance_nm``."""
    wl = np.asarray(wavelengths, dtype=np.float64)
    idx = int(np.nanargmin(np.abs(wl - target_nm)))
    if abs(wl[idx] - target_nm) > tolerance_nm:
        raise SpectralError(f"no channel within {tolerance_nm} nm of {target_nm} nm (nearest {wl[idx]:.2f} nm)")
    return idx


# --------------------------------------------------------------------------- #
# 2. Spectral library and endmember matrix
# --------------------------------------------------------------------------- #


@dataclass
class SpectralLibrary:
    path: Path
    library: str
    spectra: dict[str, tuple[np.ndarray, np.ndarray]]  # endmember -> (wavelength_nm, reflectance)
    titles: dict[str, str] = field(default_factory=dict)

    @property
    def endmembers(self) -> tuple[str, ...]:
        return tuple(self.spectra)


def resolve_library_path(settings: Optional[Settings] = None) -> Path:
    """Prefer a splib07 extract (``DATA_DIR/reference`` then the repo), else the bundled splib06a extract."""
    candidates = []
    if settings is not None:
        candidates.append(settings.data_path / "reference" / SPLIB07_EXTRACT_NAME)
    candidates.append(REFERENCE_DIR / SPLIB07_EXTRACT_NAME)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    if SPLIB06_EXTRACT_FILE.exists():
        logger.warning(
            "%s not found; using the bundled USGS splib06a extract %s", SPLIB07_EXTRACT_NAME, SPLIB06_EXTRACT_FILE
        )
        return SPLIB06_EXTRACT_FILE
    raise FileNotFoundError(f"No USGS spectral library extract found (looked for {[str(c) for c in candidates]})")


def load_spectral_library(
    path: Optional[Path] = None, *, settings: Optional[Settings] = None, endmembers: Sequence[str] = ENDMEMBERS
) -> SpectralLibrary:
    """Load the endmember spectra from a long-format CSV extract.

    Required columns: ``endmember``, ``wavelength_nm``, ``reflectance``;
    optional ``usgs_title`` and ``library``. Endmember names match
    case-insensitively. Deleted/negative-wavelength points are dropped and
    reflectance is clipped to the physical range [0, 1].
    """
    path = Path(path) if path is not None else resolve_library_path(settings)
    table = pd.read_csv(path)
    missing_cols = {"endmember", "wavelength_nm", "reflectance"} - set(table.columns)
    if missing_cols:
        raise SpectralError(f"{path.name} is missing columns {sorted(missing_cols)}")

    table["_key"] = table["endmember"].astype(str).str.strip().str.lower()
    spectra: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    titles: dict[str, str] = {}
    missing = []
    for name in endmembers:
        rows = table[table["_key"] == name.lower()]
        rows = rows[np.isfinite(rows["wavelength_nm"]) & np.isfinite(rows["reflectance"]) & (rows["wavelength_nm"] > 0)]
        if rows.empty:
            missing.append(name)
            continue
        rows = rows.sort_values("wavelength_nm").drop_duplicates("wavelength_nm")
        spectra[name] = (
            rows["wavelength_nm"].to_numpy(dtype=np.float64),
            np.clip(rows["reflectance"].to_numpy(dtype=np.float64), 0.0, 1.0),
        )
        if "usgs_title" in rows:
            titles[name] = " ".join(str(rows["usgs_title"].iloc[0]).split())
    if missing:
        raise SpectralError(f"{path.name} has no spectra for endmember(s) {missing}")

    library = str(table["library"].iloc[0]) if "library" in table and not table.empty else "unknown"
    logger.info("Loaded %d endmember spectra (%s) from %s", len(spectra), library, path)
    return SpectralLibrary(path=path, library=library, spectra=spectra, titles=titles)


def build_endmember_matrix(active_wavelengths: Sequence[float], library: SpectralLibrary) -> np.ndarray:
    """Interpolate each library spectrum onto ``active_wavelengths`` -> ``M`` of shape (n_active, n_endmembers)."""
    wl = np.asarray(active_wavelengths, dtype=np.float64)
    columns = []
    for name, (lib_wl, lib_refl) in library.spectra.items():
        if wl.min() < lib_wl.min() or wl.max() > lib_wl.max():
            raise SpectralError(
                f"{name} spectrum covers {lib_wl.min():.1f}-{lib_wl.max():.1f} nm, "
                f"not the required {wl.min():.1f}-{wl.max():.1f} nm"
            )
        columns.append(np.interp(wl, lib_wl, lib_refl))
    return np.clip(np.column_stack(columns), 0.0, 1.0)


# --------------------------------------------------------------------------- #
# 3. Constrained NNLS unmixing
# --------------------------------------------------------------------------- #


def unmix_spectrum(r: np.ndarray, M: np.ndarray) -> tuple[np.ndarray, float]:
    """Solve ``min ||r - M f||^2`` s.t. ``f >= 0``, ``sum(f) <= 1``. Returns ``(f, rmse)``.

    The problem is convex with a single extra inequality, so if the plain NNLS
    solution already satisfies ``sum(f) <= 1`` it is optimal; otherwise the sum
    constraint is active and the problem is re-solved with ``sum(f) = 1``
    enforced by a heavily weighted augmentation row (as in FCLS), then
    renormalised so the sum is exactly 1.

    Edge cases: channels with non-finite reflectance are ignored; with fewer
    than ``2 * n_endmembers`` usable channels the pixel is no-data (NaN).
    """
    n_end = M.shape[1]
    r = np.asarray(r, dtype=np.float64)
    usable = np.isfinite(r)
    if usable.sum() < 2 * n_end:
        return np.full(n_end, np.nan), float("nan")
    Mv, rv = M[usable], r[usable]

    f, _ = nnls(Mv, rv)
    if f.sum() > 1.0:
        weight = 1e3 * max(np.linalg.norm(Mv), 1.0)
        A = np.vstack([Mv, np.full((1, n_end), weight)])
        b = np.append(rv, weight)
        f, _ = nnls(A, b)
        total = f.sum()
        f = f / total if total > 0 else f
    f = np.clip(f, 0.0, None)
    rmse = float(np.sqrt(np.mean((rv - Mv @ f) ** 2)))
    return f, rmse


def unmix_cube(cube: np.ndarray, M: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Unmix a ``(band, y, x)`` cube of active channels -> fractions ``(endmember, y, x)`` and ``rmse (y, x)``."""
    n_bands, ny, nx = cube.shape
    if n_bands != M.shape[0]:
        raise SpectralError(f"cube has {n_bands} channels but M has {M.shape[0]} rows")
    pixels = cube.reshape(n_bands, -1).T
    fractions = np.full((pixels.shape[0], M.shape[1]), np.nan, dtype=np.float64)
    rmse = np.full(pixels.shape[0], np.nan, dtype=np.float64)
    for i in np.flatnonzero(np.isfinite(pixels).any(axis=1)):
        fractions[i], rmse[i] = unmix_spectrum(pixels[i], M)
    return fractions.T.reshape(M.shape[1], ny, nx), rmse.reshape(ny, nx)


# --------------------------------------------------------------------------- #
# 4. Indices and SAI
# --------------------------------------------------------------------------- #


def _normalized_difference(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        denom = a + b
        out = (a - b) / denom
    out[~np.isfinite(out) | (denom <= 0)] = np.nan
    return out


def spectral_indices(cube: np.ndarray, wavelengths: Sequence[float]) -> dict[str, Any]:
    """NDVI and NDWI_SWIR from the nearest channels (within 15 nm, no interpolation)."""
    idx = {name: nearest_band(wavelengths, target) for name, target in INDEX_BANDS_NM.items()}
    r640, r860, r1240 = (cube[idx[k]].astype(np.float64) for k in ("R640", "R860", "R1240"))
    return {
        "ndvi": _normalized_difference(r860, r640),
        "ndwi_swir": _normalized_difference(r860, r1240),
        "band_indices": idx,
        "band_wavelengths": {k: float(np.asarray(wavelengths)[i]) for k, i in idx.items()},
    }


def seepage_alteration_index(fractions: Mapping[str, np.ndarray], ndwi_swir: np.ndarray) -> np.ndarray:
    """SAI = 0.40 f_jarosite + 0.30 f_smectite (montmorillonite) + 0.20 f_goethite + 0.10 NDWI_SWIR."""
    sai = SAI_NDWI_WEIGHT * ndwi_swir
    for name, weight in SAI_FRACTION_WEIGHTS.items():
        sai = sai + weight * fractions[name]
    return sai


# --------------------------------------------------------------------------- #
# Cube-level processing
# --------------------------------------------------------------------------- #


def _cube_wavelengths(rfl: xr.DataArray, wavelengths: Optional[Sequence[float]]) -> np.ndarray:
    if wavelengths is not None:
        wl = np.asarray(wavelengths, dtype=np.float64)
    elif "wavelength" in rfl.coords:
        wl = rfl["wavelength"].values.astype(np.float64)
    elif rfl.sizes["band"] == EMIT_N_BANDS:
        logger.info("Cube has no wavelength coordinate; using the bundled EMIT calibration")
        wl, _ = load_emit_wavelengths()
    else:
        raise SpectralError(f"cannot infer wavelengths for a {rfl.sizes['band']}-band cube without a 'wavelength' coordinate")
    if wl.size != rfl.sizes["band"]:
        raise SpectralError(f"{wl.size} wavelengths for {rfl.sizes['band']} bands")
    return wl


def process_reflectance(
    rfl: xr.DataArray,
    *,
    wavelengths: Optional[Sequence[float]] = None,
    library: Optional[SpectralLibrary] = None,
    settings: Optional[Settings] = None,
) -> xr.Dataset:
    """Unmix a ``(band, y, x)`` reflectance cube and derive indices, SAI, confidence and flags.

    The output shares the input grid (same ``x``/``y`` and CRS).
    """
    if set(rfl.dims) != {"band", "y", "x"}:
        raise SpectralError(f"expected dims (band, y, x), got {rfl.dims}")
    rfl = rfl.transpose("band", "y", "x")
    wl = _cube_wavelengths(rfl, wavelengths)
    library = library or load_spectral_library(settings=settings)

    cube = rfl.values.astype(np.float64)
    cube[cube <= -9999] = np.nan
    scale = 1.0
    if np.nanmax(cube, initial=0.0) > SCALED_REFLECTANCE_THRESHOLD:
        scale = REFLECTANCE_SCALE_FACTOR
        logger.info("Reflectance looks integer-scaled (max %.1f); dividing by %g", np.nanmax(cube), scale)
        cube /= scale

    active = active_band_mask(wl)
    M = build_endmember_matrix(wl[active], library)
    fractions, rmse = unmix_cube(cube[active], M)

    indices = spectral_indices(cube, wl)
    ndvi, ndwi = indices["ndvi"], indices["ndwi_swir"]
    named = dict(zip(library.endmembers, fractions))
    sai = seepage_alteration_index(named, ndwi)

    no_data = ~np.isfinite(rmse) | ~np.isfinite(sai)
    vegetated = ~no_data & (ndvi > NDVI_VEGETATION_THRESHOLD)
    poor_fit = ~no_data & ~vegetated & (rmse > RMSE_REJECT_THRESHOLD)
    flags = np.full(rmse.shape, OBSERVABLE, dtype=np.int8)
    flags[poor_fit], flags[vegetated], flags[no_data] = POOR_FIT, VEGETATED, NO_DATA

    confidence = np.clip(1.0 - rmse / RMSE_REJECT_THRESHOLD, 0.0, 1.0)
    confidence[vegetated | no_data] = 0.0
    sai = np.where(vegetated | no_data, np.nan, sai)

    yx = ("y", "x")
    ds = xr.Dataset(
        {
            "abundance": (("endmember", "y", "x"), fractions.astype(np.float32)),
            "fraction_sum": (yx, np.nansum(fractions, axis=0, where=np.isfinite(fractions)).astype(np.float32)),
            "rmse": (yx, rmse.astype(np.float32)),
            "ndvi": (yx, ndvi.astype(np.float32)),
            "ndwi_swir": (yx, ndwi.astype(np.float32)),
            "sai": (yx, sai.astype(np.float32)),
            "confidence": (yx, confidence.astype(np.float32)),
            "observability": (yx, flags),
        },
        coords={"endmember": list(library.endmembers), "y": rfl["y"].values, "x": rfl["x"].values},
    )
    ds["fraction_sum"] = ds["fraction_sum"].where(~no_data)
    # The full (unit-scaled) cube is kept so the inspector can plot the measured spectrum of a
    # cell next to its NNLS reconstruction; `band_active` marks the channels used in the fit.
    ds["reflectance"] = (("band", "y", "x"), cube.astype(np.float32))
    ds = ds.assign_coords(
        band=np.arange(1, wl.size + 1, dtype=np.int16),
        wavelength=("band", wl.astype(np.float32)),
        band_active=("band", active),
    )
    ds["reflectance"].attrs["long_name"] = "surface reflectance (unit scale); water-vapour channels have band_active=False"
    ds["observability"].attrs.update(
        flag_values=[OBSERVABLE, VEGETATED, NO_DATA, POOR_FIT], flag_meanings=FLAG_MEANINGS
    )
    ds["abundance"].attrs["long_name"] = "NNLS endmember fraction (f >= 0, sum <= 1)"
    ds["rmse"].attrs["long_name"] = "unmixing RMSE over active channels (reflectance units)"
    ds["sai"].attrs["long_name"] = "Seepage Alteration Index; NaN where vegetated or no data"
    ds["confidence"].attrs["long_name"] = f"max(0, 1 - rmse/{RMSE_REJECT_THRESHOLD}); 0 where vegetated/no data"

    active_wl = wl[active]
    ds.attrs.update(
        {
            "n_bands": int(wl.size),
            "n_active_bands": int(active.sum()),
            "water_vapor_windows_nm": json.dumps(WATER_VAPOR_WINDOWS_NM),
            "active_wavelengths_nm": json.dumps(np.round(active_wl, 4).tolist()),
            "endmember_library": library.library,
            "endmember_library_file": library.path.name,
            "endmember_titles": json.dumps(library.titles),
            "index_band_wavelengths_nm": json.dumps(indices["band_wavelengths"]),
            "sai_formula": "0.40*f_Jarosite + 0.30*f_Montmorillonite + 0.20*f_Goethite + 0.10*NDWI_SWIR",
            "ndvi_vegetation_threshold": NDVI_VEGETATION_THRESHOLD,
            "rmse_reject_threshold": RMSE_REJECT_THRESHOLD,
            "reflectance_scale_divisor": scale,
        }
    )
    for key in ("origin", "data_label", "granule_id", "dataset_id"):
        if key in rfl.attrs:
            ds.attrs[f"source_{key}"] = rfl.attrs[key]
    if rfl.rio.crs is not None:
        ds = ds.rio.write_crs(rfl.rio.crs)
    return ds


# --------------------------------------------------------------------------- #
# 5. Validation, logs and I/O
# --------------------------------------------------------------------------- #


def rmse_summary(ds: xr.Dataset) -> dict[str, Any]:
    rmse = ds["rmse"].values.astype(np.float64)
    flags = ds["observability"].values
    valid = rmse[np.isfinite(rmse)]
    summary: dict[str, Any] = {
        "n_pixels": int(rmse.size),
        "n_unmixed": int(valid.size),
        "n_observable": int((flags == OBSERVABLE).sum()),
        "n_vegetated": int((flags == VEGETATED).sum()),
        "n_no_data": int((flags == NO_DATA).sum()),
        "n_poor_fit": int((flags == POOR_FIT).sum()),
        "rmse_reject_threshold": RMSE_REJECT_THRESHOLD,
    }
    if valid.size:
        summary.update(
            rmse_mean=float(valid.mean()),
            rmse_median=float(np.median(valid)),
            rmse_p95=float(np.percentile(valid, 95)),
            rmse_max=float(valid.max()),
            fraction_above_threshold=float((valid > RMSE_REJECT_THRESHOLD).mean()),
        )
    return summary


def l2bmin_mineral_ids(grouping_matrix: Optional[Path] = None) -> dict[str, list[int]]:
    """Map validatable endmembers to EMITL2BMIN ``group_*_mineral_id`` values."""
    table = pd.read_csv(grouping_matrix or L2BMIN_GROUPING_MATRIX_FILE)
    ids: dict[str, list[int]] = {}
    for name, column in L2BMIN_COLUMN.items():
        ids[name] = sorted(int(i) for i in table.loc[pd.to_numeric(table[column], errors="coerce") > 0, "Index"])
    for name, pattern in L2BMIN_FILENAME_PATTERN.items():
        hit = table["Filename"].astype(str).str.contains(pattern, flags=re.IGNORECASE, regex=True)
        ids[name] = sorted(int(i) for i in table.loc[hit, "Index"])
    return ids


def _r_squared(a: np.ndarray, b: np.ndarray) -> tuple[Optional[float], Optional[float], int]:
    ok = np.isfinite(a) & np.isfinite(b)
    n = int(ok.sum())
    if n < 3 or np.std(a[ok]) == 0 or np.std(b[ok]) == 0:
        return None, None, n
    r = float(np.corrcoef(a[ok], b[ok])[0, 1])
    return r * r, r, n


def compare_with_l2bmin(
    ds: xr.Dataset, l2bmin_path: Path, *, grouping_matrix: Optional[Path] = None
) -> dict[str, Any]:
    """Spatial R^2 between our abundances and EMITL2BMIN band depths on our grid.

    For each validatable endmember the reference map is the L2B band depth of
    pixels whose group-1 or group-2 mineral ID belongs to that mineral (0 where
    another mineral or none was identified; the larger depth if both groups
    match). Only ``observable`` pixels are compared. Quartz-Feldspar and
    Water-Shade have no L2B counterpart.
    """
    from app.pipelines.ingestion import orthorectify_emit

    if ds.rio.crs is None:
        raise SpectralError("spectral dataset has no CRS; cannot align EMITL2BMIN")
    template = ds["rmse"].rio.write_crs(ds.rio.crs)
    bbox = template.rio.transform_bounds("EPSG:4326")

    layers = {}
    for group in (1, 2):
        for kind in ("band_depth", "mineral_id"):
            name = f"group_{group}_{kind}"
            da = orthorectify_emit(Path(l2bmin_path), bbox, variable=name)
            layers[name] = da.rio.reproject_match(template, resampling=Resampling.nearest).values

    ids = l2bmin_mineral_ids(grouping_matrix)
    observable = ds["observability"].values == OBSERVABLE
    results: dict[str, Any] = {}
    for name, mineral_ids in ids.items():
        if name not in ds["endmember"].values:
            continue
        depths = []
        for group in (1, 2):
            bd, mid = layers[f"group_{group}_band_depth"], layers[f"group_{group}_mineral_id"]
            depths.append(np.where(np.isfinite(mid), np.where(np.isin(mid, mineral_ids), bd, 0.0), np.nan))
        reference = np.fmax(depths[0], depths[1])
        ours = ds["abundance"].sel(endmember=name).values.astype(np.float64)
        r2, r, n = _r_squared(np.where(observable, ours, np.nan), reference)
        results[name] = {
            "r2": r2,
            "pearson_r": r,
            "n_pixels": n,
            "n_l2bmin_detections": int(np.nansum(reference > 0)),
            "l2bmin_mineral_ids": mineral_ids,
        }
    results["not_comparable"] = [e for e in ds["endmember"].values.tolist() if e not in ids]
    return results


def load_reflectance(path: Path) -> xr.DataArray:
    """Open a Phase 1 EMIT output (Zarr with ``reflectance`` + ``wavelength``, or GeoTIFF)."""
    path = Path(path)
    if path.suffix == ".zarr":
        # decode_coords="all" re-attaches the grid_mapping (CRS) variable as a coordinate.
        da = xr.open_zarr(path, decode_coords="all")["reflectance"].load()
    elif path.suffix.lower() in {".tif", ".tiff"}:
        da = rioxarray.open_rasterio(path, masked=True).load()
    else:
        raise SpectralError(f"unsupported reflectance input {path.name}")
    if "band" not in da.dims:
        raise SpectralError(f"{path.name} has no band dimension")
    return da


def write_spectral_zarr(ds: xr.Dataset, output_path: Path) -> None:
    import zarr

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if ds.rio.crs is not None:
        # Readers recover the CRS with xr.open_zarr(path, decode_coords="all").
        for name in ds.data_vars:
            ds[name].attrs["grid_mapping"] = "spatial_ref"
    compressor = zarr.Blosc(cname="zstd", clevel=5, shuffle=zarr.Blosc.BITSHUFFLE)
    encoding = {name: {"compressor": compressor} for name in ds.data_vars}
    if "reflectance" in ds:
        # One chunk holds every band of a 64 x 64 block, so a single-pixel spectrum is one read.
        encoding["reflectance"]["chunks"] = (ds.sizes["band"], min(64, ds.sizes["y"]), min(64, ds.sizes["x"]))
    ds.to_zarr(output_path, mode="w", consolidated=True, encoding=encoding)


def _json_default(value: Any) -> Any:
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if math.isnan(value) else float(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    return str(value)


def run_spectral_pipeline(
    input_path: Path,
    output_path: Path,
    *,
    log_path: Optional[Path] = None,
    l2bmin_path: Optional[Path] = None,
    library_path: Optional[Path] = None,
    settings: Optional[Settings] = None,
) -> dict[str, Any]:
    """Unmix ``input_path``, write ``output_path`` (Zarr) and an RMSE/validation JSON log."""
    rfl = load_reflectance(input_path)
    library = load_spectral_library(library_path, settings=settings)
    ds = process_reflectance(rfl, library=library)
    write_spectral_zarr(ds, Path(output_path))

    summary: dict[str, Any] = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "input": str(input_path),
        "output": str(output_path),
        "source_origin": ds.attrs.get("source_origin"),
        "n_active_bands": ds.attrs["n_active_bands"],
        "endmember_library": f"{library.library} ({library.path.name})",
        "rmse": rmse_summary(ds),
        "mean_abundance_observable": {
            name: float(np.nanmean(np.where(ds["observability"].values == OBSERVABLE, ds["abundance"].sel(endmember=name).values, np.nan)))
            if (ds["observability"].values == OBSERVABLE).any()
            else None
            for name in library.endmembers
        },
        "l2bmin_validation": None,
    }
    if l2bmin_path is not None:
        summary["l2bmin_validation"] = {"file": str(l2bmin_path), **compare_with_l2bmin(ds, Path(l2bmin_path))}
    else:
        logger.warning("No EMITL2BMIN granule supplied; skipping spatial R^2 validation")

    if log_path is not None:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        Path(log_path).write_text(json.dumps(summary, indent=2, default=_json_default))
    rm = summary["rmse"]
    logger.info(
        "Unmixed %d/%d pixels: RMSE mean=%s p95=%s; %d vegetated, %d poor fit -> %s",
        rm["n_unmixed"],
        rm["n_pixels"],
        f"{rm['rmse_mean']:.4f}" if "rmse_mean" in rm else "n/a",
        f"{rm['rmse_p95']:.4f}" if "rmse_p95" in rm else "n/a",
        rm["n_vegetated"],
        rm["n_poor_fit"],
        output_path,
    )
    return summary


def find_l2bmin_for(raw_dir: Path, granule_id: Optional[str]) -> Optional[Path]:
    """Find the EMITL2BMIN NetCDF matching an L2A granule ID (same acquisition/orbit/scene)."""
    if not granule_id:
        return None
    match = re.search(r"_(\d{8}T\d{6}_\d+_\d+)", granule_id)
    if not match:
        return None
    hits = sorted((raw_dir / "EMITL2BMIN").glob(f"EMIT_L2B_MIN_*{match.group(1)}*.nc"))
    return hits[0] if hits else None
