# IMPLEMENTATION_STATUS.md

Tracks progress against the phases in `IMPLEMENTATION_PLAYBOOK.md`.

| Phase | Scope | Status |
|-------|-------|--------|
| 1 | Environment, configuration, CLI, Earthdata download/crop pipeline | **Done**: tests pass; the live Earthdata path has not been run against NASA (see below) |
| 2 | Spectroscopy / NNLS unmixing | **Done**: tests pass. 245 active channels, not 242 (see below); not yet run on a real EMIT or EMITL2BMIN granule |
| 3 | Kinematics / InSAR trend modelling | Not started |
| 4 | Hydrology / DSWx setback and flow routing | Not started |
| 5 | Co-anomaly coupling and regime classifier | Not started |
| 6 | API and database | Not started |
| 7 | Audit copilot | Not started |
| 8 | Next.js / deck.gl map | Not started |
| 9 | Inspector panels | Not started |
| 10 | Scenarios, backtest UI, CI | Not started |

---

## Phase 1: Environment, configuration, CLI and download pipeline

### Delivered

| File | Purpose |
|------|---------|
| `backend/requirements.txt` | The 13 specified pins, followed by transitive pins that a working install needs (see *Environment*). |
| `backend/app/config.py` | pydantic-settings `Settings`: `EARTHDATA_USERNAME`, `EARTHDATA_PASSWORD` (default `None`) and `DATA_DIR` (default `"../data"`, resolved against `backend/`). Creates `raw/ cache/ processed/ mock/ logs/` and raises `PermissionError` if any of them cannot be written. Reads `backend/.env`, then `./.env`. |
| `backend/app/pipelines/ingestion.py` | `async download_and_crop(dataset_id, bbox, output_path)` plus the product catalogue, granule ranking, Earthdata search and download, offline and cache fallbacks, EMIT GLT orthorectification, and reprojection and clipping. |
| `backend/app/main.py` | argparse CLI `ingest --facility NAME [--bbox XMIN YMIN XMAX YMAX] [--datasets ...] [--format tif\|zarr]`. |
| `backend/tests/test_ingestion.py` | 35 offline tests plus 3 opt-in live CMR tests. |
| `backend/tests/test_environment.py` | Checks that the interpreter and every installed package match `requirements.txt` exactly. |
| `backend/pytest.ini`, `backend/.env.example`, `.gitignore` | Test configuration, credential template, ignores for generated data. |

### Pipeline behaviour

* **Product IDs** (exact): `EMITL2ARFL.001` → CMR `EMITL2ARFL` v`001`; `OPERA_L3_DISP-S1_V1` → `OPERA_L3_DISP-S1_V1` (no version filter); `OPERA_L3_DSWX-HLS_V1.0` → `OPERA_L3_DSWX-HLS_V1` v`1.0`.
* **Search:** bbox `[xmin, ymin, xmax, ymax]` (EPSG:4326, validated), temporal window 2022-01-01 to 2026-12-31.
* **Best overpass:** among granules whose footprint touches the bbox, ranked by full bbox coverage, then lowest cloud cover (optical products only), then largest coverage, then most recent.
* **Assets downloaded:** only the science file, to `DATA_DIR/raw/<short_name>/`:
  * EMIT: `EMIT_L2A_RFL_*.nc`
  * DSWx: `*_B01_WTR.tif`
  * DISP-S1: `*.nc`, variable `displacement`
* **EMIT:** the swath is orthorectified through its GLT. Only the window covering the bbox is resolved, so the full 285-band scene is never loaded.
* **Reproject and clip:**
  1. Coarse clip in the source CRS (bbox plus a 0.01° margin).
  2. Reproject to `EPSG:32612`: bilinear for reflectance and displacement, nearest for the categorical DSWx WTR layer.
  3. Exact clip to the bbox polygon in UTM.
* **Outputs:** DEFLATE-tiled GeoTIFF (`.tif`) or Blosc/zstd Zarr (`.zarr`), chosen by the `output_path` suffix. Each output also gets:
  * a `<stem>_aoi.geojson` AOI written with pyogrio;
  * a `<output>.meta.json` sidecar;
  * `origin` and `data_label` attributes.
* **DISP-S1 outside CONUS** (e.g. Brumadinho): CMR is never queried. `DATA_DIR/cache` is searched for a backup:
  * a `*.meta.json` sidecar with a matching `dataset_id` and an intersecting `bbox`, or
  * a raster whose bounds intersect the bbox.

  If a backup is found, the result is `status="cache_fallback"`, `origin="historical_backup"`. Otherwise `CoverageError` is raised.
* **Earthdata unreachable** (connection error or timeout during search or download): a warning is logged and granule records are read from `DATA_DIR/mock/<dataset_id>/*.json`. The file format is documented in the module docstring.
  * Outputs built from mock inputs are labelled `origin="mock"` / `"MOCK INPUT - not an authentic observation"`.
  * A mock record without a usable `local_asset` gives `status="metadata_only"`, and nothing is written.
* **Missing or invalid Earthdata credentials:** `EarthdataAuthError`. The pipeline does not silently fall back to mock data.

### CLI

```bash
python backend/app/main.py ingest --facility Sierrita --bbox -111.12 31.83 -111.04 31.90
```

* Validates the facility name (no path characters) and the bbox. `--bbox` is optional for the registered facilities Sierrita, Mission and Brumadinho.
* Runs the three products concurrently. A failure in one product does not stop the others.
* Writes outputs to `DATA_DIR/processed/<facility>/<facility>_<product>.tif`.
* Upserts the facility feature (bbox polygon, centre, `ingest_status`, `last_run_id`, `last_run_utc`, `last_run_results`) in `DATA_DIR/global_tailings.geojson` with pyogrio.
* Logs to `DATA_DIR/logs/ingest_<run_id>.log` and to stderr.
* Exit codes: `0` on success; `1` if any product errored; `2` for invalid arguments or an unwritable `DATA_DIR`.

### Verification

```
$ cd backend && pytest
59 passed, 3 skipped
```

* `test_ingestion.py` (35 passed) covers:
  * Settings: environment variables, defaults, path resolution, unwritable and non-directory `DATA_DIR`.
  * Exact product IDs.
  * Bbox validation.
  * The CONUS guard.
  * Granule ranking for optical and radar products.
  * The online path with `earthaccess` stubbed: query arguments, the 2022–2026 window, science-asset-only download, raw storage location.
  * Reprojected values against an analytic field (to within 0.2 px of GDAL's approximate transformer).
  * Clip bounds within one pixel of the UTM AOI.
  * DSWx categorical values preserved.
  * DISP-S1 NetCDF reading.
  * EMIT GLT orthorectification exactness and nodata handling.
  * GeoTIFF and Zarr outputs.
  * Offline mock fallback.
  * The DISP-S1 non-CONUS error and cache fallback.
  * CLI registry upsert, logs, exit codes and argument validation.
* `test_environment.py` (24 passed) confirms every pin is installed at exactly the pinned version on a supported Python.
* Skipped: 3 live CMR tests. Run them with `LITHOCREEP_LIVE_TESTS=1`.
* A fresh Python 3.11 virtualenv built from `requirements.txt` alone passes the full suite.

### Environment

* **Python 3.9–3.11 only.** `duckdb==0.9.2` has no CPython 3.12 wheel, and its source build fails.
* **Extra pins.** The 13 specified pins are kept unchanged. Installing them alone does not give a working environment, so these pins were added:

| Pin | Reason |
|-----|--------|
| `setuptools==69.0.3` | `earthaccess==0.8.2` imports `pkg_resources`, which current setuptools no longer provides, so `import earthaccess` fails. |
| `pandas==2.1.4`, `numpy==1.26.4` | Unpinned, the resolver picks pandas 3.x, and `pyogrio==0.7.2` `read_dataframe` then crashes (`UnboundLocalError`). |
| `h5netcdf==1.3.0`, `h5py==3.10.0` | Needed to read EMIT and DISP-S1 NetCDF granules. |
| `zarr==2.16.1`, `numcodecs==0.12.1` | Needed for the Zarr output format (newer numcodecs breaks zarr 2.16). |
| `rasterio==1.4.4`, `affine==2.4.0` | Pin the GDAL stack. affine 3.x deprecates operators that rasterio and rioxarray use. |

### Known gaps / follow-ups

* **The live Earthdata path has not been run against NASA.** The development sandbox's network policy blocks `cmr.earthdata.nasa.gov`. The CLI there correctly fell back to mock metadata. Before relying on it, run `LITHOCREEP_LIVE_TESTS=1 pytest -m network` and a real `ingest` on a machine with Earthdata access. Points to confirm:
  * the DISP-S1 collection's version string and its NetCDF layout (`displacement` variable, CRS in `spatial_ref`);
  * the asset filename patterns.
* **No mock rasters or historical backups are bundled.** `data/mock/` and `data/cache/` start empty, so an offline `ingest` reports `no_granules` until fixtures are added. `data/brumadinho_historic.csv` (Carla et al., 2019) will need a `.meta.json` sidecar (`dataset_id: OPERA_L3_DISP-S1_V1`, `bbox`) in `data/cache/` to be picked up as the DISP-S1 backup.
* **`EPSG:32612` is the default output CRS.** For bboxes outside UTM zone 12N (e.g. Brumadinho), a warning is logged. Automatic per-site UTM selection is a candidate for Phase 5 grid alignment.
* **DISP-S1 selection.** One granule (the most recent full-coverage one) is selected. Phase 3 will need the full displacement time series, not a single overpass.
* **`global_tailings.geojson`.** The CLI records only the bbox, name and country of the facilities it ingests. Phase 6 still needs to add mine type, embankment type, crest lines, and the Bingham Canyon entry.

---

## Phase 2: Spectroscopy pipeline and NNLS unmixing

### Delivered

| File | Purpose |
|------|---------|
| `backend/app/pipelines/unmixing.py` | The spectroscopy engine: EMIT band mapping, library loading, endmember matrix, constrained NNLS, NDVI / NDWI_SWIR / SAI, confidence and observability flags, Zarr output, RMSE log, and EMITL2BMIN spatial R². |
| `backend/app/main.py` | New CLI command `unmix --facility NAME [--input] [--output] [--l2bmin] [--library]`. |
| `backend/app/pipelines/ingestion.py` | `orthorectify_emit(..., variable=)` now also orthorectifies 2-D EMIT layers (the L2B `group_*` maps). The L2A behaviour is unchanged. |
| `backend/scripts/build_reference_data.py` | Rebuilds the bundled reference files from their public sources. |
| `data/reference/` | `emit_wavelengths_285.csv`, `usgs_splib06a_extract.csv`, `emit_l2b_mineral_grouping_matrix_20230503.csv`, and a provenance `README.md`. |
| `backend/tests/test_unmixing.py` | 28 tests. |

### Engine behaviour

* **Band mapping.** The 285 EMIT channels (381.0–2492.9 nm) come from EMIT's L1B calibration (`EMIT_Wavelengths_20220817`, detector rows 20–304). A granule's own `wavelength` coordinate takes precedence when present. Channels in [1340, 1460] nm and [1790, 1970] nm are dropped.
* **Endmembers.** Real USGS spectra are used for Jarosite, Goethite, Ferrihydrite, Montmorillonite, Gypsum, Quartz-Feldspar and Water-Shade (sample choices are in `data/reference/README.md`). They are linearly interpolated onto the active channels to give **M (245 × 7)**.
  * The spectra stay in reflectance units, clipped to [0, 1]. They are not rescaled, so the fractions keep their meaning as areal abundances.
  * Conditioning on the EMIT grid: condition number 341. With 0.002 reflectance noise, the mean fraction error is 0.001–0.004 for the six minerals and about 0.02 for the near-zero Water-Shade spectrum.
* **NNLS** (`scipy.optimize.nnls`) with `f ≥ 0` and `Σf ≤ 1`:
  1. Solve plain NNLS first. If the result already satisfies `Σf ≤ 1`, it is optimal (the problem is convex).
  2. Otherwise re-solve with `Σf = 1` enforced by a heavily weighted augmentation row, as in FCLS, then renormalise.
  3. Edge cases: non-finite channels are skipped. With fewer than 14 usable channels the pixel is treated as no-data.
  4. Per-pixel `rmse` is computed over the channels used.
* **Indices.** The nearest channels to 640, 860 and 1240 nm (≤ 15 nm away, no interpolation) are 641.3, 857.6 and 1238.1 nm. They give NDVI and NDWI_SWIR, and `SAI = 0.40 f_Jarosite + 0.30 f_Montmorillonite + 0.20 f_Goethite + 0.10 NDWI_SWIR`. Smectite is represented by montmorillonite.
* **Masking and confidence.**
  * `observability` flags: 0 observable, 1 `unobservable-vegetated` (NDVI > 0.35), 2 no-data, 3 poor-fit (RMSE > 0.05).
  * `confidence = max(0, 1 − rmse/0.05)`; it is 0 for vegetated and no-data pixels.
  * SAI is NaN for vegetated and no-data pixels.
* **Scaling.** If a cube's maximum reflectance exceeds 1.5, it is treated as integer-scaled and divided by 10 000.
* **Outputs.**
  * `DATA_DIR/cache/<Facility>_spectral.zarr` (Blosc/zstd) on the **input grid**, i.e. the Phase 1 UTM 32612 grid. Variables: `abundance(endmember,y,x)`, `fraction_sum`, `rmse`, `ndvi`, `ndwi_swir`, `sai`, `confidence`, `observability`. Open it with `xr.open_zarr(path, decode_coords="all")` to recover the CRS.
  * Input provenance (`origin` / `data_label`) is passed through, so mock inputs stay labelled.
  * `DATA_DIR/logs/<Facility>_spectral_rmse_<run>.json` holds RMSE statistics, flag counts, mean abundances and the L2BMIN validation results.
* **EMITL2BMIN validation.**
  * The L2B granule is taken from `--l2bmin`, or found automatically as `DATA_DIR/raw/EMITL2BMIN/EMIT_L2B_MIN_*<same scene id>*.nc`.
  * Its `group_{1,2}_band_depth` and `group_{1,2}_mineral_id` layers are orthorectified through the GLT and nearest-resampled onto our grid.
  * Mineral IDs are mapped using EMIT's grouping matrix: the EMIT-10 columns for Goethite, Gypsum and Montmorillonite, and library filenames for Jarosite and Ferrihydrite.
  * The spatial R² (squared Pearson r) is computed over observable pixels. R² is reported as `null` when it is undefined (for example, a mineral that is absent everywhere). Quartz-Feldspar and Water-Shade have no L2B counterpart.

### Verification

```
$ cd backend && pytest
87 passed, 3 skipped
```

`test_unmixing.py` (28 passed) covers:

* **Band mapping:** the 285-band grid; vapour windows excluded exactly; 245 active channels; nearest-band matching and its 15 nm guard.
* **Library and M:** library provenance; splib07 priority; missing-endmember error; M aligned to the active channels.
* **Unmixing accuracy:**
  * Each **pure reference spectrum unmixes to ≥ 95 %** of its own endmember. In practice it is 1.000, with RMSE below 1e-6.
  * Noise-free mixtures are recovered exactly.
  * 200 noisy mixtures (σ = 0.002) have RMSE < 0.02, `f ≥ 0`, `Σf ≤ 1`, and mineral fraction error < 0.03.
  * The sum constraint holds for over-bright pixels.
  * Edge cases: all-NaN, all-zero, partial-NaN and too-few-channel pixels.
* **Cube processing:** abundance maps recovered on the input grid and CRS; vegetated and no-data masking with zero confidence; SAI matches the formula; integer-scaled input rescaled; poor-fit flag; fallback to the bundled wavelength grid.
* **Outputs and validation:**
  * Zarr and RMSE-log contents.
  * The L2BMIN comparison against a synthetic L2B granule: goethite and jarosite R² > 0.95, gypsum R² `null`.
  * The L2B granule finder.
  * The `unmix` CLI.
  * The full `ingest` → `unmix` chain: an EMIT NetCDF goes through GLT orthorectification, UTM reprojection and Zarr output, then unmixing.

### Deviations and known gaps

* **245 active channels, not 242.** On EMIT's real wavelength grid, the specified windows (1340–1460 nm and 1790–1970 nm) remove 40 of the 285 channels. This holds for every EMIT calibration from 2022 to 2026. EMIT's own `good_wavelengths` mask (1325–1435 nm, 1770–1962 nm) keeps 244. The code follows the specified windows, so **M is 245 × 7**, and nothing hard-codes 242.
  * Dropping the three channels below 400 nm as well would give exactly 242. That is a one-line change to `WATER_VAPOR_WINDOWS_NM` or the active mask, if that is what was intended.
* **USGS splib06a, not splib07.** The USGS servers that host splib07 are blocked from the build environment. The bundled extract is the public-domain splib06a release, taken from EMIT's Tetracorder repository and labelled as splib06a.
  * A `usgs_splib07_extract.csv` with the same columns overrides it automatically.
  * Quartz-Feldspar is a derived 50/50 quartz/orthoclase mix, because the library has no single quartz–feldspar spectrum.
* **Jarosite and EMIT-10.** Jarosite is not one of the EMIT-10 classes. It is validated through the L2B per-pixel mineral IDs, which do include Tetracorder's jarosite entries.
* **Band depth versus abundance.** L2B band depths are not abundances. The R² therefore measures spatial agreement only, not calibrated fractions.
* **Not run on real EMIT data.** Earthdata is blocked in this sandbox, so neither Sierrita EMIT granules nor EMITL2BMIN files could be fetched. No `Sierrita_spectral.zarr` or R² figures exist yet. Every result above is from synthetic cubes built from the real library spectra on the real EMIT grid.
  * `ingest` does not yet download EMITL2BMIN. Download the matching granule (same scene id) into `DATA_DIR/raw/EMITL2BMIN/`, or pass it with `--l2bmin`.
* **No masks for cloud, cirrus or other conditions.** EMIT L2A `mask` files are not applied yet. Only vegetation (NDVI), no-data and poor-fit pixels are masked.

