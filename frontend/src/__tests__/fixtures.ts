/**
 * Small API fixtures in the exact response shapes of backend/app/api/schemas.py, and a fetch stub
 * that routes /api/* requests to them. (The browser tests in e2e/ run against the real backend.)
 */
import { vi } from 'vitest';

import type {
  Alert,
  AuditResponse,
  AuditStatus,
  BacktestResponse,
  DashboardSummary,
  DataSource,
  Facility,
  FukuzonoFit,
  KinematicSeries,
  LayerInfo,
  Matrix,
  PixelResponse,
  Spectrum,
  StressTestResponse,
} from '@/utils/api';

export const SIERRITA_BBOX: [number, number, number, number] = [-111.12, 31.83, -111.04, 31.9];

export const FACILITIES: Facility[] = [
  {
    id: 'sierrita', name: 'Freeport-McMoRan Sierrita TSF', country: 'USA', latitude: 31.865, longitude: -111.08,
    bbox_xmin: -111.12, bbox_ymin: 31.83, bbox_xmax: -111.04, bbox_ymax: 31.9,
    mine_type: 'copper-molybdenum', embankment_type: 'unknown', crest_line_geom: null, status: 'ACTIVE',
  },
  {
    id: 'mission', name: 'ASARCO Mission Complex TSF', country: 'USA', latitude: 31.975, longitude: -111.04,
    bbox_xmin: -111.08, bbox_ymin: 31.95, bbox_xmax: -111.0, bbox_ymax: 32.0,
    mine_type: 'copper', embankment_type: 'unknown', crest_line_geom: null, status: 'ACTIVE',
  },
];

export const SIERRITA_ALERT: Alert = {
  id: 1, facility_id: 'sierrita', timestamp: '2026-10-14T09:10:41', hazard_level: 'CRITICAL_TIER3',
  failure_date_est: '2025-01-13T16:12:12', failure_date_error_days: 13.55,
  diagnostic_evidence: { contains_synthetic_or_mock_inputs: true },
};

export const SYNTH: DataSource = { product: 'coupled', file: 'Sierrita_coupled.zarr', variable: 'regime', synthetic_or_mock: true, label: 'SYNTHETIC: demo' };
export const REAL_DISP: DataSource = { product: 'kinematics', file: 'Sierrita_kinematics.zarr', variable: 'displacement_smooth', synthetic_or_mock: false, label: 'OPERA_L3_DISP-S1_V1' };

const ROWS = 4;
const COLS = 5;
export const DISP_TIMES = ['2023-01-13T00:00:00', '2023-01-25T00:00:00', '2023-02-06T00:00:00'];
export const DSWX_TIMES = ['2023-01-25T00:00:00', '2023-03-05T00:00:00'];

function grid(f: (r: number, c: number) => number | null): (number | null)[][] {
  return Array.from({ length: ROWS }, (_, r) => Array.from({ length: COLS }, (_, c) => f(r, c)));
}

export function matrix(layer: string, values: (number | null)[][], extra: Partial<Matrix> = {}): Matrix {
  const valid = values.flat().filter((v) => v !== null).length / (ROWS * COLS);
  return {
    facility_id: 'sierrita', layer, units: 'u', categorical: false, crs: 'EPSG:4326', bounds: SIERRITA_BBOX,
    shape: [ROWS, COLS], resolution_deg: [0.016, 0.0175], time: null, requested_time: null, available_times: [],
    values, valid_fraction: valid, flag_meanings: null, source: SYNTH, note: null, ...extra,
  };
}

/** Regime 4 in the top-left cell, Regime 1 elsewhere, null in the last column. */
export const MATRICES: Record<string, (t: string | null) => Matrix> = {
  anomaly_regime: () => matrix('anomaly_regime', grid((r, c) => (c === COLS - 1 ? null : r === 0 && c === 0 ? 4 : 1)), { categorical: true, units: 'regime code' }),
  sai: () => matrix('sai', grid((r, c) => (c === COLS - 1 ? null : r === 0 ? 0.5 : 0.02)), { units: 'index' }),
  spectral_rmse: () => matrix('spectral_rmse', grid((r, c) => (c === COLS - 1 ? null : r === 3 ? 0.08 : 0.01)), { units: 'reflectance' }),
  coherence: () => matrix('coherence', grid((r, c) => (c === COLS - 1 ? null : c === 2 ? 0.4 : 0.9)), { units: 'unitless', source: { ...REAL_DISP, variable: 'temporal_coherence' } }),
  displacement: (t) => {
    const i = t ? Math.max(0, DISP_TIMES.findIndex((x) => x.slice(0, 10) === t.slice(0, 10))) : DISP_TIMES.length - 1;
    return matrix('displacement', grid((r, c) => (c === COLS - 1 ? null : -(i + 1) * (r + 1))), {
      units: 'mm', time: DISP_TIMES[i], available_times: DISP_TIMES, source: REAL_DISP,
    });
  },
  flow_accumulation: () => matrix('flow_accumulation', grid((r, c) => (c === 1 ? 500 : 2)), { units: 'cells' }),
  // vegetated (1) at row 1 col 0, decorrelated (2) down column 2
  observability_mask: () =>
    matrix('observability_mask', grid((r, c) => (c === COLS - 1 ? null : r === 1 && c === 0 ? 1 : c === 2 ? 2 : 0)), { categorical: true, units: 'mask code' }),
};

export function layerInfo(available: string[]): LayerInfo[] {
  const all = ['sai', 'spectral_rmse', 'displacement', 'coherence', 'anomaly_regime', 'flow_accumulation', 'water_class', 'observability_mask'];
  return all.map((layer) => ({
    layer,
    units: 'u',
    categorical: false,
    time_dependent: layer === 'displacement' || layer === 'water_class',
    available: available.includes(layer),
    source: available.includes(layer) ? SYNTH : null,
    times: !available.includes(layer) ? [] : layer === 'displacement' ? DISP_TIMES : layer === 'water_class' ? DSWX_TIMES : [],
    detail: available.includes(layer) ? null : `Mission_${layer}.zarr not found`,
  }));
}

/** 285 EMIT-like band centres (381-2493 nm); the water-vapour windows are inactive (40 channels). */
export const WAVELENGTHS = Array.from({ length: 285 }, (_, i) => 381 + i * ((2493 - 381) / 284));
export const ACTIVE = WAVELENGTHS.map((w) => !((w >= 1340 && w <= 1460) || (w >= 1790 && w <= 1970)));

export const SPECTRUM: Spectrum = {
  match: { method: 'nearest', nearest_cell_distance_m: 21, note: 'spectrum, fit and fractions of the nearest cell' },
  wavelengths_nm: WAVELENGTHS,
  reflectance: WAVELENGTHS.map((w) => 0.3 + 0.1 * Math.sin(w / 300)),
  active: ACTIVE,
  modelled: WAVELENGTHS.map((w, i) => (ACTIVE[i] ? 0.3 + 0.1 * Math.sin(w / 300) + 0.002 : null)),
  excluded_windows_nm: [[1340, 1460], [1790, 1970]],
  abundance: { Jarosite: 0.6, Goethite: 0.05, Ferrihydrite: 0, Montmorillonite: 0.25, Gypsum: 0.02, 'Quartz-Feldspar': 0.06, 'Water-Shade': 0.02 },
  rmse: 0.0123,
  sai: 0.47,
  endmember_titles: { Jarosite: 'Jarosite GDS99 K 200C Syn W1R1Ba AREF' },
};

export const K_TIMES = ['2024-01-01T00:00:00', '2024-01-13T00:00:00', '2024-01-25T00:00:00', '2024-02-06T00:00:00', '2024-02-18T00:00:00', '2024-03-01T00:00:00', '2024-03-13T00:00:00'];

export const FIT: FukuzonoFit = {
  window_times: K_TIMES.slice(2),
  inverse_velocity_yr_mm: [0.05, 0.04, 0.03, 0.02, 0.01],
  inverse_velocity_lower_yr_mm: [0.045, 0.035, 0.025, 0.015, 0.006],
  inverse_velocity_upper_yr_mm: [0.055, 0.045, 0.035, 0.025, 0.014],
  t0: K_TIMES[6],
  lambda_0_yr_mm: 0.01,
  slope_yr_mm_per_yr: -0.3,
  slope_std: 0.02,
  slope_significant: true,
  line_times: ['2024-01-25T00:00:00', '2024-03-13T00:00:00', '2024-03-25T00:00:00', '2024-04-06T00:00:00'],
  line_yr_mm: [0.05, 0.01, 0.0, -0.01],
  line_lower_yr_mm: [0.046, 0.007, -0.004, -0.016],
  line_upper_yr_mm: [0.054, 0.013, 0.004, -0.004],
  crossing_estimate: '2024-03-25T00:00:00',
  crossing_lower: '2024-03-20T00:00:00',
  crossing_upper: '2024-04-01T00:00:00',
  label: 'Estimated change in velocity trend (inverse-velocity extrapolation), not a guaranteed failure date',
};

export const KINEMATICS: KinematicSeries = {
  source: REAL_DISP, match: { method: 'bilinear', nearest_cell_distance_m: 10, note: 'series: bilinear between the 4 nearest cells; status and timeline: nearest cell' },
  times: K_TIMES,
  displacement_mm: [0, -1, -2.5, -4.5, -7.5, -12, -20],
  displacement_smooth_mm: [0, -1.1, -2.4, -4.6, -7.4, -12.1, -19.8],
  displacement_smooth_lower_mm: [-0.5, -1.6, -2.9, -5.1, -7.9, -12.6, -20.6],
  displacement_smooth_upper_mm: [0.5, -0.6, -1.9, -4.1, -6.9, -11.6, -19.0],
  velocity_mm_yr: [-25, -30, -40, -55, -80, -120, -200],
  velocity_lower_mm_yr: [-30, -35, -45, -60, -85, -128, -215],
  velocity_upper_mm_yr: [-20, -25, -35, -50, -75, -112, -185],
  acceleration_mm_yr2: [-50, -100, -200, -400, -800, -1500, -2600],
  acceleration_lower_mm_yr2: [-150, -200, -300, -500, -900, -1700, -3000],
  acceleration_upper_mm_yr2: [50, 0, -100, -300, -700, -1300, -2200],
  inverse_velocity_yr_mm: [0.04, 0.033, 0.05, 0.04, 0.03, 0.02, 0.01],
  temporal_coherence: 0.91, coherence_threshold: 0.65, status: 'accelerating-forecast',
  instability_time: '2024-03-25T00:00:00', instability_time_lower: '2024-03-20T00:00:00', instability_time_upper: '2024-04-01T00:00:00',
  disclaimer: 'Trend instability timelines are statistical extrapolations ... NOT a guaranteed or predicted collapse or failure date.',
  fukuzono: FIT,
};

export const PIXEL: PixelResponse = {
  facility_id: 'sierrita', lat: 31.89, lon: -111.11, utm_x: 0, utm_y: 0, utm_crs: 'EPSG:32612',
  kinematics: KINEMATICS,
  spectral: {
    source: SYNTH, match: { method: 'bilinear', nearest_cell_distance_m: 10, note: null }, sai: 0.48, rmse: 0.0123, ndvi: 0.1, confidence: 0.9,
    observability: 'observable', abundance: { Jarosite: 0.58, Montmorillonite: 0.24 }, spectrum: SPECTRUM, spectrum_unavailable: null,
  },
  hydrology: null,
  classification: {
    source: SYNTH, match: { method: 'nearest', nearest_cell_distance_m: 10, note: null }, regime: 4, regime_meaning: 'regime4-active-instability-tier3-critical',
    alert_tier: 3, flags: [], low_confidence: false, sai_interval: [0.4, 0.55], velocity_interval_mm_yr: [-30, -20],
    failure_time_estimate: '2025-01-13T00:00:00', failure_time_lower: '2024-12-31T00:00:00', failure_time_upper: '2025-01-27T00:00:00',
  },
  unavailable: { hydrology: 'Sierrita_hydrology.zarr not found' },
};

export const DASHBOARD: DashboardSummary = {
  facility_id: 'sierrita',
  risk_classification: 'Tier-3 / Active Instability',
  hydrological_setback: {
    facility_id: 'sierrita', available: true, crest_line: 'crest.geojson',
    thresholds: { shrink_alert_m: -30, proximity_alert_m: 15 }, n_epochs: 3, n_valid_epochs: 3,
    latest_valid: { date: '2024-05-05', d_crest_m: 12, status: 'valid', delta_vs_baseline_m: -138, delta_vs_previous_m: -78, alerts: ['shrink_vs_baseline', 'shrink_vs_previous', 'crest_proximity'], observed_fraction_pct: 100, water_area_m2: 2e5 },
    min_valid_d_crest_m: 12, encroaching: true,
    series: [
      { date: '2024-01-05', d_crest_m: 150, status: 'valid', delta_vs_baseline_m: 0, delta_vs_previous_m: null, alerts: [], observed_fraction_pct: 100, water_area_m2: 72000 },
      { date: '2024-03-05', d_crest_m: 90, status: 'valid', delta_vs_baseline_m: -60, delta_vs_previous_m: -60, alerts: ['shrink_vs_baseline', 'shrink_vs_previous'], observed_fraction_pct: 100, water_area_m2: 108000 },
      { date: '2024-05-05', d_crest_m: 12, status: 'valid', delta_vs_baseline_m: -138, delta_vs_previous_m: -78, alerts: ['shrink_vs_baseline', 'shrink_vs_previous', 'crest_proximity'], observed_fraction_pct: 100, water_area_m2: 2e5 },
    ],
  },
  consistency_checks: [],
  all_checks_passed: true,
  gistm_screening: {
    candidate_issues: [{ gistm_principle: 'Principle 6: plan, build and operate the facility to manage risk', issue: 'The pond-to-crest setback triggered a shrink or proximity alert.', evidence_paths: ['get_hydrological_setback:latest_valid'] }],
    recommendations: [],
    data_limitations: [],
  },
};

// --------------------------------------------------------------------------- //
// Phase 10 fixtures
// --------------------------------------------------------------------------- //

export const BRUMADINHO: Facility = {
  id: 'brumadinho', name: 'Brumadinho Dam I (Corrego do Feijao)', country: 'Brazil', latitude: -20.12, longitude: -44.125,
  bbox_xmin: -44.15, bbox_ymin: -20.14, bbox_xmax: -44.1, bbox_ymax: -20.1,
  mine_type: 'iron ore', embankment_type: 'upstream', crest_line_geom: null, status: 'FAILED',
};

const BT_TIMES = ['2018-12-12T00:00:00', '2018-12-18T00:00:00', '2018-12-24T00:00:00', '2018-12-30T00:00:00', '2019-01-05T00:00:00', '2019-01-11T00:00:00', '2019-01-17T00:00:00'];

export function backtest(over: Partial<BacktestResponse> = {}): BacktestResponse {
  return {
    facility_id: 'brumadinho',
    collapse_date: '2019-01-25T00:00:00',
    historical: true,
    dataset_label: 'HISTORICAL DATASET: Example et al. (published series)',
    source: { product: 'kinematics', file: 'brumadinho_historic.csv', variable: 'displacement', synthetic_or_mock: false, label: 'historical backup; Example et al.' },
    citation: 'Example et al. (published series)',
    points: [
      { point_id: 'toe', zone: 'toe', longitude: -44.121, latitude: -20.119, temporal_coherence: 0.85 },
      { point_id: 'crest', zone: 'crest', longitude: -44.118, latitude: -20.116, temporal_coherence: 0.85 },
    ],
    point_id: 'toe',
    window_days: 40,
    window_start: '2018-12-16T00:00:00',
    cutoff_days: 0,
    series: {
      ...KINEMATICS,
      times: BT_TIMES,
      inverse_velocity_yr_mm: [0.06, 0.05, 0.04, 0.03, 0.02, 0.012, 0.006],
      fukuzono: {
        ...FIT,
        window_times: BT_TIMES.slice(2),
        t0: BT_TIMES[6],
        line_times: ['2018-12-24T00:00:00', '2019-01-17T00:00:00', '2019-01-26T00:00:00'],
        line_yr_mm: [0.04, 0.006, 0],
        line_lower_yr_mm: [0.036, 0.003, -0.003],
        line_upper_yr_mm: [0.044, 0.009, 0.003],
        crossing_estimate: '2019-01-26T15:00:00',
        crossing_lower: '2019-01-22T00:00:00',
        crossing_upper: '2019-01-30T00:00:00',
      },
    },
    outcome: {
      issued: '2019-01-17T00:00:00', lead_days: 8, estimate: '2019-01-26T15:00:00', lower: '2019-01-22T00:00:00', upper: '2019-01-30T00:00:00',
      error_days: 1.6, collapse_within_95: true, status: 'accelerating-forecast',
    },
    sweep: [
      { issued: '2019-01-11T00:00:00', lead_days: 14, estimate: '2019-02-02T00:00:00', lower: '2019-01-24T00:00:00', upper: '2019-02-11T00:00:00', error_days: 8, collapse_within_95: true, status: 'accelerating-forecast' },
      { issued: '2019-01-17T00:00:00', lead_days: 8, estimate: '2019-01-26T15:00:00', lower: '2019-01-22T00:00:00', upper: '2019-01-30T00:00:00', error_days: 1.6, collapse_within_95: true, status: 'accelerating-forecast' },
    ],
    disclaimer: 'Trend instability timelines are statistical extrapolations ... NOT a guaranteed or predicted collapse or failure date.',
    ...over,
  };
}

export const BACKTEST_MISSING = {
  message: 'brumadinho_historic.csv not found in DATA_DIR. OPERA DISP-S1 does not cover South America ... It is not bundled.',
  expected_file: 'DATA_DIR/brumadinho_historic.csv',
  expected_columns: ['date', 'displacement_mm', 'point_id', 'temporal_coherence', 'longitude', 'latitude', 'zone', 'source'],
  required_columns: ['date', 'displacement_mm'],
};

export function stressResult(intensity: number): StressTestResponse {
  const sim: DataSource = { product: 'stress_test', file: 'Sierrita_coupled.zarr', variable: 'regime', synthetic_or_mock: true, label: 'SIMULATED STRESS TEST (test overlay)' };
  return {
    facility_id: 'sierrita', simulated: true, label: 'SIMULATED STRESS TEST: synthetic perturbation of the cached products, not an observation',
    lat: 31.86, lon: -111.1,
    parameters: { intensity, delta_sai_max: 0.35 * intensity, footprint_radius_m: 90, creep_onset: '2023-09-15T00:00:00', creep_lead_days: 30 + 150 * (1 - intensity), creep_A_yr_mm: 0.03, d_crest_target_m: 5 },
    baseline: { sai: 0.08, regime: 1, regime_meaning: 'regime1-benign-consolidation', alert_tier: 0, kinematic_status: 'stable-below-2mm-yr', velocity_mm_yr: -0.5, failure_time_estimate: null },
    scenario: { sai: 0.08 + 0.35 * intensity, regime: intensity > 0.5 ? 4 : 1, regime_meaning: intensity > 0.5 ? 'regime4-active-instability-tier3-critical' : 'regime1-benign-consolidation', alert_tier: intensity > 0.5 ? 3 : 0, kinematic_status: intensity > 0.5 ? 'accelerating-forecast' : 'moving-linear', velocity_mm_yr: -100 * intensity, failure_time_estimate: null },
    times: DISP_TIMES,
    baseline_displacement_mm: [0, -0.1, -0.2],
    scenario_displacement_mm: [0, -2 * intensity, -10 * intensity],
    scenario_series: { ...KINEMATICS, source: sim, fukuzono: null },
    setback: { dates: DSWX_TIMES, baseline_d_crest_m: [150, 90], scenario_d_crest_m: [150, 90 - 85 * intensity], scenario_alerts: [[], intensity > 0.5 ? ['shrink_vs_baseline', 'crest_proximity'] : ['shrink_vs_baseline']] },
    baseline_hazard_level: 'CRITICAL_TIER3', scenario_hazard_level: 'CRITICAL_TIER3',
    baseline_regime_counts: { 'regime4-active-instability-tier3-critical': 16 },
    scenario_regime_counts: { 'regime4-active-instability-tier3-critical': intensity > 0.5 ? 25 : 16 },
    footprint: [[-111.101, 31.86], [-111.1, 31.861], [-111.099, 31.86], [-111.101, 31.86]],
    regime_matrix: matrix('stress_test_regime', MATRICES.anomaly_regime(null).values, { categorical: true, source: sim, note: 'SIMULATED' }),
  };
}

export const AUDIT_STATUS: AuditStatus = {
  llm_available: false, model: 'claude-opus-5-5', effort: 'high', auto_mode_uses: 'deterministic', modes: ['auto', 'deterministic'],
  standard: 'GISTM (Global Industry Standard on Tailings Management, 2020; co-convened by ICMM, UNEP and PRI)',
};

export const AUDIT: AuditResponse = {
  facility_id: 'sierrita',
  generated_by: 'deterministic-template',
  brief: {
    facility: 'Freeport-McMoRan Sierrita TSF', facility_id: 'sierrita', risk_classification: 'Tier-3 / Active Instability', hazard_level: 'CRITICAL_TIER3',
    executive_summary: 'Freeport-McMoRan Sierrita TSF: screening classification Tier-3 / Active Instability. 82 confident high-SAI pixels.',
    spectral_evidence: { summary: '82 confident pixels exceed the SAI threshold of 0.25.', facts: [{ label: 'Pixels with SAI above threshold (confident)', tool: 'get_spectral_alerts', path: 'counts.high_sai_pixels_confident', value: '82', unit: 'pixels' }] },
    kinematic_evidence: {
      summary: '40 accelerating pixels.', facts: [{ label: 'Accelerating pixels', tool: 'get_kinematics', path: 'accelerating_pixels', value: '40', unit: 'pixels' }],
      prediction_windows: [{ label: 'Trend-instability window, pixel 1', tool: 'get_kinematics', path: 'accelerating_pixels_by_earliest_date[0]', estimate: '2025-01-08', lower_95: '2024-12-29', upper_95: '2025-01-19' }],
    },
    hydrological_evidence: { summary: 'Latest valid setback 90.0 m.', facts: [{ label: 'Latest valid D_crest', tool: 'get_hydrological_setback', path: 'latest_valid.d_crest_m', value: '90.0', unit: 'm' }] },
    compliance_issues: [{ gistm_principle: 'Principle 7: design, implement and operate monitoring systems to manage risk', issue: 'Monitoring-based trigger and response actions should be reviewed.', evidence_paths: ['get_kinematics:accelerating_pixels'] }],
    recommendations: ['Verify the flagged REGIME_4 area with in-situ instrumentation.'],
    data_limitations: ['Some inputs are SYNTHETIC or MOCK data; this brief is not suitable for regulatory use.'],
    disclaimer: 'Satellite screening only.',
  },
  validation: { passed: true, facts_checked: 3, prose_numbers_checked: 5, prediction_windows_checked: 1, tools_used: ['dashboard_validation_query'] },
  tool_calls: [{ tool: 'dashboard_validation_query', input: { facility_id: 'sierrita' }, ok: true, error: null }],
  turns: 0,
  usage: {},
};

export interface StubOptions {
  facilitiesStatus?: number;
  available?: Record<string, string[]>;
  pixel?: PixelResponse;
  /** answer /pixel with the API's 404 "no data at this point" body */
  pixelUnavailable?: Record<string, string>;
  dashboard?: DashboardSummary | { status: number; detail: string };
  healthDown?: boolean;
  backtest?: BacktestResponse | 'missing' | { status: number; detail: string };
  stress?: { status: number; detail: string };
  audit?: AuditResponse | { status: number; detail: unknown };
  auditStatus?: AuditStatus;
  extraFacilities?: Facility[];
}

/** Install a fetch stub serving the fixtures; returns the mock (inspect `.mock.calls`). */
export function stubFetch(opts: StubOptions = {}) {
  const available = opts.available ?? { sierrita: Object.keys(MATRICES).concat('water_class'), mission: [] };
  const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
  const fn = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
    const url = new URL(String(input), 'http://localhost');
    const parts = url.pathname.split('/').filter(Boolean); // api, facilities, id?, sub?
    const facilities = [...FACILITIES, ...(opts.extraFacilities ?? [])];
    if (url.pathname === '/api/facilities') return opts.facilitiesStatus ? json({ detail: 'database offline' }, opts.facilitiesStatus) : json(facilities);
    if (url.pathname === '/api/health') return opts.healthDown ? json({ detail: 'down' }, 503) : json({ status: 'ok', database: 'x.duckdb', facilities: facilities.length, alerts: 1 });
    if (url.pathname === '/api/audit/status') return json(opts.auditStatus ?? AUDIT_STATUS);
    if (url.pathname === '/api/backtest/brumadinho') {
      const b = opts.backtest ?? backtest();
      if (b === 'missing') return json({ detail: BACKTEST_MISSING }, 404);
      if ('status' in b && typeof b.status === 'number') return json({ detail: (b as { detail: string }).detail }, b.status);
      const cutoff = Number(url.searchParams.get('cutoff_days') ?? 0);
      return json({ ...(b as BacktestResponse), cutoff_days: cutoff, point_id: url.searchParams.get('point') ?? (b as BacktestResponse).point_id });
    }
    const id = parts[2];
    const sub = parts[3];
    if (!facilities.some((f) => f.id === id)) return json({ detail: `unknown facility ${id}` }, 404);
    if (sub === 'stress-test') {
      if (opts.stress) return json({ detail: opts.stress.detail }, opts.stress.status);
      return json({ ...stressResult(Number(url.searchParams.get('intensity'))), lat: Number(url.searchParams.get('lat')), lon: Number(url.searchParams.get('lon')) });
    }
    if (sub === 'audit') {
      const a = opts.audit ?? AUDIT;
      return 'brief' in a ? json(a) : json({ detail: a.detail }, a.status);
    }
    if (sub === 'alerts') return json(id === 'sierrita' ? [SIERRITA_ALERT] : []);
    if (sub === 'layers') return json(layerInfo(available[id] ?? []));
    if (sub === 'matrices') {
      const layer = url.searchParams.get('layer')!;
      if (!(available[id] ?? []).includes(layer) || !MATRICES[layer]) return json({ detail: `${layer} not found` }, 404);
      return json(MATRICES[layer](url.searchParams.get('t')));
    }
    if (sub === 'pixel' && opts.pixelUnavailable)
      return json({ detail: { message: `No data for ${id} at this point`, unavailable: opts.pixelUnavailable } }, 404);
    if (sub === 'pixel') return json({ ...(opts.pixel ?? PIXEL), lat: Number(url.searchParams.get('lat')), lon: Number(url.searchParams.get('lon')) });
    if (sub === 'dashboard') {
      const d = opts.dashboard ?? DASHBOARD;
      return 'status' in d ? json({ detail: d.detail }, d.status) : json(d);
    }
    return json({ detail: 'not found' }, 404);
  });
  vi.stubGlobal('fetch', fn);
  return fn;
}

export function requestedUrls(fn: ReturnType<typeof stubFetch>): string[] {
  return fn.mock.calls.map(([u]) => String(u));
}
