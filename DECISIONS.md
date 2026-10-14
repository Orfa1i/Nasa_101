# DECISIONS.md

Design decisions for LithoCreep, with the reason for each. Results are labelled by provenance. **No classification has yet been run on real Sierrita EMIT, DISP-S1 or DSWx data**; see [Results to date](#results-to-date).

## Cross-cutting

| # | Decision | Reason |
|---|----------|--------|
| X1 | Every product is stored as Zarr (Blosc/zstd) or GeoTIFF on UTM 12N (`EPSG:32612`), with CRS in `spatial_ref`. Read Zarr with `xr.open_zarr(path, decode_coords="all")`. | One CRS for every metric computation. Writing `grid_mapping` makes the CRS survive round trips. |
| X2 | Duration fields carry CF `units: "days"`, so xarray decodes them as `timedelta64[ns]`. Consumers convert with `coupling.as_days()`. The fields are `instability_days_*` (kinematics) and `failure_days_*` (coupled). | This is correct CF. It caused a real bug in Phase 5 (nanoseconds read as days); the full-chain test now guards against it. |
| X3 | Mock and synthetic inputs are labelled (`origin`, `data_label`, `source_origin`, `contains_synthetic_or_mock_inputs`). The labels carry forward to the coupled output and the CLI warns about them. | The playbook requires authentic observations to be visibly separated from simulated ones. |
| X4 | No data is fabricated: there is no Brumadinho CSV, no crest line, and no splib07 file. A missing input gives a clear error or an `unavailable` attribute. | A screening tool must not present invented observations as evidence. |

## Phase 1: ingestion

| # | Decision | Reason |
|---|----------|--------|
| I1 | Transitive pins are added to `requirements.txt`: setuptools, pandas < 3, h5netcdf/h5py, zarr/numcodecs, rasterio/affine, and scikit-learn. | The 13 specified pins alone don't install a working environment; pandas 3 breaks pyogrio 0.7.2. |
| I2 | DISP-S1 outside CONUS never queries CMR; it uses cached or historical backups instead. | DISP-S1 is North-America-only. |
| I3 | If Earthdata is unreachable, the pipeline falls back to mock metadata and **labels** the outputs. Missing credentials raise an error. | Offline development is possible, but mock data is never silently used as real. |

## Phase 2: spectroscopy

| # | Decision | Reason |
|---|----------|--------|
| S1 | **245 active channels, not 242.** The specified windows (1340–1460, 1790–1970 nm) remove 40 of EMIT's 285 bands on every calibration from 2022 to 2026. | Follow the physical definition. Nothing hard-codes 242. Also dropping the 3 bands below 400 nm would give 242 if that was the intent. |
| S2 | The endmembers come from **USGS splib06a** (public domain, via EMIT's Tetracorder repository). A `usgs_splib07_extract.csv` with the same columns overrides it. | splib07 was not reachable from the build environment. |
| S3 | Quartz-Feldspar is a derived 50/50 quartz–orthoclase mix. | splib06a has no single quartz–feldspar spectrum. |
| S4 | Endmembers stay in reflectance units; they are not rescaled. | Keeps NNLS fractions meaningful as areal abundances under `Σf ≤ 1`. |
| S5 | The sum constraint is handled by solving plain NNLS first; if `Σf > 1`, it is re-solved with an FCLS-style weighted row. | Exact for a convex problem with one inequality constraint. |

## Phase 3: kinematics

| # | Decision | Reason |
|---|----------|--------|
| K1 | Velocity and acceleration come from analytic derivatives of the GP posterior for the latent function. | Finite differences of scikit-learn predictions would include white-noise variance. |
| K2 | The coherence gate uses the **minimum** `temporal_coherence` over the stack. | Strictest reading of "never run predictive functions on low coherence". |
| K3 | The trend regression is a Bayesian linear regression (flat prior, Gaussian noise from GP-propagated σ_Λ), inflated by the reduced χ² when residuals are larger. | Stops overly tight GP errors from producing overconfident timelines. |
| K4 | Hybrid hyperparameter mode: quiet and moving ground each share fitted hyperparameters, and anything that could yield a forecast is refitted per pixel. | A single shared fit smoothed an accelerating block down to "stable" in testing. |
| K5 | **Timelines are biased late.** In synthetic tests the estimate is roughly twice the true lead time, and the ±2σ band is too narrow. This is documented, not tuned away. | Stationary-GP lag at the series end. Trailing windows were tried and lose detection. Read a timeline as "trend present", with the date as an upper estimate. |

## Phase 4: hydrology

| # | Decision | Reason |
|---|----------|--------|
| H1 | The master grid is the GLO-30 DEM reprojected to **30 m UTM 32612, snapped to the bbox**. | GLO-30 is the only openly downloadable layer and sets the routing grid; other layers are resampled onto it. |
| H2 | D_crest is measured from the **pixel-edge polygons** of the water mask, not from pixel centres. | Exact geometric distance for a 30 m classification. |
| H3 | An epoch is **obscured** when snow, cloud, fill or vegetation is closer to the crest than the observed water; it raises no alert. | Clouds can hide an encroaching shoreline. |
| H4 | Routing uses priority-flood + epsilon filling, then Tarboton D∞. | Guarantees drainage, and therefore an acyclic graph for Phase 5. |
| H5 | Under dense vegetation (NDVI > 0.35), slope, aspect and flow direction are masked, but accumulation passes through. | GLO-30 is a surface model, so canopy hides the ground. Flow continuity is kept. |

## Phase 5: coupling and classification

| # | Decision | Reason |
|---|----------|--------|
| C1 | **Alignment.** The master grid is the Phase 4 DEM grid. EMIT continuous layers use bilinear resampling; flags use nearest; DISP-S1 layers all use nearest. A test checks transform, shape and CRS. | Bilinear suits 60 m → 30 m continuous fields. Nearest keeps each InSAR cell's GP fit (value, σ and status) internally consistent. |
| C2 | **DAG.** Cells link to their D∞ receivers on the filled DEM. Edges strictly descend, so descending elevation is the topological order; `dag_is_acyclic()` asserts this on every run. | Gives "upslope of" and "downslope of" an exact, testable meaning. |
| C3 | **Coupling** has all of the following parts. | This is the specified connectivity rule, plus the specified aspect-path validation as an independent check. |
| | • Candidate: a mid-slope cell (has both donors and receivers; not water) with \|v\| > 2σ_v and ≥ 2 mm/yr, or acceleration along the motion > 2σ_a. | |
| | • A pond-boundary cell is a DAG ancestor. | |
| | • A cell with SAI > 0.25 is a strict DAG descendant. | |
| | • The latest valid Phase 4 epoch carries a shrink or proximity alert. | |
| | • Horn-aspect D8 paths confirm both links, within one cell. | |
| C4 | **Regime precedence**, first match wins. | REGIME_2 is defined by unobservability, so it overrides. The rejection rule in requirement 4 applies before any SAI or velocity regime. |
| | 1. Coherence < 0.65 (or unknown inside the stack) or earthworks → REGIME_2 | |
| | 2. Spectral RMSE > 0.05, vegetated, or missing data → 0 (rejected/unclassified) | |
| | 3. High SAI + accelerating + coupled → REGIME_4 | |
| | 4. High SAI → REGIME_3 | |
| | 5. Low SAI + accelerating → 5 | |
| | 6. Otherwise → REGIME_1 | |
| C5 | **Code 5, `kinematic-anomaly-low-sai`.** It is outside the 4-regime scheme. | The specification leaves accelerating ground with low SAI undefined. Calling it "benign" (REGIME_1) would hide a kinematic warning, so it is reported separately. In the full-chain scenario, 24 such cells sit on the fringe of a moving block where 60 m SAI resampled to 30 m dips under 0.25. |
| C6 | **High SAI + accelerating but not coupled** → REGIME_3 plus the `accelerating-not-coupled` flag. | No pond connection means REGIME_4's condition isn't met, but the cell still deserves a watch, and the flag keeps the acceleration visible. |
| C7 | **"Active earthworks"** comes only from an operator-supplied polygon (`couple --earthworks`). | No defensible automatic detector is available from these inputs. Low coherence already captures most decorrelating works. |
| C8 | **Uncertainty.** | A linear propagation of the unmixing residual through M. The 0.70 factor is conservative: a Monte-Carlo run through NNLS gives about 0.42. The phase-noise term partly overlaps the GP's noise term, again on the conservative side. |
| | • σ_SAI = RMSE × √(wᵀ(MᵀM)⁻¹w), which is 0.70 × RMSE for the bundled library. | |
| | • σ_v = √(σ_GP² + σ_phase²), with single-look Cramér–Rao phase noise (λ = 55.47 mm) propagated to a velocity over the stack span. | |
| C9 | **Low confidence** means RMSE > 0.05 or coherence < 0.65. A cell whose SAI interval straddles 0.25 gets the `sai-interval-straddles-threshold` flag but keeps its regime. | Requirement 4 says to reject; marginal SAI is information, not grounds for rejection. |
| C10 | **Facility hazard level.** `CRITICAL_TIER3` if any REGIME_4 cell; `WATCH_TIER1` if any REGIME_3 cell; otherwise `NONE`. `ALERT_TIER2` is never issued. | The regime scheme defines only Tier-1 and Tier-3. A single cell triggers the facility level, so cluster-size filtering is a candidate refinement. |
| C11 | **Only REGIME_4 cells carry failure-time estimates.** They are kept as numeric days, and also as dates when within ±200 years. | Weak inverse-velocity trends extrapolate centuries ahead, which overflows nanosecond datetimes. |

## Results to date

**Real data.** Only GLO-30 terrain has been processed for real (Phase 4: Sierrita, 260 × 253 cells). Earthdata is blocked in the development sandbox, so there are no real EMIT, DISP-S1 or DSWx inputs, and **no real hazard classification exists for any facility**.

**Synthetic scenario 1, block layout** (`tests/test_coupling.py`; 40 × 40 cells, mock spectral origin):

| Block | Regime | Flags | SAI [95 %] | v mm/yr [95 %] |
|-------|--------|-------|------------|----------------|
| A: high SAI, accelerating, below pond | **4** | – | 0.500 [0.486, 0.514] | −30.0 [−32.2, −27.8] |
| B: high SAI, stable | **3** | – | 0.500 [0.486, 0.514] | 0.0 [−2.2, 2.2] |
| C: low SAI, linear | **1** | – | 0.050 [0.036, 0.064] | −10.0 [−12.2, −7.8] |
| D: high SAI, accelerating, no pond upslope | **3** | accelerating-not-coupled | 0.500 | −30.0 |
| E: low SAI, accelerating | **5** | – | 0.050 | −30.0 |
| F: coherence 0.4 | **2** | low-coherence | 0.050 | 0.0 [−5.0, 5.0] |
| G: RMSE 0.08 | **0** | high-spectral-rmse | 0.050 [−0.062, 0.162] | 0.0 |

Facility totals:

| Regime | Cells |
|--------|-------|
| 1 | 1348 |
| 2 | 16 |
| 3 | 196 |
| 4 | 12 |
| 5 | 16 |
| 0 | 12 |

The facility level is **CRITICAL_TIER3**. Block A gives 12 REGIME_4 cells, not 16, because the grid-edge column has no Horn aspect, so the aspect check can't confirm it. Varying the scenario: with no encroaching setback, A becomes REGIME_3 and the facility drops to WATCH_TIER1. With aspect missing between the pond and the slope, A also becomes REGIME_3.

**Synthetic scenario 2, full chain** (real Phase 2 → 3 → 4 → 5 code; ~1 km site; mock EMIT cube, synthetic DISP-S1 stack and DSWx):

| Regime | Cells |
|--------|-------|
| 1 | 1112 |
| 3 | 64 (seepage toe) |
| 4 | 16 (accelerating block) |
| 5 | 24 (block fringe; see C5) |

The facility level is **CRITICAL_TIER3**; 40 cells are coupled, and the setback shrank 60 m (150 → 90 m). REGIME_4 failure-time estimates are **32–48 days ± 12–14 days, against a true 30 days**, consistent with K5's late bias.
