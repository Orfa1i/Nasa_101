"""Verification checks for the EMIT spectroscopy engine (band mapping, NNLS unmixing, SAI).

Synthetic cubes are linear mixtures of the bundled USGS endmember spectra on
the real EMIT wavelength grid, so the true abundances are known exactly.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import rioxarray  # noqa: F401
import xarray as xr
from pyproj import Transformer

from app import config, main
from app.config import Settings
from app.pipelines import unmixing as u

TO_UTM = Transformer.from_crs("EPSG:4326", "EPSG:32612", always_xy=True)
TO_LONLAT = Transformer.from_crs("EPSG:32612", "EPSG:4326", always_xy=True)


@pytest.fixture(scope="module")
def wavelengths():
    return u.load_emit_wavelengths()[0]


@pytest.fixture(scope="module")
def library():
    return u.load_spectral_library(u.SPLIB06_EXTRACT_FILE)


@pytest.fixture(scope="module")
def M(wavelengths, library):
    return u.build_endmember_matrix(wavelengths[u.active_band_mask(wavelengths)], library)


@pytest.fixture(scope="module")
def M_full(wavelengths, library):
    """Endmembers on all 285 channels (incl. vapour bands) to synthesise full cubes."""
    return u.build_endmember_matrix(wavelengths, library)


@pytest.fixture
def settings(tmp_path):
    return Settings(DATA_DIR=str(tmp_path / "data"))


def vegetation_spectrum(wavelengths):
    """Red-edge shaped spectrum (not in the library): NDVI ~ 0.8."""
    return np.interp(wavelengths, [400, 680, 750, 2500], [0.05, 0.05, 0.45, 0.30])


def truth_maps(ny=30, nx=40):
    """Known abundance maps (endmember, y, x) summing to 1."""
    gx = np.linspace(0.0, 0.3, nx)[None, :].repeat(ny, axis=0)
    jy = np.linspace(0.0, 0.3, ny)[:, None].repeat(nx, axis=1)
    f = np.zeros((7, ny, nx))
    f[u.ENDMEMBERS.index("Jarosite")] = jy
    f[u.ENDMEMBERS.index("Goethite")] = gx
    f[u.ENDMEMBERS.index("Montmorillonite")] = 0.1
    f[u.ENDMEMBERS.index("Water-Shade")] = 0.1
    f[u.ENDMEMBERS.index("Quartz-Feldspar")] = 0.8 - gx - jy
    return f


def make_cube(M_full, wavelengths, fractions, *, x0=None, y0=None, res=60.0, vegetated=(), nan_pixels=()):
    """Synthetic EMIT cube on a 60 m UTM 12N grid near Sierrita."""
    _, ny, nx = fractions.shape
    if x0 is None:
        x0, y0 = TO_UTM.transform(-111.11, 31.89)
    data = np.einsum("bk,kyx->byx", M_full, fractions)
    for iy, ix in vegetated:
        data[:, iy, ix] = vegetation_spectrum(wavelengths)
    for iy, ix in nan_pixels:
        data[:, iy, ix] = np.nan
    x = x0 + (np.arange(nx) + 0.5) * res
    y = y0 - (np.arange(ny) + 0.5) * res
    da = xr.DataArray(
        data.astype(np.float32),
        dims=("band", "y", "x"),
        coords={"band": np.arange(1, 286), "wavelength": ("band", wavelengths.astype(np.float32)), "x": x, "y": y},
        name="reflectance",
        attrs={"origin": "mock", "data_label": "MOCK INPUT - not an authentic observation", "granule_id": "EMIT_L2A_RFL_001_20240501T180000_2412212_005"},
    )
    return da.rio.write_crs("EPSG:32612")


# --------------------------------------------------------------------------- #
# 1. Band mapping
# --------------------------------------------------------------------------- #


def test_emit_grid_has_285_increasing_bands(wavelengths):
    assert wavelengths.size == u.EMIT_N_BANDS == 285
    assert np.all(np.diff(wavelengths) > 0)
    assert 380 < wavelengths[0] < 382 and 2492 < wavelengths[-1] < 2494
    assert 7.0 < np.median(np.diff(wavelengths)) < 7.8


def test_water_vapor_windows_excluded_exactly(wavelengths):
    active = u.active_band_mask(wavelengths)
    in_windows = ((wavelengths >= 1340) & (wavelengths <= 1460)) | ((wavelengths >= 1790) & (wavelengths <= 1970))
    np.testing.assert_array_equal(active, ~in_windows)
    # On EMIT's calibration these windows remove 40 channels, leaving 245 (not the 242
    # quoted in the playbook; EMIT's own good_wavelengths flag keeps 244).
    assert active.sum() == 245
    assert not np.any(active & in_windows)


def test_nearest_band_matches_without_interpolation(wavelengths):
    for target in (640.0, 860.0, 1240.0):
        idx = u.nearest_band(wavelengths, target)
        assert abs(wavelengths[idx] - target) <= 15.0
        assert idx == int(np.argmin(np.abs(wavelengths - target)))
    sparse = wavelengths[(wavelengths < 1200) | (wavelengths > 1300)]
    with pytest.raises(u.SpectralError, match="no channel within"):
        u.nearest_band(sparse, 1240.0)


# --------------------------------------------------------------------------- #
# 2. Library and endmember matrix
# --------------------------------------------------------------------------- #


def test_library_has_all_endmembers_with_provenance(library):
    assert library.endmembers == u.ENDMEMBERS
    assert library.library == "splib06a"
    assert "GDS99" in library.titles["Jarosite"] and "SWy-1" in library.titles["Montmorillonite"]
    for wl, refl in library.spectra.values():
        assert wl.min() < 381 and wl.max() > 2493
        assert refl.min() >= 0 and refl.max() <= 1


def test_splib07_extract_takes_priority(settings, tmp_path):
    reference = settings.data_path / "reference"
    reference.mkdir()
    rows = [(name, w, 0.5) for name in u.ENDMEMBERS for w in (300.0, 2600.0)]
    pd.DataFrame(rows, columns=["endmember", "wavelength_nm", "reflectance"]).assign(library="splib07a").to_csv(
        reference / u.SPLIB07_EXTRACT_NAME, index=False
    )
    lib = u.load_spectral_library(settings=settings)
    assert lib.library == "splib07a" and lib.path.parent == reference


def test_library_missing_endmember_rejected(tmp_path):
    path = tmp_path / "lib.csv"
    pd.DataFrame({"endmember": ["Jarosite"] * 2, "wavelength_nm": [300, 2600], "reflectance": [0.2, 0.3]}).to_csv(path, index=False)
    with pytest.raises(u.SpectralError, match="no spectra for endmember"):
        u.load_spectral_library(path)


def test_endmember_matrix_aligned_to_active_channels(M, wavelengths, library):
    active_wl = wavelengths[u.active_band_mask(wavelengths)]
    assert M.shape == (245, 7)
    assert np.isfinite(M).all() and M.min() >= 0 and M.max() <= 1
    lib_wl, lib_refl = library.spectra["Gypsum"]
    np.testing.assert_allclose(M[:, 4], np.interp(active_wl, lib_wl, lib_refl))
    with pytest.raises(u.SpectralError, match="covers"):
        u.build_endmember_matrix(np.array([300.0, 3000.0]), library)


# --------------------------------------------------------------------------- #
# 3. NNLS unmixing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("k", range(7))
def test_pure_reference_spectra_unmix_above_95_percent(M, k):
    f, rmse = u.unmix_spectrum(M[:, k], M)
    assert f[k] >= 0.95
    assert np.delete(f, k).sum() <= 0.05
    assert rmse < 1e-6


def test_noise_free_mixtures_recovered_exactly(M):
    rng = np.random.default_rng(1)
    for _ in range(50):
        truth = rng.dirichlet(np.ones(7)) * rng.uniform(0.5, 1.0)
        f, rmse = u.unmix_spectrum(M @ truth, M)
        np.testing.assert_allclose(f, truth, atol=1e-6)
        assert rmse < 1e-8


def test_noisy_mixtures_meet_rmse_and_simplex_constraints(M):
    rng = np.random.default_rng(2)
    mineral = [u.ENDMEMBERS.index(n) for n in u.ENDMEMBERS if n != "Water-Shade"]
    for _ in range(200):
        truth = rng.dirichlet(np.ones(7)) * rng.uniform(0.6, 1.0)
        f, rmse = u.unmix_spectrum(M @ truth + rng.normal(0, 0.002, M.shape[0]), M)
        assert (f >= 0).all() and f.sum() <= 1 + 1e-9
        assert rmse < 0.02
        # Minerals are well separated; the near-zero Water-Shade spectrum is weakly constrained.
        assert np.abs(f[mineral] - truth[mineral]).max() < 0.03


def test_sum_constraint_enforced_for_bright_pixels(M):
    f, _ = u.unmix_spectrum(1.5 * M[:, 0], M)
    assert (f >= 0).all()
    assert f.sum() == pytest.approx(1.0, abs=1e-9)


def test_unmix_edge_cases(M):
    f, rmse = u.unmix_spectrum(np.full(M.shape[0], np.nan), M)
    assert np.isnan(f).all() and np.isnan(rmse)
    f, rmse = u.unmix_spectrum(np.zeros(M.shape[0]), M)
    assert (f == 0).all() and rmse == 0
    r = M[:, 1].copy()
    r[::3] = np.nan  # a third of the channels missing: still solvable
    f, rmse = u.unmix_spectrum(r, M)
    assert f[1] > 0.99 and rmse < 1e-6
    r = np.full(M.shape[0], np.nan)
    r[:10] = M[:10, 1]  # fewer than 2 * n_endmembers usable channels
    assert np.isnan(u.unmix_spectrum(r, M)[0]).all()


# --------------------------------------------------------------------------- #
# 4. Cube processing, indices and SAI
# --------------------------------------------------------------------------- #


def test_process_reflectance_recovers_maps_and_sai(M_full, wavelengths, library):
    truth = truth_maps()
    rfl = make_cube(M_full, wavelengths, truth, vegetated=[(0, 0), (5, 7)], nan_pixels=[(29, 39)])
    ds = u.process_reflectance(rfl, library=library)

    # Same grid and CRS as the input.
    np.testing.assert_array_equal(ds.x.values, rfl.x.values)
    np.testing.assert_array_equal(ds.y.values, rfl.y.values)
    assert ds.rio.crs.to_epsg() == 32612
    assert ds.attrs["n_active_bands"] == 245 and ds.attrs["endmember_library"] == "splib06a"
    assert ds.attrs["source_origin"] == "mock"

    flags = ds["observability"].values
    assert flags[0, 0] == u.VEGETATED and flags[5, 7] == u.VEGETATED
    assert flags[29, 39] == u.NO_DATA
    observable = flags == u.OBSERVABLE
    assert observable.sum() == 30 * 40 - 3

    np.testing.assert_allclose(ds["abundance"].values[:, observable], truth[:, observable], atol=1e-4)
    assert float(ds["rmse"].values[observable].max()) < 1e-4
    assert (ds["confidence"].values[observable] > 0.99).all()

    # Vegetated / no-data: SAI masked, confidence zero.
    assert np.isnan(ds["sai"].values[~observable]).all()
    assert (ds["confidence"].values[~observable] == 0).all()
    assert (ds["ndvi"].values[0, 0] > 0.35) and (ds["ndvi"].values[observable] < 0.35).all()

    # SAI follows the formula with the cube's own NDWI_SWIR (R860 vs R1240, nearest bands).
    i860, i1240 = u.nearest_band(wavelengths, 860), u.nearest_band(wavelengths, 1240)
    cube = rfl.values.astype(np.float64)
    ndwi = (cube[i860] - cube[i1240]) / (cube[i860] + cube[i1240])
    names = list(u.ENDMEMBERS)
    expected = 0.40 * truth[names.index("Jarosite")] + 0.30 * truth[names.index("Montmorillonite")] + 0.20 * truth[names.index("Goethite")] + 0.10 * ndwi
    np.testing.assert_allclose(ds["sai"].values[observable], expected[observable], atol=1e-4)


def test_integer_scaled_reflectance_is_rescaled(M_full, wavelengths, library):
    truth = truth_maps(4, 5)
    rfl = make_cube(M_full, wavelengths, truth) * 10000.0
    ds = u.process_reflectance(rfl, library=library)
    assert ds.attrs["reflectance_scale_divisor"] == 10000.0
    np.testing.assert_allclose(ds["abundance"].values, truth, atol=1e-4)


def test_poor_fit_pixels_get_zero_confidence(M_full, wavelengths, library):
    truth = truth_maps(3, 3)
    rfl = make_cube(M_full, wavelengths, truth)
    spike = rfl.values.copy()
    spike[::2, 1, 1] += 0.3  # non-physical saw-tooth spectrum: no mixture fits it
    rfl = rfl.copy(data=spike)
    ds = u.process_reflectance(rfl, library=library)
    assert ds["rmse"].values[1, 1] > u.RMSE_REJECT_THRESHOLD
    assert ds["observability"].values[1, 1] == u.POOR_FIT and ds["confidence"].values[1, 1] == 0
    assert np.isfinite(ds["sai"].values[1, 1])  # kept, but flagged


def test_cube_without_wavelengths_uses_bundled_grid(M_full, wavelengths, library):
    rfl = make_cube(M_full, wavelengths, truth_maps(2, 2)).drop_vars("wavelength")
    ds = u.process_reflectance(rfl, library=library)
    assert ds.attrs["n_active_bands"] == 245
    with pytest.raises(u.SpectralError, match="cannot infer wavelengths"):
        u.process_reflectance(rfl.isel(band=slice(0, 100)), library=library)


# --------------------------------------------------------------------------- #
# 5. Zarr output, RMSE log, EMITL2BMIN validation, CLI
# --------------------------------------------------------------------------- #


def write_input_zarr(rfl: xr.DataArray, path: Path) -> Path:
    rfl.to_dataset(name="reflectance").to_zarr(path, mode="w")
    return path


def make_l2bmin(path: Path, truth_fn, *, res=0.0006, lon0=-111.115, lat0=31.895, nx=50, ny=40, goethite_id=3, jarosite_id=205):
    """Synthetic EMIT_L2B_MIN NetCDF: identity GLT on an EPSG:4326 grid, IDs from the 20230503 matrix."""
    lon = lon0 + (np.arange(nx) + 0.5) * res
    lat = lat0 - (np.arange(ny) + 0.5) * res
    LON, LAT = np.meshgrid(lon, lat)
    gt, jar = truth_fn(*TO_UTM.transform(LON, LAT))
    g1_id = np.where(gt > 0.02, goethite_id, 0).astype(np.int16)
    g1_bd = np.where(gt > 0.02, 0.5 * gt, 0.0).astype(np.float32)
    g2_id = np.where(jar > 0.02, jarosite_id, 0).astype(np.int16)
    g2_bd = np.where(jar > 0.02, 0.4 * jar, 0.0).astype(np.float32)
    g1_id[:2, :2], g1_bd[:2, :2] = -9999, -9999  # masked corner
    dims = ("downtrack", "crosstrack")
    root = xr.Dataset({"group_1_band_depth": (dims, g1_bd), "group_1_mineral_id": (dims, g1_id),
                       "group_2_band_depth": (dims, g2_bd), "group_2_mineral_id": (dims, g2_id)})
    root.attrs["geotransform"] = np.array([lon0, res, 0.0, lat0, 0.0, -res])
    root.to_netcdf(path, engine="h5netcdf", mode="w")
    jj, ii = np.meshgrid(np.arange(nx), np.arange(ny))
    xr.Dataset({"glt_x": (("ortho_y", "ortho_x"), (jj + 1).astype(np.int32)),
                "glt_y": (("ortho_y", "ortho_x"), (ii + 1).astype(np.int32))}).to_netcdf(path, engine="h5netcdf", mode="a", group="location")
    return path


def test_l2bmin_ids_map_to_grouping_matrix():
    ids = u.l2bmin_mineral_ids()
    assert 3 in ids["Goethite"] and 138 in ids["Gypsum"] and 141 in ids["Montmorillonite"]
    assert {204, 205, 206} <= set(ids["Jarosite"]) and 29 in ids["Ferrihydrite"]


def test_run_pipeline_writes_zarr_log_and_l2bmin_r2(M_full, wavelengths, tmp_path, settings):
    ny, nx, res = 30, 40, 60.0
    x0, y0 = TO_UTM.transform(-111.11, 31.89)
    truth = truth_maps(ny, nx)

    def truth_fn(x, y):  # the same ramps as truth_maps, as functions of UTM position
        col = np.clip((x - x0) / res - 0.5, 0, nx - 1)
        row = np.clip((y0 - y) / res - 0.5, 0, ny - 1)
        return 0.3 * col / (nx - 1), 0.3 * row / (ny - 1)

    rfl = make_cube(M_full, wavelengths, truth, x0=x0, y0=y0, vegetated=[(0, 0)])
    src = write_input_zarr(rfl, tmp_path / "sierrita_emit_l2a_rfl.zarr")
    l2bmin = make_l2bmin(tmp_path / "EMIT_L2B_MIN_001_20240501T180000_2412212_005.nc", truth_fn)
    out = settings.cache_dir / "Sierrita_spectral.zarr"
    log = settings.log_dir / "Sierrita_spectral_rmse.json"

    summary = u.run_spectral_pipeline(src, out, log_path=log, l2bmin_path=l2bmin, settings=settings)

    ds = xr.open_zarr(out, decode_coords="all")
    assert set(ds.data_vars) >= {"abundance", "rmse", "sai", "ndvi", "ndwi_swir", "confidence", "observability", "fraction_sum"}
    assert ds["abundance"].encoding["compressor"].cname == "zstd"
    assert ds.rio.crs.to_epsg() == 32612
    assert list(ds["endmember"].values) == list(u.ENDMEMBERS)

    logged = json.loads(log.read_text())
    assert logged["rmse"]["n_unmixed"] == ny * nx and logged["rmse"]["n_vegetated"] == 1
    assert logged["rmse"]["rmse_p95"] < 1e-4  # only the vegetated pixel fits badly
    assert logged["rmse"]["rmse_max"] > u.RMSE_REJECT_THRESHOLD
    assert logged["n_active_bands"] == 245 and logged["source_origin"] == "mock"
    val = logged["l2bmin_validation"]
    assert val["Goethite"]["r2"] > 0.95 and val["Jarosite"]["r2"] > 0.95
    assert val["Goethite"]["n_l2bmin_detections"] > 0
    assert val["Gypsum"]["r2"] is None  # no gypsum anywhere: R^2 undefined, not faked
    assert set(val["not_comparable"]) == {"Quartz-Feldspar", "Water-Shade"}
    assert summary["l2bmin_validation"]["Goethite"]["r2"] == val["Goethite"]["r2"]


def test_find_l2bmin_for_granule(tmp_path):
    folder = tmp_path / "EMITL2BMIN"
    folder.mkdir()
    target = folder / "EMIT_L2B_MIN_001_20240501T180000_2412212_005.nc"
    target.touch()
    assert u.find_l2bmin_for(tmp_path, "EMIT_L2A_RFL_001_20240501T180000_2412212_005") == target
    assert u.find_l2bmin_for(tmp_path, "EMIT_L2A_RFL_001_20240601T180000_2415312_003") is None
    assert u.find_l2bmin_for(tmp_path, None) is None


@pytest.fixture
def cli_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "cli_data"))
    config.get_settings.cache_clear()
    yield config.get_settings()
    config.get_settings.cache_clear()


def test_cli_unmix_end_to_end(cli_settings, M_full, wavelengths):
    rfl = make_cube(M_full, wavelengths, truth_maps(6, 8))
    facility_dir = cli_settings.processed_dir / "sierrita"
    facility_dir.mkdir(parents=True)
    write_input_zarr(rfl, facility_dir / "sierrita_emit_l2a_rfl.zarr")

    assert main.main(["unmix", "--facility", "Sierrita"]) == 0
    out = cli_settings.cache_dir / "Sierrita_spectral.zarr"
    assert out.exists()
    logs = list(cli_settings.log_dir.glob("Sierrita_spectral_rmse_*.json"))
    assert len(logs) == 1 and json.loads(logs[0].read_text())["l2bmin_validation"] is None
    assert "skipping spatial R^2 validation" in next(cli_settings.log_dir.glob("unmix_*.log")).read_text()


def test_cli_unmix_without_ingest_output_fails(cli_settings):
    assert main.main(["unmix", "--facility", "Mission"]) == 1


def test_ingest_then_unmix_chain(cli_settings, M_full, wavelengths, monkeypatch, tmp_path):
    """Phase 1 output (EMIT NetCDF -> GLT ortho -> UTM Zarr) feeds Phase 2 unchanged."""
    import earthaccess
    import requests

    from app.pipelines import ingestion

    # EMIT-style L2A NetCDF on an EPSG:4326 grid (identity GLT) with a known goethite ramp.
    res, lon0, lat0, nx, ny = 0.0006, -111.13, 31.91, 180, 140
    gx = np.linspace(0.0, 0.4, nx)[None, :].repeat(ny, axis=0)
    f = np.zeros((7, ny, nx))
    f[u.ENDMEMBERS.index("Goethite")] = gx
    f[u.ENDMEMBERS.index("Quartz-Feldspar")] = 0.9 - gx
    f[u.ENDMEMBERS.index("Water-Shade")] = 0.1
    swath = np.einsum("bk,kyx->yxb", M_full, f).astype(np.float32)  # (downtrack, crosstrack, bands)
    nc = tmp_path / "EMIT_L2A_RFL_001_20240501T180000_2412212_005.nc"
    root = xr.Dataset({"reflectance": (("downtrack", "crosstrack", "bands"), swath)})
    root.attrs["geotransform"] = np.array([lon0, res, 0.0, lat0, 0.0, -res])
    root.to_netcdf(nc, engine="h5netcdf", mode="w")
    jj, ii = np.meshgrid(np.arange(nx), np.arange(ny))
    xr.Dataset({"glt_x": (("ortho_y", "ortho_x"), (jj + 1).astype(np.int32)),
                "glt_y": (("ortho_y", "ortho_x"), (ii + 1).astype(np.int32))}).to_netcdf(nc, engine="h5netcdf", mode="a", group="location")
    xr.Dataset({"wavelengths": (("bands",), wavelengths.astype(np.float32))}).to_netcdf(
        nc, engine="h5netcdf", mode="a", group="sensor_band_parameters"
    )

    def offline(**_):
        raise requests.exceptions.ConnectionError("offline")

    monkeypatch.setattr(earthaccess, "search_data", offline)
    monkeypatch.setattr(ingestion, "_logged_in", False)
    mock_dir = cli_settings.mock_dir / "EMITL2ARFL.001"
    mock_dir.mkdir(parents=True)
    granule = {"umm": {"GranuleUR": nc.stem, "CloudCover": 0,
                       "SpatialExtent": {"HorizontalSpatialDomain": {"Geometry": {"BoundingRectangles": [
                           {"WestBoundingCoordinate": lon0, "EastBoundingCoordinate": lon0 + nx * res,
                            "SouthBoundingCoordinate": lat0 - ny * res, "NorthBoundingCoordinate": lat0}]}}}},
               "local_asset": str(nc)}
    (mock_dir / "g.json").write_text(json.dumps(granule))

    assert main.main(["ingest", "--facility", "Sierrita", "--datasets", "EMITL2ARFL.001", "--format", "zarr"]) == 0
    assert main.main(["unmix", "--facility", "Sierrita"]) == 0

    ds = xr.open_zarr(cli_settings.cache_dir / "Sierrita_spectral.zarr", decode_coords="all")
    assert ds.rio.crs.to_epsg() == 32612
    assert ds.attrs["n_active_bands"] == 245 and ds.attrs["source_origin"] == "mock"
    observable = ds["observability"].values == u.OBSERVABLE
    assert observable.mean() > 0.9
    # Bilinear resampling of linear mixtures is still a linear mixture: abundances stay consistent.
    goethite = ds["abundance"].sel(endmember="Goethite").values[observable]
    qf = ds["abundance"].sel(endmember="Quartz-Feldspar").values[observable]
    assert 0.0 <= goethite.min() and goethite.max() <= 0.4 + 1e-3
    np.testing.assert_allclose(goethite + qf, 0.9, atol=1e-3)
    assert float(ds["rmse"].values[observable].max()) < 1e-3
