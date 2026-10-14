# IMPLEMENTATION_STATUS.md

Tracks progress against the phases in `IMPLEMENTATION_PLAYBOOK.md`.

| Phase | Scope | Status |
|-------|-------|--------|
| 1 | Environment, configuration, CLI, Earthdata download/crop pipeline | **Done**: tests pass; the live Earthdata path has not been run against NASA (see below) |
| 2 | Spectroscopy / NNLS unmixing | **Done**: tests pass. 245 active channels, not 242 (see below); not yet run on a real EMIT or EMITL2BMIN granule |
| 3 | Kinematics / InSAR trend modelling | **Done**: tests pass. Timelines are biased late (see below); no real DISP-S1 stack or Brumadinho CSV has been run yet |
| 4 | Hydrology / DSWx setback and flow routing | **Done**: tests pass and terrain/routing ran on real GLO-30 for Sierrita. The setback series has not run on real DSWx, and no crest line exists yet |
| 5 | Co-anomaly coupling and regime classifier | **Done**: tests pass, including a full Phase 2→5 chain. No real classification exists yet (no real EMIT, DISP-S1 or DSWx data) |
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
| `scikit-learn==1.3.2`, `joblib==1.3.2`, `threadpoolctl==3.2.0` | Added in Phase 3 for the GP regressor (era-consistent with scipy 1.11 / numpy 1.26). |

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

---

## Phase 3: Kinematic engine and InSAR trend modelling

### Delivered

| File | Purpose |
|------|---------|
| `backend/app/pipelines/kinematics.py` | Everything for the kinematics step:<br>• DISP-S1 stack loading and chaining, plus the historical CSV loader<br>• coherence gating<br>• GP smoothing with analytic velocity and acceleration<br>• Fukuzono inverse velocity and the Bayesian trend regression<br>• status classification and Zarr output |
| `backend/app/pipelines/ingestion.py` | New `download_disp_stack()`: downloads every DISP-S1 granule of the frame that best covers the bbox. CONUS only; no query outside CONUS. |
| `backend/app/main.py` | New CLI command `kinematics --facility NAME [--bbox] [--download] [--stack-dir] [--historic-csv] [--hyperparameters auto\|per_pixel\|hybrid] [--jobs N] [--output]`. |
| `backend/requirements.txt` | Adds `scikit-learn==1.3.2`, `joblib==1.3.2`, `threadpoolctl==3.2.0`. |
| `backend/tests/test_kinematics.py` | 18 tests. |

### Engine behaviour

* **DISP-S1 inputs (CONUS).**
  * The layer names (`displacement` in metres, `temporal_coherence`), `spatial_ref` grid mapping and filename pattern were checked against OPERA's [`opera-adt/disp-s1`](https://github.com/opera-adt/disp-s1) product code (commit `10387d6`).
  * Granules are deduplicated, keeping the newest production of each (reference, secondary) pair. They must belong to a single frame.
  * Connectivity is checked from the filenames before any raster is read. A reference date that isn't an earlier epoch raises an error.
  * Each granule is clipped and reprojected to UTM 32612 with the Phase 1 helpers. The granules are then chained into a single-reference series in mm: the running sum that OPERA's `scripts/align_to_common_reference_date.py` computes. Each ministack's reference date is a previous secondary date.
* **Non-CONUS (e.g. Brumadinho).**
  * No Earthdata search or download is made; the tests assert this.
  * The series is read from `DATA_DIR/<facility>_historic.csv`, so `data/brumadinho_historic.csv` for Brumadinho.
  * Columns: `date, displacement_mm[, point_id, temporal_coherence, longitude, latitude, source]`.
  * A CSV without a coherence column is recorded as "assumed pre-filtered by source study".
* **Coherence gate.**
  * The per-pixel minimum `temporal_coherence` over the stack must be ≥ 0.65.
  * Low or unknown coherence gives status `unobservable-low-coherence`, and the pixel is never passed to the GP or the forecast; a test checks that no fit is made for it.
* **GP model.**
  * scikit-learn `GaussianProcessRegressor`, kernel `C·Matern(ν=2.5) + C·DotProduct` (the linear trend) `+ WhiteKernel`.
  * Fitted by maximum marginal likelihood on standardised displacement, with time in years and a length scale ≥ 0.1 yr.
* **Velocity and acceleration.**
  * Computed from **analytic derivatives** of the posterior for the latent function, which excludes the white noise; the outputs carry ±2σ bands. Matérn-5/2 is twice differentiable, so the acceleration variance is finite.
  * A test checks the kernel derivatives against finite differences, and checks the posterior mean and variance and the velocity against scikit-learn's own predictions.
* **Classification at the last epoch.**
  * `stable` if |v| < 2 mm/yr; no predictive step is run.
  * `moving-linear` if acceleration in the direction of motion is ≤ 2σ_a.
  * Otherwise **accelerating**, and the forecast step runs.
* **Fukuzono trend instability.**
  * Λ = 1/|v| with σ_Λ = σ_v/v² (delta method).
  * A Bayesian linear regression (Gaussian likelihood, flat prior) is fitted over the last 5 epochs: Λ = Λ₀ − (t − t₀)/A, with t₀ the last epoch.
  * The posterior covariance is inflated by the reduced χ² when the scatter exceeds the GP errors.
  * `t_instability = t₀ + AΛ₀`, with ±2σ from the delta method.
  * If Λ is not decreasing, the status is `accelerating-no-converging-trend` and no date is produced.
  * The `fukuzono_slope_significant` flag records whether the slope's 2σ band excludes zero.
* **Disclaimer.** Every output and run summary includes: *"…indicate a potential change in deformation trend, NOT a guaranteed or predicted collapse or failure date…"*. The CLI also logs it as a warning.
* **Hyperparameter modes.**
  * `per_pixel` fits every series.
  * `hybrid` (what `auto` picks above 500 observable series):
    1. Quiet ground (OLS |v| < 1 mm/yr) and moving ground each share hyperparameters, fitted on a ≤ 64-series sample of their own kind.
    2. Every series that could yield a forecast is then refitted alone and gets exactly the per-pixel result: shared-model |v| ≥ 2 mm/yr and acceleration beyond 1σ, or a moving series with a significant quadratic term (|t| > 2).
  * Per-series fits run in parallel through joblib (`--jobs`).
  * A synthetic 260 × 253 px stack (60 epochs) runs in about 30 s.
  * **Why hybrid exists:** an earlier "shared" mode fitted one set of hyperparameters on random pixels and smoothed an accelerating block down to "stable". It was replaced.
* **Output: `DATA_DIR/cache/<Facility>_kinematics.zarr`** (Blosc/zstd). Open it with `xr.open_zarr(path, decode_coords="all")`. Rasters have dims `(time, y, x)`; CSV points have `(time, point)`.
  * Series: `displacement`, `displacement_smooth`, `velocity` (mm/yr), `acceleration` (mm/yr²) and `inverse_velocity` (yr/mm), each with `_lower` / `_upper` 95 % bounds.
  * Per pixel: `temporal_coherence` and `kinematic_status` (CF flags).
  * Fukuzono results: `fukuzono_lambda_0`, `_A`, `_slope`, `_slope_std` and `_slope_significant`.
  * Timeline: `instability_time` with `_lower` / `_upper`, `instability_days_after_last_epoch`, and `instability_days_2sigma`.
  * A JSON summary goes to `DATA_DIR/logs/`.

### Verification

```
$ cd backend && pytest
108 passed, 3 skipped
```

`test_kinematics.py` (18 passed) covers:

* **GP maths:** kernel derivatives and the prior variances of f′ and f″; the posterior against scikit-learn.
* **Stable series:** −1 mm/yr creep is `stable`; the band covers the truth; no Λ and no forecast.
* **Linear creep:** −10 mm/yr creep is `moving-linear`; the velocity band covers the truth; acceleration is consistent with zero; no forecast.
* **Accelerating series** (Fukuzono-generated) yield a timeline with A > 0, Λ₀ > 0 and decreasing Λ.
* **Coherence gate:** low- and unknown-coherence series are never fitted and stay all-NaN.
* **Short series:** too few epochs gives `no-data`.
* **Hybrid mode:** matches per-pixel for every status and for the forecast-bearing series.
* **Bayesian regression:** recovers a known line, and its covariance is inflated when the scatter exceeds the stated errors.
* **DISP-S1 stack:** chaining across a reference-date change; newer production wins; mixed-frame and gap rejection; frame selection in `download_disp_stack`.
* **Brumadinho-style backtest:** no Earthdata calls; `toe` gets a forecast, `crest` is stable, `decorrelated` is unobservable; bounds and disclaimer present.
* **Missing CSV:** fails with a clear error.
* **CLI raster run:** writes `Sierrita_kinematics.zarr` containing stable, forecast and unobservable pixels, and falls back offline.

A fresh Python 3.11 virtualenv built from `requirements.txt` alone passes the full suite.

### Detection limits and bias (measured, synthetic)

**This is the most important caveat for anyone reading the outputs.** The scenarios were Fukuzono creep with true failure time t_f a set lead after the last epoch (120 epochs at 6-day spacing), Gaussian noise, and 3 seeds each.

| Scenario | Velocity at last epoch | Result |
|----------|------------------------|--------|
| t_f 30 d after last epoch, A = 0.03, noise 0.5 mm | −406 mm/yr | Forecast 3/3. Estimates **62–75 d** (truth 30 d); ±2σ only ±14–20 d |
| Same, noise 1 mm | −406 mm/yr | Forecast 3/3. Estimates 92–111 d, ±42–64 d |
| Same, noise 2 mm | −406 mm/yr | **Not detected** (classed moving-linear) |
| t_f 60 d, A = 0.03, noise 0.5 mm | −203 mm/yr | Estimates 136–157 d (truth 60 d) |
| t_f 60 d, A = 0.03, noise 1 mm | −203 mm/yr | Forecast 2/3 |
| t_f 120 d, A = 0.03, noise 0.5 mm | −101 mm/yr | Estimates 274–368 d (truth 120 d) |
| t_f 120 d, A = 0.03, noise 1 mm | −101 mm/yr | Not detected |
| A = 0.1 (gentler), any noise or lead tested | −30 to −122 mm/yr | **Never detected.** The last-epoch velocity is underestimated by about 40 % (e.g. −74 against −122 mm/yr), and its 95 % band does not cover the truth |

**Cause.** A single stationary GP over the whole history picks a long length scale and lags the final acceleration. Velocities at the series end are biased low with overconfident bands, so timelines come out **late**, which is the unsafe direction, and their ±2σ intervals are too narrow.

**What doesn't help.** Fitting the GP to a trailing 120- or 240-day window was tried; it mostly loses detection instead.

**Possible improvements**, not implemented because they change the specified method: a non-stationary or change-point kernel; Fukuzono on raw, moving-window velocities; or calibrating the bands against known failures.

Until something like that is done, treat a forecast as "accelerating trend present", and treat the date as a likely-late upper estimate.

### Known gaps

* **No real data has been run.**
  * Earthdata is blocked in this sandbox, so no DISP-S1 stack exists for Sierrita.
  * `data/brumadinho_historic.csv` is **not** included. The published Carlà et al. / Grebby et al. series could not be obtained here, and I did not want to fabricate it.
  * The backtest test uses a clearly labelled synthetic CSV in the documented schema.
  * Running the real backtest needs that CSV, with a `source` citation column.
* **`recommended_mask` is not applied.** DISP-S1's `recommended_mask` (water, unwrapping components) and `short_wavelength_displacement` are not used yet. Only `temporal_coherence` gates pixels, as specified.
* **Line-of-sight only.** Displacement is LOS, not decomposed into vertical and east components, and ascending and descending tracks are not combined.

---

## Phase 4: Hydrological flow engine and DSWx processing

### Delivered

| File | Purpose |
|------|---------|
| `backend/app/pipelines/hydrology.py` | GLO-30 download and loading; DSWx-HLS series loading and mosaicking; water boundaries; D_crest and alerts; Horn slope/aspect; priority-flood filling; Tarboton D∞ directions and accumulation; flow vectors, stream network and flow-path tracing; vegetation mask; Zarr output. |
| `backend/app/pipelines/ingestion.py` | New `download_granule_series()`: downloads the science asset of every granule touching the bbox, with an optional cloud-cover cap. It is used for the DSWx-HLS time series. |
| `backend/app/main.py` | New CLI command `hydrology --facility NAME [--bbox] [--download] [--max-cloud] [--dem ...] [--dswx-dir] [--crest] [--boundary] [--ndvi] [--stream-threshold] [--output]`. |
| `backend/tests/test_hydrology.py` | 25 tests plus 1 opt-in live GLO-30 test. |

### Engine behaviour

* **GLO-30 DEM.**
  * 1° Copernicus COG tiles come from the public `copernicus-dem-30m` AWS bucket over HTTPS, with no credentials. The tile name is the SW corner, `floor(lat)` / `floor(lon)`; this was checked against the real N31/W112 and S21/W045 tile bounds.
  * Missing (ocean) tiles are skipped. Tiles are mosaicked, cropped and reprojected (bilinear) to a **30 m UTM 32612 grid snapped to the bbox**.
  * Every other layer is put on this grid.
* **DSWx-HLS `B01_WTR`.**
  * The class codes were checked against OPERA's PROTEUS source (commit `426a9e7`): 0 not water, 1 open water, 2 partial surface water, 252 snow/ice, 253 cloud/shadow, 254 ocean, 255 fill.
  * The sensing time is parsed from the DAAC filename (`OPERA_L3_DSWx-HLS_T<tile>_<sensing>Z_<production>Z_<sensor>_30_v1.0_B01_WTR.tif`). **This pattern is from the DAAC naming convention and has not been checked against a real downloaded file.**
  * Granules are nearest-resampled onto the grid. Tiles from the same date are mosaicked; the first valid value wins.
* **Water boundary and D_crest(t).**
  * Water is WTR ∈ {1, 2} inside the facility boundary. The boundary comes from `--boundary` or `DATA_DIR/reference/<facility>_boundary.geojson`, and falls back to the bbox.
  * Water cells are polygonised at their pixel edges. D_crest is the shapely distance from those polygons to the crest line, in metres (0 if the water touches the crest).
* **Epoch status.**
  * `no-data`: less than 50 % of the facility is observed.
  * `no-water`.
  * `obscured-upper-bound`: snow, cloud, fill or vegetated cells lie closer to the crest than the observed water.
  * `valid`.
* **Alerts** are raised only on `valid` epochs, with these bit flags:
  * `1`: ΔD_crest against the first valid epoch < −30 m.
  * `2`: ΔD_crest against the previous valid epoch < −30 m.
  * `4`: D_crest < 15 m.

  Alerts are listed in the run summary and logged as warnings.
* **Terrain.**
  * Horn (1981) slope and aspect. Aspect is the downslope azimuth, clockwise from north; it is NaN at edges and on flat ground.
  * Priority-flood depression filling with an epsilon gradient (Barnes et al., 2014). `depression_depth` keeps the fill depth, which marks ponds and pits.
  * Tarboton (1997) D∞ on the 8 triangular facets: a continuous steepest-descent angle, with flow split between the two facet neighbours in proportion to the angle.
  * Flow accumulation is computed from high to low elevation, as a cell count and a contributing area in m².
  * Flow vectors (`flow_u` / `flow_v`) and accumulation vectors (contributing area × unit vector).
  * A stream network where accumulation ≥ 100 cells (0.09 km²).
  * `trace_flow_path()` follows the dominant receiver downslope, for Phase 5's slope-path checks.
* **Vegetation filter.**
  * NDVI > 0.35 comes from the Phase 2 `<Facility>_spectral.zarr` (`ndvi`) or an NDVI raster given with `--ndvi`.
  * Vegetated cells get `terrain_observability = 1`; their slope, aspect and flow direction/vectors are NaN. Accumulation still passes through them, so flow stays continuous.
  * Vegetated cells also count as unknown in the D_crest obscuration check.
  * With no NDVI source, the filter is recorded as `unavailable` and a warning is logged.
* **Output: `DATA_DIR/cache/<Facility>_hydrology.zarr`** (open with `decode_coords="all"`).
  * Grids: `elevation`, `elevation_filled`, `depression_depth`, `slope_deg`, `aspect_deg`, `flow_direction_deg`, `flow_u` / `flow_v`, `flow_accumulation_cells`, `contributing_area_m2`, `accumulation_vector_u` / `_v`, `stream_network`, `terrain_observability`.
  * With DSWx: `water_class (time, y, x)` and `water_area_m2 (time)`.
  * With a crest line, per epoch: `d_crest_m`, `d_crest_status`, `d_crest_unknown_distance_m`, `observed_fraction`, `delta_d_crest_baseline_m`, `delta_d_crest_previous_m`, `setback_alert_flags`.
  * A JSON summary goes to `DATA_DIR/logs/`.

### Verification

```
$ cd backend && pytest
133 passed, 4 skipped
$ LITHOCREEP_LIVE_TESTS=1 pytest tests/test_hydrology.py -k live   # real GLO-30 download
1 passed
```

`test_hydrology.py` (25 passed) covers:

* **Distances:** exact pixel-edge distances (120 m; 90√2 m to a segment end); crest crossing water gives 0.
* **Epoch status:** cloud-obscured and vegetation-obscured epochs; facility-boundary exclusion; no-data epochs.
* **Alerts:** baseline, previous and proximity flags, including that obscured epochs never alert and that exactly −30 m doesn't alert.
* **Terrain:** Horn slope and aspect on 6 tilted planes; flat ground.
* **D∞:** angle (30°) and the 1/3–2/3 split on a plane; mass conservation of accumulation (every unit reaches one outlet; no interior sinks after filling); a V-valley routes to its axis and outlet; trace path; pit filling.
* **Inputs:** GLO-30 tile names (N/S/E/W, multi-tile); download with streaming, 404 skip and reuse; DSWx filename parsing and same-day mosaicking.
* **Vegetation:** masking of terrain outputs.
* **CLI end to end:** synthetic DEM, 4 DSWx epochs and a crest GeoJSON. D_crest is ≈ 300 / 310 / 60 / 5 m, with shrink and proximity alerts; the offline fallbacks are logged.

**Real-data run** (`hydrology --facility Sierrita --download`, GLO-30 downloaded live):

| Measure | Value |
|---------|-------|
| Grid | 260 × 253 cells (30 m) |
| Elevation | 710.5–1309.9 m |
| Slope | median 3.4°, 95th percentile 20.9° |
| Largest contributing area | 16.0 km² |
| Stream cells | 4,343 |
| Closed depressions > 1 m deep | 0.66 km² (fill depth up to 20.8 m) |

This run produced terrain products only. DSWx was skipped because Earthdata is blocked, and there was no crest line and no NDVI.

### Known gaps

* **No crest line for any facility.** D_crest and the setback alerts need an embankment crest LineString, passed with `--crest` or saved as `DATA_DIR/reference/<facility>_crest.geojson`.
  * I did not invent or derive one; a crest traced from imagery or the operator's GISTM disclosure is needed.
  * Phase 6's `crest_line_geom` field should be the long-term source.
* **DSWx has not run on real granules.** Earthdata is blocked in this sandbox, so neither the filename pattern nor the mosaicking has met a real granule.
* **Alerts can be triggered by pixel noise.** The −30 m shrink threshold equals one DSWx pixel, so a single-pixel shoreline flicker can trigger an alert. Consider requiring persistence over two or more valid epochs before acting on one.
* **The DEM is a decade old.** GLO-30 is a surface model (it includes canopy and structures) built from TanDEM-X data acquired around 2011–2015. At an active tailings facility the embankments and beaches have changed since, so slopes and flow paths over the facility itself are historical.
* **Coarse vegetation mask.** The NDVI comes from 60 m EMIT data (Phase 2) bilinearly resampled to 30 m, so it is coarse at the edges.

---

## Phase 5: Co-anomaly coupling and spatial-topological classifier

### Delivered

| File | Purpose |
|------|---------|
| `backend/app/pipelines/coupling.py` | The coupling step:<br>• grid alignment<br>• the D∞ DAG with acyclicity check and up/downslope propagation<br>• the aspect-path validator<br>• coupling rule, regime classifier, uncertainty model and alert tiers<br>• Zarr output |
| `backend/app/main.py` | New CLI command `couple --facility NAME [--spectral] [--kinematics] [--hydrology] [--earthworks] [--output]`. Inputs default to the cache Zarrs of Phases 2–4, and a missing input names the command that produces it. |
| `backend/tests/test_coupling.py` | 18 tests, including a full-chain contract test that runs the real Phase 2, 3 and 4 pipelines. |
| `DECISIONS.md` | New. Design decisions for all phases, plus the classification results. |

### Behaviour (details and reasons in `DECISIONS.md`, C1–C11)

* **Alignment.** Every layer goes onto the Phase 4 grid (30 m, EPSG:32612).
  * EMIT 60 m layers: bilinear for continuous values, nearest for flags.
  * DISP-S1 layers: nearest.
  * Grid identity is tested.
* **DAG.** Built from D∞ receivers on the filled DEM. Acyclicity is asserted on every run, and up/downslope reachability uses linear passes in topological order.
* **Coupling.** A candidate is a mid-slope kinematic anomaly (velocity or acceleration beyond 2σ). It is coupled when all of these hold:
  * the pond boundary is a DAG ancestor;
  * an SAI > 0.25 cell is a strict descendant;
  * the setback is encroaching (Phase 4 alert at the latest valid epoch);
  * Horn-aspect D8 paths independently confirm both links.
* **Regimes.**
  * REGIME_1 benign.
  * REGIME_2 unobservable or anthropogenic: low coherence, or an earthworks polygon.
  * REGIME_3 legacy seepage, Tier-1 watch.
  * REGIME_4 active instability, Tier-3 critical, with a failure-time estimate ±2σ.
  * Two extra codes: **0** rejected/unclassified (RMSE > 0.05, vegetated, missing data), and **5** kinematic anomaly with low SAI (outside the 4-regime scheme; see C5).
  * Flag bits record low coherence, high RMSE, earthworks, vegetation, missing inputs, an SAI interval that straddles 0.25, and accelerating-not-coupled.
* **Uncertainty (95 % bounds).**
  * SAI: ±2 × 0.70 × RMSE (linear propagation through the endmember matrix).
  * Velocity: GP and InSAR phase noise (Cramér–Rao, Sentinel-1 λ) combined.
  * Acceleration: GP bounds.
  * Failure time: the Phase 3 bounds.
  * `low_confidence` marks cells with RMSE > 0.05 or coherence < 0.65.
* **Output: `DATA_DIR/cache/<Facility>_coupled.zarr`.**
  * Classification: `regime`, `alert_tier`, `classification_flags`, `low_confidence`.
  * Aligned values with bounds: `sai*`, `velocity*`, `acceleration*`, `spectral_rmse`, `temporal_coherence`.
  * Topology: `water_boundary`, `upslope_water`, `downslope_seepage`, `mid_slope`, `kinematic_anomaly`, `aspect_validated`, `coupled`.
  * Failure time: `failure_days_*` and `failure_time_*`.
  * Attributes: `facility_hazard_level`, `regime_counts`, `setback_state`, `input_provenance`, `contains_synthetic_or_mock_inputs` and the disclaimer. A JSON summary also goes to `DATA_DIR/logs/`.

### Verification

```
$ cd backend && pytest
151 passed, 4 skipped
```

`test_coupling.py` (18 passed) covers:

* **Regimes and tiers:** every regime and alert tier on a 7-block scenario (codes 0–5, Tier-1 and Tier-3, all flags). REGIME_4 carries its failure time and the disclaimer.
* **Scenario variations:**
  * With no encroachment, A drops to REGIME_3.
  * With aspect paths broken, D∞ still connects but validation fails, so A drops to REGIME_3.
  * An earthworks polygon forces REGIME_2, both as a mask and through a GeoJSON file.
  * A quiet facility comes out `NONE`.
* **Alignment and topology:** grid identity after alignment; acyclicity and topological order on rough terrain; up/downslope propagation; aspect paths; water-boundary detection.
* **Uncertainty:** the σ_SAI factor (0.70) and SAI bounds; the marginal-SAI flag; the phase-noise formula; the combined velocity interval.
* **CLI:** the run writes `Sierrita_coupled.zarr` and reports synthetic provenance; missing inputs are reported.
* **Full chain:** real Phase 2/3/4 pipeline code on a ~1 km synthetic site leads to REGIME_4 on the accelerating block and REGIME_3 on the seepage toe. This test found a real bug: Phase 3's CF `units: days` fields decode as `timedelta64[ns]`, and the coupler read nanoseconds as days. It is fixed with `as_days()`, and the unit fixture now carries the same units, so it fails without the fix.

### Classification results

See `DECISIONS.md` → *Results to date* for the full tables. In short:

* **Real data:** none. Only GLO-30 terrain is real so far, and EMIT, DISP-S1 and DSWx inputs could not be fetched.
* **Synthetic block scenario:** the facility level is CRITICAL_TIER3. Cell counts:

  | Regime | Cells |
  |--------|-------|
  | 1 | 1348 |
  | 2 | 16 |
  | 3 | 196 |
  | 4 | 12 |
  | 5 | 16 |
  | 0 | 12 |

* **Synthetic full chain:** CRITICAL_TIER3, with 16 REGIME_4 cells. Failure estimates are 32–48 days, against a true 30 days.

### Known gaps

* **Single cells drive the facility level.** One REGIME_4 cell sets `CRITICAL_TIER3`; there is no minimum cluster size or persistence over time yet.
* **`ALERT_TIER2` is never issued.** No regime defines it.
* **Earthworks need an operator polygon.** There is no automatic detector.
* **Strict coupling.** Coupling needs both a crest line (for the encroaching-setback test) and DSWx water. Without them, no cell can reach REGIME_4; the setback state records why.
* **Facility-wide setback.** Encroachment is tested facility-wide (the latest valid epoch), not per shoreline segment.
* **Edge resampling effects.**
  * Bilinear 60 → 30 m resampling dilutes SAI at block edges, which creates code-5 fringes.
  * Grid-edge cells have no Horn aspect, so they can't be aspect-validated.

