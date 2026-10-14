/**
 * Data preparation for the Double Inspector charts (pure functions of API responses).
 */
import type { FukuzonoFit, KinematicSeries, SetbackEpoch, SetbackSummary, Spectrum } from './api';
import { normaliseTime } from './provenance';

/** Chart colours: dataviz reference palette, dark steps (validated on the #0f172a chart surface). */
export const VIZ = {
  series1: '#3987e5', // observed / measured
  series2: '#d95926', // model / fit
  grid: '#334155',
  axis: '#94a3b8',
  text: '#e2e8f0',
  muted: '#64748b',
  excluded: '#475569',
};

/** Status palette (reserved: always shown with an icon and a label). */
export const STATUS = {
  good: { color: '#0ca30c', icon: '✓', label: 'OK' },
  warning: { color: '#fab219', icon: '!', label: 'Warning' },
  serious: { color: '#ec835a', icon: '▲', label: 'Alert' },
  critical: { color: '#d03b3b', icon: '✕', label: 'Critical' },
  unknown: { color: '#94a3b8', icon: '?', label: 'Unknown' },
} as const;
export type StatusLevel = keyof typeof STATUS;

export const ms = (t: string) => Date.parse(normaliseTime(t));

const finite = (v: number | null | undefined): v is number => typeof v === 'number' && Number.isFinite(v);

// --------------------------------------------------------------------------- //
// Spectroscopy
// --------------------------------------------------------------------------- //

export interface SpectrumRow {
  wl: number;
  /** null on excluded (water-vapour) channels so the line breaks there. */
  measured: number | null;
  modelled: number | null;
}

export function spectrumRows(s: Spectrum): SpectrumRow[] {
  return s.wavelengths_nm.map((wl, i) => ({
    wl,
    measured: s.active[i] && finite(s.reflectance[i]) ? s.reflectance[i] : null,
    modelled: s.active[i] && finite(s.modelled[i]) ? s.modelled[i] : null,
  }));
}

export interface AbundanceGroup {
  key: string;
  label: string;
  value: number | null;
  members: string[];
}

/** Endmembers grouped for display; "Smectite" is the montmorillonite endmember. */
export const ABUNDANCE_GROUPS: { key: string; label: string; members: string[] }[] = [
  { key: 'jarosite', label: 'Jarosite', members: ['Jarosite'] },
  { key: 'smectite', label: 'Smectite (montmorillonite)', members: ['Montmorillonite'] },
  { key: 'goethite', label: 'Goethite', members: ['Goethite'] },
  { key: 'other', label: 'Other alteration (ferrihydrite, gypsum)', members: ['Ferrihydrite', 'Gypsum'] },
  { key: 'background', label: 'Background (quartz-feldspar, water/shade)', members: ['Quartz-Feldspar', 'Water-Shade'] },
];

export function abundanceGroups(abundance: Record<string, number | null>): AbundanceGroup[] {
  return ABUNDANCE_GROUPS.map((g) => {
    const vals = g.members.map((m) => abundance[m]);
    const known = vals.filter(finite);
    return { ...g, value: known.length ? known.reduce((a, b) => a + b, 0) : null };
  });
}

export const RMSE_THRESHOLD = 0.05;
export const SAI_THRESHOLD = 0.25;

export function rmseLevel(rmse: number | null | undefined): StatusLevel {
  if (!finite(rmse)) return 'unknown';
  return rmse > RMSE_THRESHOLD ? 'warning' : 'good';
}

export function saiLevel(sai: number | null | undefined): StatusLevel {
  if (!finite(sai)) return 'unknown';
  return sai > SAI_THRESHOLD ? 'serious' : 'good';
}

// --------------------------------------------------------------------------- //
// Kinematics
// --------------------------------------------------------------------------- //

export type Band = [number, number] | null;

export interface KinematicRow {
  t: number;
  observed: number | null;
  smooth: number | null;
  smoothBand: Band;
  velocity: number | null;
  velocityBand: Band;
  acceleration: number | null;
  accelerationBand: Band;
}

const band = (lo: (number | null)[] | null | undefined, hi: (number | null)[] | null | undefined, i: number): Band =>
  lo && hi && finite(lo[i]) && finite(hi[i]) ? [lo[i] as number, hi[i] as number] : null;

const at = (s: (number | null)[] | null | undefined, i: number) => (s && finite(s[i]) ? (s[i] as number) : null);

export function kinematicRows(k: KinematicSeries): KinematicRow[] {
  return k.times.map((t, i) => ({
    t: ms(t),
    observed: at(k.displacement_mm, i),
    smooth: at(k.displacement_smooth_mm, i),
    smoothBand: band(k.displacement_smooth_lower_mm, k.displacement_smooth_upper_mm, i),
    velocity: at(k.velocity_mm_yr, i),
    velocityBand: band(k.velocity_lower_mm_yr, k.velocity_upper_mm_yr, i),
    acceleration: at(k.acceleration_mm_yr2, i),
    accelerationBand: band(k.acceleration_lower_mm_yr2, k.acceleration_upper_mm_yr2, i),
  }));
}

export const hasSeries = (rows: KinematicRow[], key: 'smooth' | 'velocity' | 'acceleration') => rows.some((r) => r[key] !== null);

export interface CoherenceCheck {
  gamma: number | null;
  threshold: number;
  low: boolean;
  message: string | null;
}

export function coherenceCheck(k: KinematicSeries): CoherenceCheck {
  const threshold = k.coherence_threshold ?? 0.65;
  const gamma = finite(k.temporal_coherence) ? k.temporal_coherence : null;
  const low = (gamma !== null && gamma < threshold) || k.status === 'unobservable-low-coherence';
  return {
    gamma,
    threshold,
    low,
    message: low
      ? `Low temporal coherence (γ = ${gamma === null ? 'unknown' : gamma.toFixed(2)} < ${threshold}): the GP fit is unreliable and no trend is modelled for this cell.`
      : null,
  };
}

export interface FukuzonoRow {
  t: number;
  /** observed Lambda = 1/|v| (yr/mm) at the epochs used in the fit */
  lambda: number | null;
  /** observed Lambda at earlier epochs, shown as context */
  context: number | null;
  fit: number | null;
  fitBand: Band;
}

/**
 * Inverse velocity for the Fukuzono chart: the fit-window epochs, as many earlier epochs for
 * context, and the regression line with its band out past the latest crossing bound.
 */
export function fukuzonoRows(k: KinematicSeries, fit: FukuzonoFit): FukuzonoRow[] {
  const rows = new Map<number, FukuzonoRow>();
  const get = (t: number) => {
    let r = rows.get(t);
    if (!r) rows.set(t, (r = { t, lambda: null, context: null, fit: null, fitBand: null }));
    return r;
  };
  const windowStart = ms(fit.window_times[0]);
  const contextStart = windowStart - (ms(fit.t0) - windowStart);
  (k.inverse_velocity_yr_mm ?? []).forEach((v, i) => {
    const t = ms(k.times[i]);
    if (finite(v) && t >= contextStart && t < windowStart) get(t).context = v;
  });
  fit.window_times.forEach((t, i) => {
    const v = fit.inverse_velocity_yr_mm[i];
    if (finite(v)) get(ms(t)).lambda = v;
  });
  fit.line_times.forEach((t, i) => {
    const r = get(ms(t));
    r.fit = fit.line_yr_mm[i];
    r.fitBand = [fit.line_lower_yr_mm[i], fit.line_upper_yr_mm[i]];
  });
  return [...rows.values()].sort((a, b) => a.t - b.t);
}

const STATUS_TEXT: Record<string, string> = {
  'stable-below-2mm-yr': 'Stable (below 2 mm/yr): no trend modelled.',
  'moving-linear': 'Moving at a steady rate: no acceleration, so no inverse-velocity trend.',
  'accelerating-no-converging-trend': 'Accelerating, but inverse velocity is not converging towards zero: no timeline estimated.',
  'unobservable-low-coherence': 'Decorrelated cell: not modelled.',
  'no-data': 'No displacement data at this cell.',
};

export const kinematicStatusText = (status: string | null) => (status ? STATUS_TEXT[status] ?? status : 'Unknown status');

// --------------------------------------------------------------------------- //
// Hydrology
// --------------------------------------------------------------------------- //

export const ALERT_TEXT: Record<string, string> = {
  shrink_vs_baseline: 'Setback shrank more than 30 m since the first epoch',
  shrink_vs_previous: 'Setback shrank more than 30 m since the previous epoch',
  crest_proximity: 'Pond within 15 m of the crest',
};

/** Alert level of one D_crest epoch: proximity is critical, shrink alerts are serious. */
export function setbackLevel(e: SetbackEpoch): StatusLevel {
  if (e.status !== 'valid' || !finite(e.d_crest_m)) return 'unknown';
  if (e.alerts.includes('crest_proximity')) return 'critical';
  if (e.alerts.length) return 'serious';
  return 'good';
}

export interface SetbackRow {
  t: number;
  date: string;
  dCrest: number | null;
  level: StatusLevel;
  epoch: SetbackEpoch;
}

export function setbackRows(s: SetbackSummary): SetbackRow[] {
  return (s.series ?? []).map((e) => ({ t: ms(e.date), date: e.date, dCrest: finite(e.d_crest_m) ? e.d_crest_m : null, level: setbackLevel(e), epoch: e }));
}

export interface ComplianceCheck {
  id: string;
  principle: string;
  check: string;
  level: StatusLevel;
  detail: string;
}

/**
 * Screening checks on the pond-to-crest setback, mapped to GISTM principles for oversight.
 * They flag items for a qualified reviewer; they are not a compliance determination.
 */
export function setbackChecks(s: SetbackSummary): ComplianceCheck[] {
  const thr = s.thresholds ?? { proximity_alert_m: 15, shrink_alert_m: -30 };
  if (!s.available) {
    return [
      {
        id: 'setback-data',
        principle: 'Principle 7 (monitoring)',
        check: 'Pond-to-crest setback is monitored',
        level: 'unknown',
        detail: s.reason ?? 'No setback product for this facility.',
      },
    ];
  }
  const latest = s.latest_valid ?? null;
  const checks: ComplianceCheck[] = [];
  checks.push(
    latest && finite(latest.d_crest_m)
      ? {
          id: 'proximity',
          principle: 'Principle 6 (beach width / freeboard)',
          check: `Latest setback at least ${thr.proximity_alert_m} m`,
          level: latest.d_crest_m < thr.proximity_alert_m ? 'critical' : 'good',
          detail: `${latest.d_crest_m.toFixed(1)} m on ${latest.date}`,
        }
      : { id: 'proximity', principle: 'Principle 6 (beach width / freeboard)', check: `Latest setback at least ${thr.proximity_alert_m} m`, level: 'unknown', detail: 'No valid epoch.' },
  );
  const base = latest?.delta_vs_baseline_m;
  checks.push({
    id: 'shrink-baseline',
    principle: 'Principle 6 (pond operation)',
    check: `No setback loss beyond ${Math.abs(thr.shrink_alert_m)} m since the first epoch`,
    level: !finite(base) ? 'unknown' : base < thr.shrink_alert_m ? 'serious' : 'good',
    detail: finite(base) ? `${base >= 0 ? '+' : ''}${base.toFixed(1)} m` : 'n/a',
  });
  const prev = latest?.delta_vs_previous_m;
  checks.push({
    id: 'shrink-previous',
    principle: 'Principle 6 (pond operation)',
    check: `No setback loss beyond ${Math.abs(thr.shrink_alert_m)} m between epochs`,
    level: !finite(prev) ? 'unknown' : prev < thr.shrink_alert_m ? 'serious' : 'good',
    detail: finite(prev) ? `${prev >= 0 ? '+' : ''}${prev.toFixed(1)} m` : 'only one epoch',
  });
  const valid = s.n_valid_epochs ?? 0;
  const total = s.n_epochs ?? 0;
  checks.push({
    id: 'coverage',
    principle: 'Principle 7 (monitoring)',
    check: 'Every DSWx epoch yields a valid setback',
    level: total === 0 ? 'unknown' : valid === total ? 'good' : 'warning',
    detail: `${valid} of ${total} epochs valid`,
  });
  checks.push({
    id: 'crest',
    principle: 'Principle 2 (knowledge base)',
    check: 'Crest geometry on record',
    level: s.crest_line ? 'good' : 'warning',
    detail: s.crest_line ? 'crest line supplied to the hydrology run' : 'no crest line: setback cannot be measured',
  });
  return checks;
}
