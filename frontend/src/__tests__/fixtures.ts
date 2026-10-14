/**
 * Small API fixtures in the exact response shapes of backend/app/api/schemas.py, and a fetch stub
 * that routes /api/* requests to them. (The browser tests in e2e/ run against the real backend.)
 */
import { vi } from 'vitest';

import type { Alert, DashboardSummary, DataSource, Facility, FukuzonoFit, KinematicSeries, LayerInfo, Matrix, PixelResponse, Spectrum } from '@/utils/api';

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

export interface StubOptions {
  facilitiesStatus?: number;
  available?: Record<string, string[]>;
  pixel?: PixelResponse;
  /** answer /pixel with the API's 404 "no data at this point" body */
  pixelUnavailable?: Record<string, string>;
  dashboard?: DashboardSummary | { status: number; detail: string };
}

/** Install a fetch stub serving the fixtures; returns the mock (inspect `.mock.calls`). */
export function stubFetch(opts: StubOptions = {}) {
  const available = opts.available ?? { sierrita: Object.keys(MATRICES).concat('water_class'), mission: [] };
  const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
  const fn = vi.fn(async (input: RequestInfo | URL) => {
    const url = new URL(String(input), 'http://localhost');
    const parts = url.pathname.split('/').filter(Boolean); // api, facilities, id?, sub?
    if (url.pathname === '/api/facilities') return opts.facilitiesStatus ? json({ detail: 'database offline' }, opts.facilitiesStatus) : json(FACILITIES);
    const id = parts[2];
    const sub = parts[3];
    if (!FACILITIES.some((f) => f.id === id)) return json({ detail: `unknown facility ${id}` }, 404);
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
