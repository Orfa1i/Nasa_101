/**
 * Small API fixtures in the exact response shapes of backend/app/api/schemas.py, and a fetch stub
 * that routes /api/* requests to them. (The browser tests in e2e/ run against the real backend.)
 */
import { vi } from 'vitest';

import type { Alert, DataSource, Facility, LayerInfo, Matrix, PixelResponse } from '@/utils/api';

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

export const PIXEL: PixelResponse = {
  facility_id: 'sierrita', lat: 31.89, lon: -111.11, utm_x: 0, utm_y: 0, utm_crs: 'EPSG:32612',
  kinematics: {
    source: REAL_DISP, match: { method: 'bilinear', nearest_cell_distance_m: 10, note: null }, times: DISP_TIMES,
    displacement_mm: [-1, -2, -3], temporal_coherence: 0.91, status: 'accelerating-forecast',
    instability_time: '2025-01-08T00:00:00', instability_time_lower: '2024-12-29T00:00:00', instability_time_upper: '2025-01-19T00:00:00', disclaimer: 'not a forecast',
  },
  spectral: { source: SYNTH, match: { method: 'bilinear', nearest_cell_distance_m: 10, note: null }, sai: 0.48, rmse: 0.0123, ndvi: 0.1, confidence: 0.9, observability: 'observable', abundance: {} },
  hydrology: null,
  classification: {
    source: SYNTH, match: { method: 'nearest', nearest_cell_distance_m: 10, note: null }, regime: 4, regime_meaning: 'regime4-active-instability-tier3-critical',
    alert_tier: 3, flags: [], low_confidence: false, sai_interval: [0.4, 0.55], velocity_interval_mm_yr: [-30, -20],
    failure_time_estimate: '2025-01-13T00:00:00', failure_time_lower: '2024-12-31T00:00:00', failure_time_upper: '2025-01-27T00:00:00',
  },
  unavailable: { hydrology: 'Sierrita_hydrology.zarr not found' },
};

export interface StubOptions {
  facilitiesStatus?: number;
  available?: Record<string, string[]>;
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
    if (sub === 'pixel') return json({ ...PIXEL, lat: Number(url.searchParams.get('lat')), lon: Number(url.searchParams.get('lon')) });
    return json({ detail: 'not found' }, 404);
  });
  vi.stubGlobal('fetch', fn);
  return fn;
}

export function requestedUrls(fn: ReturnType<typeof stubFetch>): string[] {
  return fn.mock.calls.map(([u]) => String(u));
}
