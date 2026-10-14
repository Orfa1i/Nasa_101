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

export interface Spectrum {
  match: PointMatch;
  wavelengths_nm: number[];
  /** All 285 bands, including the excluded water-vapour channels. */
  reflectance: (number | null)[];
  /** False for channels excluded from the NNLS fit. */
  active: boolean[];
  /** NNLS reconstruction M @ f; null on excluded channels. */
  modelled: (number | null)[];
  excluded_windows_nm: [number, number][];
  abundance: Record<string, number | null>;
  rmse: number | null;
  sai: number | null;
  endmember_titles: Record<string, string>;
}

export interface FukuzonoFit {
  window_times: string[];
  inverse_velocity_yr_mm: (number | null)[];
  inverse_velocity_lower_yr_mm: (number | null)[];
  inverse_velocity_upper_yr_mm: (number | null)[];
  t0: string;
  lambda_0_yr_mm: number;
  slope_yr_mm_per_yr: number;
  slope_std: number;
  slope_significant: boolean;
  line_times: string[];
  line_yr_mm: number[];
  line_lower_yr_mm: number[];
  line_upper_yr_mm: number[];
  crossing_estimate: string | null;
  crossing_lower: string | null;
  crossing_upper: string | null;
  label: string;
}

type Series = (number | null)[];

export interface KinematicSeries {
  source: DataSource;
  match: PointMatch;
  point_id?: string | null;
  times: string[];
  displacement_mm: Series;
  displacement_smooth_mm?: Series | null;
  displacement_smooth_lower_mm?: Series | null;
  displacement_smooth_upper_mm?: Series | null;
  velocity_mm_yr?: Series | null;
  velocity_lower_mm_yr?: Series | null;
  velocity_upper_mm_yr?: Series | null;
  acceleration_mm_yr2?: Series | null;
  acceleration_lower_mm_yr2?: Series | null;
  acceleration_upper_mm_yr2?: Series | null;
  inverse_velocity_yr_mm?: Series | null;
  temporal_coherence: number | null;
  coherence_threshold?: number;
  status: string | null;
  instability_time: string | null;
  instability_time_lower: string | null;
  instability_time_upper: string | null;
  disclaimer: string | null;
  fukuzono?: FukuzonoFit | null;
}

export interface SetbackEpoch {
  date: string;
  d_crest_m: number | null;
  status: string;
  delta_vs_baseline_m: number | null;
  delta_vs_previous_m: number | null;
  alerts: string[];
  observed_fraction_pct: number | null;
  water_area_m2: number | null;
}

export interface SetbackSummary {
  facility_id: string;
  available: boolean;
  reason?: string;
  source_file?: string;
  crest_line?: string | null;
  thresholds?: { shrink_alert_m: number; proximity_alert_m: number };
  n_epochs?: number;
  n_valid_epochs?: number;
  latest_valid?: SetbackEpoch | null;
  min_valid_d_crest_m?: number | null;
  encroaching?: boolean;
  series?: SetbackEpoch[];
}

export interface GistmIssue {
  gistm_principle: string;
  issue: string;
  evidence_paths: string[];
}

/** GET /api/facilities/{id}/dashboard (Phase 7 inspection tools); only the fields the UI reads. */
export interface DashboardSummary {
  facility_id: string;
  risk_classification: string;
  hydrological_setback: SetbackSummary;
  consistency_checks: { check: string; passed: boolean; detail: string }[];
  all_checks_passed: boolean;
  gistm_screening: { candidate_issues: GistmIssue[]; recommendations: string[]; data_limitations: string[] };
}

export interface PixelResponse {
  facility_id: string;
  lat: number;
  lon: number;
  utm_x: number;
  utm_y: number;
  utm_crs: string;
  kinematics: KinematicSeries | null;
  spectral: {
    source: DataSource;
    match: PointMatch;
    sai: number | null;
    rmse: number | null;
    ndvi: number | null;
    confidence: number | null;
    observability: string | null;
    abundance: Record<string, number | null>;
    spectrum?: Spectrum | null;
    spectrum_unavailable?: string | null;
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
  constructor(public status: number, public detail: string, public path: string, public body?: unknown) {
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
    let parsed: unknown;
    try {
      const body = await res.json();
      parsed = body.detail ?? body;
      detail = typeof body.detail === 'string' ? body.detail : typeof body.detail?.message === 'string' ? body.detail.message : JSON.stringify(parsed);
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, detail, path, parsed);
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
  dashboard: (id: string, signal?: AbortSignal) => getJson<DashboardSummary>(`/api/facilities/${enc(id)}/dashboard`, signal),
  pixel: (id: string, lat: number, lon: number, signal?: AbortSignal) =>
    getJson<PixelResponse>(`/api/facilities/${enc(id)}/pixel?lat=${lat}&lon=${lon}`, signal),
};
