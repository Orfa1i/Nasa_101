"""Verification checks for the hydrology engine (setback distances, alerts, terrain and D-infinity routing).

Geometry tests use small grids where the answer is known exactly; the CLI test
runs the whole pipeline on synthetic DEM / DSWx-HLS / crest inputs. The real
GLO-30 download is opt-in (LITHOCREEP_LIVE_TESTS=1).
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

import numpy as np
import pytest
import xarray as xr
from affine import Affine
from pyproj import Transformer
from shapely.geometry import LineString, box

from app import config, main
from app.config import Settings
from app.pipelines import hydrology as h
from app.pipelines import ingestion

SIERRITA = (-111.12, 31.83, -111.04, 31.90)
TO_UTM = Transformer.from_crs("EPSG:4326", "EPSG:32612", always_xy=True)
TO_LONLAT = Transformer.from_crs("EPSG:32612", "EPSG:4326", always_xy=True)
RES = 30.0
T = Affine(RES, 0, 0, 0, -RES, 600)  # 20 x 20 grid, x 0..600, y 0..600


def wtr_grid(water=(), cloud=(), partial=()):
    g = np.zeros((20, 20), dtype=np.uint8)
    for rows, cols in water:
        g[rows, cols] = h.WTR_OPEN_WATER
    for rows, cols in partial:
        g[rows, cols] = h.WTR_PARTIAL_WATER
    for rows, cols in cloud:
        g[rows, cols] = h.WTR_CLOUD
    return g


ALL = np.ones((20, 20), dtype=bool)
CREST_X300 = LineString([(300, 0), (300, 600)])


# --------------------------------------------------------------------------- #
# Setback distance and alerts
# --------------------------------------------------------------------------- #


def test_setback_distance_is_exact_edge_to_line_distance():
    g = wtr_grid(water=[(slice(5, 11), slice(2, 6))])  # x 60..180
    e = h.setback_distance(g, T, CREST_X300, ALL)
    assert e.status == h.SETBACK_VALID
    assert e.distance_m == pytest.approx(120.0)  # 300 - 180, from the pixel edge
    assert e.water_area_m2 == pytest.approx(6 * 4 * 900)


def test_partial_surface_water_counts_and_diagonal_distance():
    g = wtr_grid(partial=[(slice(0, 2), slice(0, 2))])  # x 0..60, y 540..600
    crest = LineString([(150, 450), (300, 300)])
    e = h.setback_distance(g, T, crest, ALL)
    assert e.status == h.SETBACK_VALID
    # Nearest water corner (60, 540) to the segment from (150, 450) to (300, 300): the corner is
    # on the segment's normal through its start point, so the distance is 90 * sqrt(2).
    assert e.distance_m == pytest.approx(90 * math.sqrt(2))


def test_crest_through_water_is_zero():
    g = wtr_grid(water=[(slice(5, 11), slice(2, 12))])  # reaches x 360 > 300
    assert h.setback_distance(g, T, CREST_X300, ALL).distance_m == 0.0


def test_cloud_nearer_than_water_marks_epoch_obscured():
    g = wtr_grid(water=[(slice(5, 11), slice(2, 6))], cloud=[(slice(5, 7), slice(8, 9))])  # cloud x 240..270
    e = h.setback_distance(g, T, CREST_X300, ALL)
    assert e.status == h.SETBACK_OBSCURED
    assert e.unknown_distance_m == pytest.approx(30.0) and e.distance_m == pytest.approx(120.0)


def test_vegetation_counts_as_unknown():
    g = wtr_grid(water=[(slice(5, 11), slice(2, 6))])
    veg = np.zeros((20, 20), dtype=bool)
    veg[0:3, 9] = True  # x 270..300
    assert h.setback_distance(g, T, CREST_X300, ALL, veg).status == h.SETBACK_OBSCURED


def test_facility_boundary_no_water_and_no_data():
    g = wtr_grid(water=[(slice(5, 11), slice(2, 6))])
    east_half = np.zeros((20, 20), dtype=bool)
    east_half[:, 10:] = True
    e = h.setback_distance(g, T, CREST_X300, east_half)
    assert e.status == h.SETBACK_NO_WATER and np.isnan(e.distance_m)
    cloudy = wtr_grid(water=[(slice(5, 11), slice(2, 6))], cloud=[(slice(0, 20), slice(8, 20))])
    assert h.setback_distance(cloudy, T, CREST_X300, ALL).status == h.SETBACK_NO_DATA


def test_setback_alerts_shrink_and_proximity():
    d = np.array([120.0, 110.0, 85.0, 5.0, 12.0, np.nan])
    st = np.array([h.SETBACK_VALID, h.SETBACK_VALID, h.SETBACK_VALID, h.SETBACK_OBSCURED, h.SETBACK_VALID, h.SETBACK_NO_WATER])
    a = h.setback_alerts(d, st)
    np.testing.assert_allclose(a["delta_baseline_m"], [0, -10, -35, np.nan, -108, np.nan])
    np.testing.assert_allclose(a["delta_previous_m"], [np.nan, -10, -25, np.nan, -73, np.nan])
    assert list(a["alert_flags"]) == [
        0,
        0,
        h.ALERT_SHRINK_BASELINE,  # -35 m vs baseline
        0,  # obscured epochs never alert
        h.ALERT_SHRINK_BASELINE | h.ALERT_SHRINK_PREVIOUS | h.ALERT_PROXIMITY,
        0,
    ]


def test_exactly_30m_shrink_does_not_alert():
    a = h.setback_alerts(np.array([90.0, 60.0]), np.array([h.SETBACK_VALID, h.SETBACK_VALID]))
    assert list(a["alert_flags"]) == [0, 0]  # threshold is strictly "< -30 m"


# --------------------------------------------------------------------------- #
# Slope / aspect
# --------------------------------------------------------------------------- #


def plane(gx, gy_north, n=12):
    """z = gx * x_east + gy_north * y_north on an n x n grid (row 0 is north)."""
    rows, cols = np.indices((n, n))
    return 1000.0 + gx * cols * RES + gy_north * (-rows * RES)


@pytest.mark.parametrize(
    "gx,gy,aspect",
    [(-0.1, 0.0, 90.0), (0.1, 0.0, 270.0), (0.0, 0.2, 180.0), (0.0, -0.2, 0.0), (0.1, 0.1, 225.0), (-0.1, 0.1, 135.0)],
)
def test_horn_slope_and_aspect_on_planes(gx, gy, aspect):
    slope, asp = h.slope_aspect(plane(gx, gy), RES)
    inner = (slice(1, -1), slice(1, -1))
    np.testing.assert_allclose(slope[inner], math.degrees(math.atan(math.hypot(gx, gy))), atol=1e-9)
    got = asp[inner]
    np.testing.assert_allclose(np.minimum(np.abs(got - aspect), 360 - np.abs(got - aspect)), 0.0, atol=1e-9)
    assert np.isnan(slope[0]).all() and np.isnan(asp[:, -1]).all()  # edges


def test_flat_ground_has_no_aspect():
    slope, asp = h.slope_aspect(np.full((5, 5), 7.0), RES)
    assert slope[2, 2] == 0 and np.isnan(asp[2, 2])


# --------------------------------------------------------------------------- #
# D-infinity routing
# --------------------------------------------------------------------------- #


def test_dinf_direction_and_proportions_on_tilted_plane():
    alpha = math.radians(30.0)  # downslope 30 deg counter-clockwise from east
    z = plane(-math.cos(alpha) * 0.1, -math.sin(alpha) * 0.1)
    flow = h.dinf_flow_directions(z, RES)
    inner = (slice(1, -1), slice(1, -1))
    np.testing.assert_allclose(flow.angle[inner], alpha, atol=1e-9)
    np.testing.assert_allclose(flow.proportion1[inner], 1 - 30 / 45, atol=1e-9)  # 1/3 east, 2/3 north-east
    r, c = 5, 5
    nx = z.shape[1]
    assert flow.receiver1[r, c] == r * nx + c + 1 and flow.receiver2[r, c] == (r - 1) * nx + c + 1
    # Horn aspect and the D-infinity azimuth agree on a plane (60 deg clockwise from north).
    _, asp = h.slope_aspect(z, RES)
    np.testing.assert_allclose(asp[inner], 60.0, atol=1e-9)


def test_accumulation_conserves_mass():
    rng = np.random.default_rng(0)
    z = h.fill_depressions(rng.normal(0, 5, (40, 40)) + np.add.outer(np.arange(40), np.arange(40)) * 0.5)
    flow = h.dinf_flow_directions(z, RES)
    acc = h.dinf_accumulation(z, flow)
    sinks = (flow.receiver1 < 0) & (flow.receiver2 < 0)
    assert acc[sinks].sum() == pytest.approx(z.size)  # every cell's unit ends in exactly one outlet
    interior_sinks = sinks[1:-1, 1:-1]
    assert not interior_sinks.any()  # after filling, everything drains to the edge


def test_valley_routes_to_its_axis_and_outlet():
    rows, cols = np.indices((30, 21))
    z = 100.0 + np.abs(cols - 10) * 2.0 + (29 - rows) * 0.5  # V valley along column 10, draining south
    ds_flow = h.dinf_flow_directions(z, RES)
    acc = h.dinf_accumulation(z, ds_flow)
    assert np.argmax(acc[-2]) == 10  # the largest accumulation is on the valley axis
    assert acc[-1, 10] > 0.5 * z.size  # most of the catchment exits at the valley outlet
    path = h.trace_flow_path(ds_flow, (2, 2))
    assert path[-1][0] == 29  # reaches the southern edge
    assert any(c == 10 for _, c in path)  # via the valley axis


def test_priority_flood_fills_pits_and_leaves_slopes():
    rows, cols = np.indices((15, 15))
    z = 50.0 + np.hypot(rows - 7, cols - 7)  # cone low at the centre: a closed depression
    z[7, 7] = 40.0
    filled = h.fill_depressions(z)
    assert filled[7, 7] > 50.0  # pit raised to the spill level
    assert np.all(filled >= z)
    assert filled[0, 7] == z[0, 7]  # edge outlets untouched
    flow = h.dinf_flow_directions(filled, RES)
    interior = (slice(1, -1), slice(1, -1))
    assert np.all((flow.receiver1[interior] >= 0) | (flow.receiver2[interior] >= 0))


# --------------------------------------------------------------------------- #
# Inputs: GLO-30 and DSWx-HLS
# --------------------------------------------------------------------------- #


def test_glo30_tile_names():
    assert h.glo30_tile_names(SIERRITA) == ["Copernicus_DSM_COG_10_N31_00_W112_00_DEM"]
    assert h.glo30_tile_names((-44.15, -20.14, -44.10, -20.10)) == ["Copernicus_DSM_COG_10_S21_00_W045_00_DEM"]
    assert len(h.glo30_tile_names((-111.5, 31.5, -110.5, 32.5))) == 4
    assert h.glo30_tile_names((10.2, 45.0, 10.4, 45.5)) == ["Copernicus_DSM_COG_10_N45_00_E010_00_DEM"]


def test_download_glo30_streams_skips_404_and_reuses(tmp_path, monkeypatch):
    import requests

    calls = []

    class Resp:
        def __init__(self, code):
            self.status_code = code

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            yield b"COG"

    def fake_get(url, timeout, stream):
        calls.append(url)
        return Resp(404 if "W111" in url else 200)

    monkeypatch.setattr(requests, "get", fake_get)
    paths = h.download_glo30((-112.5, 31.2, -111.5, 31.8), tmp_path)
    assert [p.name for p in paths] == ["Copernicus_DSM_COG_10_N31_00_W113_00_DEM.tif", "Copernicus_DSM_COG_10_N31_00_W112_00_DEM.tif"]
    assert all(u.startswith("https://copernicus-dem-30m.s3.amazonaws.com/") for u in calls)
    calls.clear()
    h.download_glo30((-112.5, 31.2, -112.4, 31.8), tmp_path)
    assert calls == []  # already on disk


def dswx_name(tile, sensed, sensor="S2A"):
    return f"OPERA_L3_DSWx-HLS_T{tile}_{sensed}_20250101T000000Z_{sensor}_30_v1.0_B01_WTR.tif"


def utm_template(nx=40, ny=40):
    x0, y0 = TO_UTM.transform(-111.11, 31.89)
    x0, y0 = round(x0 / RES) * RES, round(y0 / RES) * RES
    da = xr.DataArray(np.zeros((ny, nx), dtype=np.float32), dims=("y", "x"),
                      coords={"x": x0 + (np.arange(nx) + 0.5) * RES, "y": y0 - (np.arange(ny) + 0.5) * RES})
    return da.rio.write_crs("EPSG:32612")


def write_wtr(path, template, values):
    da = template.copy(data=values.astype(np.uint8)).rio.write_nodata(h.WTR_FILL)
    da.rio.to_raster(path)
    return path


def test_dswx_filename_and_same_day_mosaic(tmp_path):
    tile, sensed, sensor = h.parse_dswx_filename(Path(dswx_name("12RVV", "20240105T180000Z", "L30")))
    assert (tile, sensed.isoformat(), sensor) == ("12RVV", "2024-01-05T18:00:00", "L30")
    with pytest.raises(h.HydrologyError):
        h.parse_dswx_filename(Path("OPERA_L3_DSWx-HLS_T12RVV_B02_BWTR.tif"))

    tpl = utm_template()
    west = np.full(tpl.shape, h.WTR_FILL)
    west[:, :20] = h.WTR_OPEN_WATER
    east = np.full(tpl.shape, h.WTR_FILL)
    east[:, 20:] = h.WTR_CLOUD
    later = np.zeros(tpl.shape)
    files = [
        write_wtr(tmp_path / dswx_name("12RVV", "20240105T180000Z"), tpl, west),
        write_wtr(tmp_path / dswx_name("12RWV", "20240105T180010Z"), tpl, east),
        write_wtr(tmp_path / dswx_name("12RVV", "20240110T180000Z", "L30"), tpl, later),
    ]
    series = h.load_dswx_series(files, tpl)
    assert series.sizes["time"] == 2
    first = series.isel(time=0).values
    assert (first[:, :20] == h.WTR_OPEN_WATER).all() and (first[:, 20:] == h.WTR_CLOUD).all()
    assert (series.isel(time=1).values == 0).all()


def test_vegetation_masks_terrain_outputs(tmp_path):
    tpl = utm_template(12, 12)
    dem = tpl.copy(data=plane(-0.1, 0.0)).rio.write_nodata(np.nan, encoded=False)
    ndvi = np.zeros((12, 12))
    ndvi[3:6, 3:6] = 0.8
    ndvi_path = tmp_path / "ndvi.tif"
    tpl.copy(data=ndvi.astype(np.float32)).rio.to_raster(ndvi_path)
    veg, note = h.vegetation_mask(ndvi_path, dem)
    assert veg[4, 4] and not veg[8, 8] and "0.35" in note
    ds = h.terrain_dataset(dem, vegetated=veg)
    assert np.isnan(ds["slope_deg"].values[4, 4]) and np.isnan(ds["aspect_deg"].values[4, 4])
    assert ds["terrain_observability"].values[4, 4] == 1
    assert ds["aspect_deg"].values[8, 8] == pytest.approx(90.0)
    assert h.vegetation_mask(None, dem) == (None, "unavailable (no NDVI source)")


# --------------------------------------------------------------------------- #
# End-to-end CLI
# --------------------------------------------------------------------------- #


@pytest.fixture
def cli_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "cli_data"))
    monkeypatch.setattr(ingestion, "_logged_in", False)
    config.get_settings.cache_clear()
    yield config.get_settings()
    config.get_settings.cache_clear()


def test_cli_hydrology_end_to_end(cli_settings, tmp_path, monkeypatch):
    import earthaccess
    import requests

    monkeypatch.setattr(earthaccess, "search_data", lambda **_: (_ for _ in ()).throw(requests.exceptions.ConnectionError("offline")))
    monkeypatch.setattr(requests, "get", lambda *a, **k: (_ for _ in ()).throw(requests.exceptions.ConnectionError("offline")))

    # Synthetic UTM DEM over the bbox: a basin draining south-west.
    gx0, gy0, gx1, gy1 = h.utm_grid_bounds(SIERRITA, "EPSG:32612")
    nx, ny = int((gx1 - gx0) / RES) + 20, int((gy1 - gy0) / RES) + 20
    x = gx0 - 300 + (np.arange(nx) + 0.5) * RES
    y = gy1 + 300 - (np.arange(ny) + 0.5) * RES
    X, Y = np.meshgrid(x, y)
    z = 900 + 0.02 * (X - X.min()) + 0.03 * (Y - Y.min())
    dem = xr.DataArray(z.astype(np.float32), dims=("y", "x"), coords={"x": x, "y": y}).rio.write_crs("EPSG:32612")
    dem_path = tmp_path / "dem.tif"
    dem.rio.to_raster(dem_path)

    # Pond in the middle of the facility whose east shore advances towards a crest line.
    cx, cy = (gx0 + gx1) / 2, (gy0 + gy1) / 2
    crest_x = cx + 600
    tpl = dem.copy(data=np.zeros_like(z, dtype=np.float32))
    dswx = tmp_path / "dswx"
    dswx.mkdir()
    for k, (date, shore) in enumerate([("20240105", cx + 300), ("20240205", cx + 290), ("20240305", cx + 540), ("20240405", cx + 595)]):
        wtr = np.where((np.abs(Y - cy) < 300) & (X > cx - 300) & (X < shore), h.WTR_OPEN_WATER, h.WTR_NOT_WATER)
        write_wtr(dswx / dswx_name("12RVV", f"{date}T180000Z"), tpl, wtr)
    crest = tmp_path / "crest.geojson"
    lon0, lat0 = TO_LONLAT.transform(crest_x, cy - 1500)
    lon1, lat1 = TO_LONLAT.transform(crest_x, cy + 1500)
    crest.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {}, "geometry": {"type": "LineString", "coordinates": [[lon0, lat0], [lon1, lat1]]}}]}))

    argv = ["hydrology", "--facility", "Sierrita", "--download", "--dem", str(dem_path), "--dswx-dir", str(dswx), "--crest", str(crest)]
    assert main.main(argv) == 0

    ds = xr.open_zarr(cli_settings.cache_dir / "Sierrita_hydrology.zarr", decode_coords="all")
    assert ds.rio.crs.to_epsg() == 32612
    for name in ("slope_deg", "aspect_deg", "flow_direction_deg", "flow_accumulation_cells", "accumulation_vector_u",
                 "stream_network", "water_class", "d_crest_m", "setback_alert_flags", "delta_d_crest_baseline_m"):
        assert name in ds
    assert ds.attrs["vegetation_filter"].startswith("unavailable")
    d = ds["d_crest_m"].values
    # Pixel-edge distances: the shore snaps to the 30 m grid, so allow one pixel.
    np.testing.assert_allclose(d, [300, 310, 60, 5], atol=RES)
    flags = ds["setback_alert_flags"].values
    assert flags[0] == 0 and flags[1] == 0
    assert flags[2] & h.ALERT_SHRINK_BASELINE and flags[2] & h.ALERT_SHRINK_PREVIOUS
    assert flags[3] & h.ALERT_PROXIMITY
    aspect = ds["aspect_deg"].values
    assert np.nanmedian(aspect) == pytest.approx(math.degrees(math.atan2(-0.02, -0.03)) % 360, abs=0.5)

    summary = json.loads(next(cli_settings.log_dir.glob("Sierrita_hydrology_*.json")).read_text())
    kinds = {a["alert"] for a in summary["alerts"]}
    assert {"SETBACK_SHRINK_VS_BASELINE", "SETBACK_SHRINK_VS_PREVIOUS", "CREST_PROXIMITY"} <= kinds
    log = next(cli_settings.log_dir.glob("hydrology_*.log")).read_text()
    assert "GLO-30 download failed" in log and "Earthdata unreachable" in log


def test_cli_hydrology_without_dem_fails(cli_settings):
    assert main.main(["hydrology", "--facility", "Sierrita"]) == 1


@pytest.mark.network
@pytest.mark.skipif(os.environ.get("LITHOCREEP_LIVE_TESTS") != "1", reason="set LITHOCREEP_LIVE_TESTS=1 to download GLO-30")
def test_live_glo30_sierrita_terrain(tmp_path):
    paths = h.download_glo30(SIERRITA, tmp_path)
    dem = h.load_dem(paths, SIERRITA)
    assert 500 < float(np.nanmin(dem)) < float(np.nanmax(dem)) < 2000
    ds = h.terrain_dataset(dem)
    assert ds["stream_network"].values.any()
