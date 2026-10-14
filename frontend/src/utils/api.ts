/**
 * Typed client for the LithoCreep FastAPI backend (backend/app/api/schemas.py).
 *
 * The browser calls same-origin `/api/*`; next.config.js proxies it to the backend.
 */

export type HazardLevel = 'NONE' | 'WATCH_TIER1' | 'ALERT_TIER2' | 'CRITICAL_TIER3';

export interface Facility {
  id: string;
  name: string;
  country: string;
  latitude: number;
  longitude: number;
  bbox_xmin: number;
  bbox_ymin: number;
  bbox_xmax: number;
  bbox_ymax: number;
  mine_type: string;
  embankment_type: string;
  crest_line_geom: GeoJSON.LineString | GeoJSON.MultiLineString | null;
  status: string;
}

export interface Alert {
  id: number;
  facility_id: string;
  timestamp: string;
  hazard_level: HazardLevel;
  failure_date_est: string | null;
  failure_date_error_days: number | null;
  diagnostic_evidence: Record<string, unknown> & { contains_synthetic_or_mock_inputs?: boolean };
}

export interface DataSource {
  product: string;
  file: string;
  variable: string;
  synthetic_or_mock: boolean;
  label: string;
}

export interface LayerInfo {
  layer: string;
  units: string;
  categorical: boolean;
  time_dependent: boolean;
  available: boolean;
  source: DataSource | null;
  times: string[];
  detail: string | null;
}

export interface Matrix {
  facility_id: string;
  layer: string;
  units: string;
  categorical: boolean;
  crs: 'EPSG:4326';
  /** [xmin, ymin, xmax, ymax] = facility bbox */
  bounds: [number, number, number, number];
  /** [rows, cols]; row 0 is the northern edge */
  shape: [number, number];
  resolution_deg: [number, number];
  time: string | null;
  requested_time: string | null;
  available_times: string[];
  values: (number | null)[][];
  valid_fraction: number;
  flag_meanings: string | null;
  source: DataSource;
  note: string | null;
}

export interface PointMatch {
  method: 'exact' | 'bilinear' | 'nearest';
  nearest_cell_distance_m: number | null;
  note: string | null;
}

export interface PixelResponse {
  facility_id: string;
  lat: number;
  lon: number;
  utm_x: number;
  utm_y: number;
  utm_crs: string;
  kinematics: {
    source: DataSource;
    match: PointMatch;
    times: string[];
    displacement_mm: (number | null)[];
    velocity_mm_yr?: (number | null)[] | null;
    temporal_coherence: number | null;
    status: string | null;
    instability_time: string | null;
    instability_time_lower: string | null;
    instability_time_upper: string | null;
    disclaimer: string | null;
  } | null;
  spectral: {
    source: DataSource;
    match: PointMatch;
    sai: number | null;
    rmse: number | null;
    ndvi: number | null;
    confidence: number | null;
    observability: string | null;
    abundance: Record<string, number | null>;
  } | null;
  hydrology: {
    source: DataSource;
    match: PointMatch;
    elevation_m: number | null;
    slope_deg: number | null;
    aspect_deg: number | null;
    flow_accumulation_cells: number | null;
    stream: boolean | null;
  } | null;
  classification: {
    source: DataSource;
    match: PointMatch;
    regime: number;
    regime_meaning: string;
    alert_tier: number;
    flags: string[];
    low_confidence: boolean;
    sai_interval: (number | null)[];
    velocity_interval_mm_yr: (number | null)[];
    failure_time_estimate: string | null;
    failure_time_lower: string | null;
    failure_time_upper: string | null;
  } | null;
  unavailable: Record<string, string>;
}

export class ApiError extends Error {
  constructor(public status: number, public detail: string, public path: string) {
    super(`${status} ${path}: ${detail}`);
    this.name = 'ApiError';
  }
}

const BASE = (process.env.NEXT_PUBLIC_API_BASE || '').replace(/\/$/, '');

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${BASE}${path}`, { signal, headers: { Accept: 'application/json' } });
  } catch (err) {
    if ((err as Error).name === 'AbortError') throw err;
    throw new ApiError(0, `backend unreachable (${(err as Error).message})`, path);
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail ?? body);
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, detail, path);
  }
  return (await res.json()) as T;
}

const enc = encodeURIComponent;

export const api = {
  facilities: (signal?: AbortSignal) => getJson<Facility[]>('/api/facilities', signal),
  alerts: (id: string, signal?: AbortSignal) => getJson<Alert[]>(`/api/facilities/${enc(id)}/alerts`, signal),
  layers: (id: string, signal?: AbortSignal) => getJson<LayerInfo[]>(`/api/facilities/${enc(id)}/layers`, signal),
  matrix: (id: string, layer: string, t?: string | null, signal?: AbortSignal) =>
    getJson<Matrix>(`/api/facilities/${enc(id)}/matrices?layer=${enc(layer)}${t ? `&t=${enc(t)}` : ''}`, signal),
  pixel: (id: string, lat: number, lon: number, signal?: AbortSignal) =>
    getJson<PixelResponse>(`/api/facilities/${enc(id)}/pixel?lat=${lat}&lon=${lon}`, signal),
};
