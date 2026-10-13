"""Verification checks for configuration, the ingestion pipeline and the ingest CLI.

Network access is never required: ``earthaccess`` is stubbed for the online
path and synthetic granules (GeoTIFF / NetCDF / EMIT-style NetCDF) with known,
analytically defined values are used to check reprojection and clipping. The
single live CMR test is opt-in via ``LITHOCREEP_LIVE_TESTS=1``.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

import numpy as np
import pyogrio
import pytest
import requests
import rioxarray
import xarray as xr
from pyproj import Transformer

from app import config, main
from app.config import Settings
from app.pipelines import ingestion
from app.pipelines.ingestion import (
    CoverageError,
    PRODUCTS,
    download_and_crop,
    orthorectify_emit,
    select_best_granule,
    validate_bbox,
)

SIERRITA = (-111.12, 31.83, -111.04, 31.90)
BRUMADINHO = (-44.15, -20.14, -44.10, -20.10)
EMIT_ID, DISP_ID, DSWX_ID = "EMITL2ARFL.001", "OPERA_L3_DISP-S1_V1", "OPERA_L3_DSWX-HLS_V1.0"

TO_LONLAT = Transformer.from_crs("EPSG:32612", "EPSG:4326", always_xy=True)
TO_UTM = Transformer.from_crs("EPSG:4326", "EPSG:32612", always_xy=True)


def linear_field(lon, lat):
    """Analytic test signal: bilinear resampling reproduces it exactly."""
    return (np.asarray(lon) + 111.1) * 100.0 + (np.asarray(lat) - 31.85) * 50.0


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr(ingestion, "_logged_in", False)
    return Settings(DATA_DIR=str(tmp_path / "data"), EARTHDATA_USERNAME="tester", EARTHDATA_PASSWORD="secret")


@pytest.fixture
def geographic_tif(tmp_path):
    """EPSG:4326 float raster around Sierrita whose values follow ``linear_field``."""
    res = 0.0005
    lon = np.arange(-111.25 + res / 2, -110.90, res)
    lat = np.arange(32.00 - res / 2, 31.75, -res)
    data = linear_field(*np.meshgrid(lon, lat)).astype(np.float32)
    da = xr.DataArray(data, dims=("y", "x"), coords={"x": lon, "y": lat})
    path = tmp_path / "src" / "EMIT_L2A_RFL_fixture.tif"
    path.parent.mkdir()
    da.rio.write_crs("EPSG:4326").rio.write_nodata(np.nan, encoded=False).rio.to_raster(path)
    return path


@pytest.fixture
def dswx_tif(tmp_path):
    """DSWx-style categorical WTR layer (uint8, nodata 255) natively in UTM 12N."""
    res = 30.0
    x0, y0 = TO_UTM.transform(-111.2, 31.97)
    x = np.arange(x0, x0 + 600 * res, res) + res / 2  # 18 km: covers the whole Sierrita bbox
    y = np.arange(y0, y0 - 600 * res, -res) - res / 2
    data = ((np.arange(600)[:, None] // 20 + np.arange(600)[None, :] // 20) % 3).astype(np.uint8)
    data[:10, :] = 255
    da = xr.DataArray(data, dims=("y", "x"), coords={"x": x, "y": y})
    path = tmp_path / "src" / "OPERA_L3_DSWx-HLS_T12RVV_B01_WTR.tif"
    path.parent.mkdir(exist_ok=True)
    da.rio.write_crs("EPSG:32612").rio.write_nodata(255).rio.to_raster(path)
    return path


@pytest.fixture
def disp_nc(tmp_path):
    """DISP-S1-style NetCDF: projected (UTM 12N) ``displacement`` + ``spatial_ref``."""
    res = 30.0
    x0, y0 = TO_UTM.transform(-111.2, 31.97)
    x = np.arange(x0, x0 + 600 * res, res) + res / 2  # 18 km: covers the whole Sierrita bbox
    y = np.arange(y0, y0 - 600 * res, -res) - res / 2
    lon, lat = TO_LONLAT.transform(*np.meshgrid(x, y))
    disp = xr.DataArray(linear_field(lon, lat).astype(np.float32), dims=("y", "x"), coords={"x": x, "y": y})
    ds = disp.rio.write_crs("EPSG:32612").to_dataset(name="displacement")
    path = tmp_path / "src" / "OPERA_L3_DISP-S1_IW_F12345_VV_20230101_20230113_v1.0.nc"
    path.parent.mkdir(exist_ok=True)
    ds.to_netcdf(path, engine="h5netcdf")
    return path


@pytest.fixture
def emit_nc(tmp_path):
    """EMIT L2A RFL-style NetCDF: swath reflectance + GLT lookup to an EPSG:4326 grid.

    The swath is the ortho grid transposed and flipped, so a wrong GLT
    convention (0/1-based, x/y swap) yields wrong values.
    """
    res = 0.0006
    x0, y0 = -111.20, 31.96
    ny, nx, n_bands = 300, 340, 5
    lon = x0 + (np.arange(nx) + 0.5) * res
    lat = y0 - (np.arange(ny) + 0.5) * res
    base = linear_field(*np.meshgrid(lon, lat))  # (ny, nx) ortho truth
    bands = np.arange(n_bands, dtype=np.float32)

    # swath[downtrack=j, crosstrack=ny-1-i, band] = ortho[i, j] + band
    swath = np.empty((nx, ny, n_bands), dtype=np.float32)
    for b in range(n_bands):
        swath[:, :, b] = (base + b).T[:, ::-1]
    ii, jj = np.meshgrid(np.arange(ny), np.arange(nx), indexing="ij")
    glt_x = (ny - 1 - ii + 1).astype(np.int32)  # crosstrack, 1-based
    glt_y = (jj + 1).astype(np.int32)  # downtrack, 1-based
    glt_x[:5, :5] = 0
    glt_y[:5, :5] = 0  # unmapped corner

    path = tmp_path / "src" / "EMIT_L2A_RFL_001_20240501T180000_2412212_005.nc"
    path.parent.mkdir(exist_ok=True)
    root = xr.Dataset({"reflectance": (("downtrack", "crosstrack", "bands"), swath)})
    root.attrs["geotransform"] = np.array([x0, res, 0.0, y0, 0.0, -res])
    root.to_netcdf(path, engine="h5netcdf", mode="w")
    xr.Dataset({"glt_x": (("ortho_y", "ortho_x"), glt_x), "glt_y": (("ortho_y", "ortho_x"), glt_y)}).to_netcdf(
        path, engine="h5netcdf", mode="a", group="location"
    )
    xr.Dataset({"wavelengths": (("bands",), 400.0 + 7.4 * bands)}).to_netcdf(
        path, engine="h5netcdf", mode="a", group="sensor_band_parameters"
    )
    return path, (lon, lat, base)


def make_granule(gid, *, cloud=None, rect=None, date="2024-05-01T18:00:00Z", links=(), asset=None):
    umm = {"GranuleUR": gid, "TemporalExtent": {"RangeDateTime": {"BeginningDateTime": date}}}
    if cloud is not None:
        umm["CloudCover"] = cloud
    if rect is not None:
        w, s, e, n = rect
        umm["SpatialExtent"] = {
            "HorizontalSpatialDomain": {
                "Geometry": {
                    "BoundingRectangles": [
                        {"WestBoundingCoordinate": w, "SouthBoundingCoordinate": s,
                         "EastBoundingCoordinate": e, "NorthBoundingCoordinate": n}
                    ]
                }
            }
        }
    umm["RelatedUrls"] = [{"Type": "GET DATA", "URL": link} for link in links]
    granule = {"umm": umm}
    if asset is not None:
        granule["local_asset"] = str(asset)
    return granule


def write_mock(settings, dataset_id, records):
    folder = settings.mock_dir / dataset_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "granules.json").write_text(json.dumps(records))


def offline(monkeypatch):
    import earthaccess

    def boom(**_):
        raise requests.exceptions.ConnectionError("cmr.earthdata.nasa.gov unreachable")

    monkeypatch.setattr(earthaccess, "search_data", boom)


def utm_aoi_bounds(bbox):
    xs, ys = TO_UTM.transform([bbox[0], bbox[2], bbox[0], bbox[2]], [bbox[1], bbox[1], bbox[3], bbox[3]])
    return min(xs), min(ys), max(xs), max(ys)


def assert_clipped_to_aoi(da, bbox):
    """Output must lie on the UTM grid within one pixel of the AOI's bounds."""
    assert da.rio.crs.to_epsg() == 32612
    res = max(abs(r) for r in da.rio.resolution())
    xmin, ymin, xmax, ymax = da.rio.bounds()
    axmin, aymin, axmax, aymax = utm_aoi_bounds(bbox)
    assert axmin - res <= xmin <= axmin + res
    assert aymin - res <= ymin <= aymin + res
    assert axmax - res <= xmax <= axmax + res
    assert aymax - res <= ymax <= aymax + res


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


def test_settings_reads_environment_and_creates_data_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("EARTHDATA_USERNAME", "env-user")
    monkeypatch.setenv("EARTHDATA_PASSWORD", "env-pass")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "store"))
    s = Settings()
    assert s.EARTHDATA_USERNAME == "env-user" and s.has_earthdata_credentials
    assert s.data_path == (tmp_path / "store").resolve()
    for sub in config.DATA_SUBDIRS:
        assert (s.data_path / sub).is_dir()


def test_settings_defaults(tmp_path, monkeypatch):
    for var in ("EARTHDATA_USERNAME", "EARTHDATA_PASSWORD", "DATA_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(config, "BACKEND_DIR", tmp_path / "backend")
    s = Settings(_env_file=None)  # ignore any developer .env
    assert s.EARTHDATA_USERNAME is None and s.EARTHDATA_PASSWORD is None
    assert not s.has_earthdata_credentials
    # "../data" is resolved against backend/, i.e. <repo>/data
    assert s.data_path == (tmp_path / "data").resolve()


def test_settings_rejects_unwritable_data_dir(tmp_path, monkeypatch):
    target = tmp_path / "locked"
    target.mkdir()
    real_access = os.access
    monkeypatch.setattr(
        config.os, "access", lambda p, mode: False if Path(p) == target else real_access(p, mode)
    )
    with pytest.raises(PermissionError, match="Write permission missing"):
        Settings(DATA_DIR=str(target))


def test_settings_rejects_file_as_data_dir(tmp_path):
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")
    with pytest.raises(PermissionError):
        Settings(DATA_DIR=str(blocker))


# --------------------------------------------------------------------------- #
# Product catalogue, bbox validation, granule selection
# --------------------------------------------------------------------------- #


def test_product_ids_match_exactly():
    assert set(PRODUCTS) == {EMIT_ID, DISP_ID, DSWX_ID}
    assert (PRODUCTS[EMIT_ID].short_name, PRODUCTS[EMIT_ID].version) == ("EMITL2ARFL", "001")
    assert (PRODUCTS[DSWX_ID].short_name, PRODUCTS[DSWX_ID].version) == ("OPERA_L3_DSWX-HLS_V1", "1.0")
    assert PRODUCTS[DISP_ID].short_name == "OPERA_L3_DISP-S1_V1" and PRODUCTS[DISP_ID].conus_only


def test_unknown_dataset_rejected(settings, tmp_path):
    with pytest.raises(ValueError, match="Unknown dataset_id"):
        asyncio.run(download_and_crop("EMITL2ARFL", SIERRITA, tmp_path / "x.tif", settings=settings))


@pytest.mark.parametrize(
    "bbox",
    [
        (-111.04, 31.83, -111.12, 31.90),  # xmin > xmax
        (-111.12, 31.90, -111.04, 31.83),  # ymin > ymax
        (-111.12, 31.83, -111.04),  # 3 values
        (-191.0, 31.83, -111.04, 31.90),  # lon out of range
        (-111.12, 31.83, -111.04, 95.0),  # lat out of range
        (-111.12, float("nan"), -111.04, 31.90),
    ],
)
def test_validate_bbox_rejects_bad_input(bbox):
    with pytest.raises(ValueError):
        validate_bbox(bbox)


def test_conus_check():
    assert ingestion.bbox_within_conus(SIERRITA)
    assert not ingestion.bbox_within_conus(BRUMADINHO)


def test_select_best_granule_prefers_full_cover_then_low_cloud():
    wide = (-111.3, 31.7, -110.9, 32.1)
    granules = [
        make_granule("cloudy_recent", cloud=40, rect=wide, date="2025-06-01T00:00:00Z"),
        make_granule("clear_full", cloud=5, rect=wide, date="2023-06-01T00:00:00Z"),
        make_granule("clear_partial", cloud=0, rect=(-111.10, 31.85, -110.9, 32.1)),
        make_granule("clear_elsewhere", cloud=0, rect=(-100.0, 40.0, -99.0, 41.0)),
    ]
    best = select_best_granule(granules, SIERRITA, optical=True)
    assert best["umm"]["GranuleUR"] == "clear_full"

    # Radar: cloud cover is irrelevant, most recent full-coverage pass wins.
    best = select_best_granule(granules, SIERRITA, optical=False)
    assert best["umm"]["GranuleUR"] == "cloudy_recent"

    assert select_best_granule(granules[3:], SIERRITA, optical=True) is None


# --------------------------------------------------------------------------- #
# Online path (earthaccess stubbed)
# --------------------------------------------------------------------------- #


def test_online_search_download_and_crop(settings, geographic_tif, monkeypatch, tmp_path):
    import earthaccess

    wide = (-111.3, 31.7, -110.9, 32.1)
    url = "https://data.lpdaac.earthdatacloud.nasa.gov/EMIT/EMIT_L2A_RFL_fixture.tif"
    granules = [
        make_granule("G_cloudy", cloud=60, rect=wide, links=[url]),
        make_granule("G_best", cloud=2, rect=wide, links=[url, url.replace("RFL_", "RFLUNCERT_")]),
    ]
    calls = {}

    def fake_search(**kwargs):
        calls["search"] = kwargs
        return granules

    def fake_download(links, local_path, **_):
        calls["download"] = list(links)
        dest = Path(local_path) / Path(links[0]).name
        shutil.copy(geographic_tif, dest)
        return [str(dest)]

    class Auth:
        authenticated = True

    monkeypatch.setattr(earthaccess, "search_data", fake_search)
    monkeypatch.setattr(earthaccess, "download", fake_download)
    monkeypatch.setattr(earthaccess, "login", lambda strategy="all", persist=False: Auth())

    out = settings.processed_dir / "sierrita" / "emit.tif"
    # EMIT_ID with a GeoTIFF asset exercises the generic raster path.
    monkeypatch.setitem(
        PRODUCTS, EMIT_ID, ingestion.ProductSpec(**{**PRODUCTS[EMIT_ID].__dict__, "asset_pattern": r"EMIT_L2A_RFL_[^/]*\.tif$"})
    )
    result = asyncio.run(download_and_crop(EMIT_ID, SIERRITA, out, settings=settings))

    assert calls["search"]["short_name"] == "EMITL2ARFL"
    assert calls["search"]["version"] == "001"
    assert calls["search"]["bounding_box"] == SIERRITA
    assert calls["search"]["temporal"][0].startswith("2022-01-01")
    assert calls["search"]["temporal"][1].startswith("2026-12-31")
    assert calls["download"] == [url]  # only the science asset, not RFLUNCERT

    assert result.status == "downloaded" and result.origin == "earthdata"
    assert result.granule_id == "G_best"
    assert Path(result.raw_paths[0]).parent == settings.raw_dir / "EMITL2ARFL"
    assert out.exists() and Path(f"{out}.meta.json").exists() and Path(result.aoi_path).exists()

    with rioxarray.open_rasterio(out, masked=True) as da:
        assert_clipped_to_aoi(da, SIERRITA)
        assert da.attrs["origin"] == "earthdata"
        values = da.isel(band=0).values
        xx, yy = np.meshgrid(da.x.values, da.y.values)
        lon, lat = TO_LONLAT.transform(xx, yy)
        inside = (lon > SIERRITA[0] + 0.002) & (lon < SIERRITA[2] - 0.002) & (lat > SIERRITA[1] + 0.002) & (lat < SIERRITA[3] - 0.002)
        assert np.isfinite(values[inside]).all()
        # Bilinear on a linear field is exact up to GDAL's approximate warp transformer
        # (error threshold 0.125 px); one source pixel spans 0.05 units here, so allow 0.2 px.
        np.testing.assert_allclose(values[inside], linear_field(lon, lat)[inside], atol=0.01)

    aoi = pyogrio.read_dataframe(result.aoi_path)
    np.testing.assert_allclose(aoi.total_bounds, SIERRITA, atol=1e-6)


def test_missing_credentials_fail_loudly(tmp_path, geographic_tif, monkeypatch):
    import earthaccess

    s = Settings(DATA_DIR=str(tmp_path / "d"), EARTHDATA_USERNAME=None, EARTHDATA_PASSWORD=None)
    monkeypatch.setattr(ingestion, "_logged_in", False)
    monkeypatch.setattr(
        earthaccess, "search_data",
        lambda **_: [make_granule("G", cloud=1, rect=(-112, 31, -110, 33), links=["https://x/a_B01_WTR.tif"])],
    )

    class NoAuth:
        authenticated = False

    monkeypatch.setattr(earthaccess, "login", lambda strategy="all", persist=False: NoAuth())
    with pytest.raises(ingestion.EarthdataAuthError):
        asyncio.run(download_and_crop(DSWX_ID, SIERRITA, tmp_path / "o.tif", settings=s))


# --------------------------------------------------------------------------- #
# Offline fallback (mock metadata)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("suffix", ["tif", "zarr"])
def test_offline_falls_back_to_mock_metadata(settings, geographic_tif, monkeypatch, suffix, caplog):
    offline(monkeypatch)
    monkeypatch.setitem(
        PRODUCTS, EMIT_ID, ingestion.ProductSpec(**{**PRODUCTS[EMIT_ID].__dict__, "asset_pattern": r"\.tif$"})
    )
    write_mock(settings, EMIT_ID, [make_granule("MOCK_EMIT_1", cloud=3, rect=(-111.3, 31.7, -110.9, 32.1), asset=geographic_tif)])

    out = settings.processed_dir / f"emit_mock.{suffix}"
    with caplog.at_level("WARNING", logger="lithocreep.ingestion"):
        result = asyncio.run(download_and_crop(EMIT_ID, SIERRITA, out, settings=settings))

    assert "Earthdata unreachable" in caplog.text
    assert result.status == "mock" and result.origin == "mock" and result.granule_id == "MOCK_EMIT_1"
    if suffix == "zarr":
        ds = xr.open_zarr(out)
        da = ds["reflectance"]
        assert ds["reflectance"].encoding["compressor"].cname == "zstd"
    else:
        da = rioxarray.open_rasterio(out, masked=True)
    assert_clipped_to_aoi(da, SIERRITA)
    assert da.attrs["origin"] == "mock"
    assert "MOCK" in da.attrs["data_label"]
    meta = json.loads(Path(f"{out}.meta.json").read_text())
    assert meta["origin"] == "mock" and meta["crs"] == "EPSG:32612"


def test_offline_without_mock_assets_returns_metadata_only(settings, monkeypatch, tmp_path):
    offline(monkeypatch)
    write_mock(settings, DSWX_ID, [make_granule("MOCK_DSWX", cloud=1, rect=(-112, 31, -110, 33), asset=tmp_path / "missing.tif")])
    result = asyncio.run(download_and_crop(DSWX_ID, SIERRITA, tmp_path / "o.tif", settings=settings))
    assert result.status == "metadata_only" and result.origin == "mock" and result.output_path is None
    assert not (tmp_path / "o.tif").exists()


def test_offline_with_no_mock_files(settings, monkeypatch, tmp_path):
    offline(monkeypatch)
    result = asyncio.run(download_and_crop(DSWX_ID, SIERRITA, tmp_path / "o.tif", settings=settings))
    assert result.status == "no_granules" and result.origin == "mock"


# --------------------------------------------------------------------------- #
# Product-specific readers
# --------------------------------------------------------------------------- #


def test_dswx_categorical_values_survive_reprojection(settings, dswx_tif, monkeypatch):
    offline(monkeypatch)
    write_mock(settings, DSWX_ID, [make_granule("MOCK_DSWX", cloud=1, rect=(-111.3, 31.7, -110.9, 32.1), asset=dswx_tif)])
    out = settings.processed_dir / "dswx.tif"
    result = asyncio.run(download_and_crop(DSWX_ID, SIERRITA, out, settings=settings))
    assert result.status == "mock"
    with rioxarray.open_rasterio(out) as da:
        assert da.dtype == np.uint8 and da.rio.nodata == 255
        assert set(np.unique(da.values)) <= {0, 1, 2, 255}
        assert_clipped_to_aoi(da, SIERRITA)


def test_disp_netcdf_reprojected_and_clipped(settings, disp_nc, monkeypatch):
    offline(monkeypatch)
    write_mock(settings, DISP_ID, [make_granule("MOCK_DISP", rect=(-111.3, 31.7, -110.9, 32.1), asset=disp_nc)])
    out = settings.processed_dir / "disp.tif"
    result = asyncio.run(download_and_crop(DISP_ID, SIERRITA, out, settings=settings))
    assert result.status == "mock" and result.granule_id == "MOCK_DISP"
    with rioxarray.open_rasterio(out, masked=True) as da:
        assert_clipped_to_aoi(da, SIERRITA)
        lon, lat = TO_LONLAT.transform(*np.meshgrid(da.x.values, da.y.values))
        values = da.isel(band=0).values
        ok = np.isfinite(values)
        assert ok.mean() > 0.9
        # Source is linear in lon/lat only approximately on a UTM grid; 30 m bilinear is well within 1e-2.
        np.testing.assert_allclose(values[ok], linear_field(lon, lat)[ok], atol=1e-2)


def test_emit_glt_orthorectification(emit_nc):
    path, (lon, lat, base) = emit_nc
    da = orthorectify_emit(path, SIERRITA)
    assert da.rio.crs.to_epsg() == 4326
    assert da.sizes["band"] == 5
    np.testing.assert_allclose(da["wavelength"].values, 400.0 + 7.4 * np.arange(5))
    # Every orthorectified pixel equals the truth at its own lon/lat (+ band offset).
    xx, yy = np.meshgrid(da.x.values, da.y.values)
    for b in range(5):
        np.testing.assert_allclose(da.isel(band=b).values, linear_field(xx, yy) + b, atol=1e-4)
    # The window covers the padded bbox, not the whole scene.
    assert da.x.min() <= SIERRITA[0] and da.x.max() >= SIERRITA[2]
    assert da.sizes["x"] < lon.size and da.sizes["y"] < lat.size


def test_emit_glt_nodata_and_out_of_scene(emit_nc):
    path, _ = emit_nc
    res = 0.0006
    # 10 x 10 ortho pixels in the scene's upper-left corner, whose 5 x 5 GLT block is 0.
    da = orthorectify_emit(path, (-111.20, 31.96 - 10 * res, -111.20 + 10 * res, 31.96), pad=0.0)
    assert da.sizes["x"] == 10 and da.sizes["y"] == 10
    assert np.isnan(da.values[:, :5, :5]).all()
    assert np.isfinite(da.values[:, 5:, :]).all() and np.isfinite(da.values[:, :, 5:]).all()
    # A window made only of unmapped GLT cells, or outside the scene, is a coverage error.
    with pytest.raises(CoverageError):
        orthorectify_emit(path, (-111.20, 31.96 - 4 * res, -111.20 + 4 * res, 31.96), pad=0.0)
    with pytest.raises(CoverageError):
        orthorectify_emit(path, (-100.0, 40.0, -99.9, 40.1))


def test_emit_end_to_end_multiband(settings, emit_nc, monkeypatch):
    path, _ = emit_nc
    offline(monkeypatch)
    write_mock(settings, EMIT_ID, [make_granule("MOCK_EMIT_NC", cloud=0, rect=(-111.3, 31.7, -110.9, 32.1), asset=path)])
    out = settings.processed_dir / "emit.zarr"
    result = asyncio.run(download_and_crop(EMIT_ID, SIERRITA, out, settings=settings))
    assert result.status == "mock" and result.shape[0] == 5
    da = xr.open_zarr(out)["reflectance"]
    assert_clipped_to_aoi(da, SIERRITA)
    assert "wavelength" in da.coords


# --------------------------------------------------------------------------- #
# DISP-S1 coverage guard
# --------------------------------------------------------------------------- #


def test_disp_outside_conus_errors_without_backup(settings, monkeypatch, tmp_path):
    import earthaccess

    monkeypatch.setattr(earthaccess, "search_data", lambda **_: pytest.fail("CMR must not be queried"))
    with pytest.raises(CoverageError, match="outside CONUS"):
        asyncio.run(download_and_crop(DISP_ID, BRUMADINHO, tmp_path / "o.tif", settings=settings))


def test_disp_outside_conus_uses_cached_backup(settings, monkeypatch, tmp_path):
    import earthaccess

    monkeypatch.setattr(earthaccess, "search_data", lambda **_: pytest.fail("CMR must not be queried"))
    backup = settings.cache_dir / "brumadinho_disp_backup.csv"
    backup.write_text("date,displacement_mm\n")
    Path(f"{backup}.meta.json").write_text(json.dumps({"dataset_id": DISP_ID, "bbox": list(BRUMADINHO)}))
    # A backup registered for a different product must be ignored.
    other = settings.cache_dir / "other.csv"
    other.write_text("")
    Path(f"{other}.meta.json").write_text(json.dumps({"dataset_id": DSWX_ID, "bbox": list(BRUMADINHO)}))

    result = asyncio.run(download_and_crop(DISP_ID, BRUMADINHO, tmp_path / "o.tif", settings=settings))
    assert result.status == "cache_fallback" and result.origin == "historical_backup"
    assert result.output_path == str(backup)
    assert any("outside CONUS" in w for w in result.warnings)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    data_dir = tmp_path / "cli_data"
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setattr(ingestion, "_logged_in", False)
    config.get_settings.cache_clear()
    yield config.get_settings()
    config.get_settings.cache_clear()


def test_cli_ingest_writes_registry_outputs_and_log(cli_env, geographic_tif, dswx_tif, disp_nc, monkeypatch):
    offline(monkeypatch)
    wide = (-111.3, 31.7, -110.9, 32.1)
    monkeypatch.setitem(
        PRODUCTS, EMIT_ID, ingestion.ProductSpec(**{**PRODUCTS[EMIT_ID].__dict__, "asset_pattern": r"\.tif$"})
    )
    write_mock(cli_env, EMIT_ID, [make_granule("MOCK_EMIT", cloud=1, rect=wide, asset=geographic_tif)])
    write_mock(cli_env, DSWX_ID, [make_granule("MOCK_DSWX", cloud=1, rect=wide, asset=dswx_tif)])
    write_mock(cli_env, DISP_ID, [make_granule("MOCK_DISP", rect=wide, asset=disp_nc)])

    code = main.main(["ingest", "--facility", "Sierrita", "--bbox", "-111.12", "31.83", "-111.04", "31.90"])
    assert code == 0

    registry = pyogrio.read_dataframe(cli_env.data_path / "global_tailings.geojson")
    assert list(registry["id"]) == ["sierrita"]
    row = registry.iloc[0]
    assert row["ingest_status"] == "COMPLETE"
    np.testing.assert_allclose(registry.total_bounds, SIERRITA)
    runs = row["last_run_results"]
    runs = json.loads(runs) if isinstance(runs, str) else runs
    assert {r["dataset_id"] for r in runs} == {EMIT_ID, DISP_ID, DSWX_ID}
    assert all(r["status"] == "mock" for r in runs)

    for slug in ("emit_l2a_rfl", "opera_disp_s1", "opera_dswx_hls"):
        assert (cli_env.processed_dir / "sierrita" / f"sierrita_{slug}.tif").exists()
    logs = list(cli_env.log_dir.glob("ingest_sierrita_*.log"))
    assert len(logs) == 1 and "Earthdata unreachable" in logs[0].read_text()

    # Re-running upserts rather than duplicating; another facility is appended.
    assert main.main(["ingest", "--facility", "Sierrita", "--bbox", "-111.12", "31.83", "-111.04", "31.90"]) == 0
    assert main.main(["ingest", "--facility", "Mission", "--datasets", DSWX_ID]) == 0
    registry = pyogrio.read_dataframe(cli_env.data_path / "global_tailings.geojson")
    assert sorted(registry["id"]) == ["mission", "sierrita"]


def test_cli_reports_errors_with_nonzero_exit(cli_env, monkeypatch):
    offline(monkeypatch)
    code = main.main(["ingest", "--facility", "Brumadinho", "--datasets", DISP_ID])
    assert code == 1  # DISP-S1 outside CONUS and no cached backup
    registry = pyogrio.read_dataframe(cli_env.data_path / "global_tailings.geojson")
    assert registry.iloc[0]["ingest_status"] == "FAILED"


@pytest.mark.parametrize(
    "argv",
    [
        ["ingest", "--facility", "Sierrita", "--bbox", "-111.04", "31.83", "-111.12", "31.90"],
        ["ingest", "--facility", "Sierrita", "--bbox", "-111.12", "31.83", "-111.04"],
        ["ingest", "--facility", "Sierrita", "--bbox", "a", "31.83", "-111.04", "31.90"],
        ["ingest", "--facility", "Nowhere"],
        ["ingest", "--facility", "../etc", "--bbox", "-111.12", "31.83", "-111.04", "31.90"],
        ["ingest", "--facility", "Sierrita", "--datasets", "EMITL2ARFL"],
    ],
)
def test_cli_rejects_invalid_arguments(cli_env, argv):
    with pytest.raises(SystemExit) as exc:
        main.main(argv)
    assert exc.value.code == 2


# --------------------------------------------------------------------------- #
# Live Earthdata (opt-in)
# --------------------------------------------------------------------------- #


@pytest.mark.network
@pytest.mark.skipif(os.environ.get("LITHOCREEP_LIVE_TESTS") != "1", reason="set LITHOCREEP_LIVE_TESTS=1 to query CMR")
@pytest.mark.parametrize("dataset_id", [EMIT_ID, DISP_ID, DSWX_ID])
def test_live_cmr_search_finds_sierrita_granules(dataset_id):
    spec = PRODUCTS[dataset_id]
    granules = ingestion.search_granules(spec, SIERRITA, ingestion.DEFAULT_TEMPORAL, count=50)
    assert granules, f"CMR returned no {dataset_id} granules over Sierrita"
    best = select_best_granule(granules, SIERRITA, optical=spec.optical)
    assert best is not None
    assert ingestion.select_asset_links(best, spec)
