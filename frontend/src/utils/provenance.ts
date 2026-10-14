/** Data-provenance verdict for the HUD watermark: authentic observations vs simulated data. */
import type { Alert, DataSource, LayerInfo } from './api';

export type ProvenanceState = 'authentic' | 'simulated' | 'none';

export interface Provenance {
  state: ProvenanceState;
  headline: string;
  /** One line per distinct source shown. */
  sources: { label: string; synthetic: boolean; products: string[] }[];
}

/**
 * Conservative: anything synthetic/mock in the shown matrices, or in the alert that coloured the
 * facility, makes the whole view "simulated". Only when every shown source is a real
 * observation is the view labelled authentic.
 */
export function provenance(shown: DataSource[], alert: Alert | null | undefined): Provenance {
  const bySource = new Map<string, { label: string; synthetic: boolean; products: Set<string> }>();
  for (const s of shown) {
    const key = `${s.synthetic_or_mock}|${s.label}`;
    const entry = bySource.get(key) ?? { label: s.label, synthetic: s.synthetic_or_mock, products: new Set<string>() };
    entry.products.add(s.product);
    bySource.set(key, entry);
  }
  const alertSynthetic = alert?.diagnostic_evidence?.contains_synthetic_or_mock_inputs === true;
  if (alertSynthetic) {
    const key = 'true|alert';
    if (!bySource.has(key)) bySource.set(key, { label: 'Facility alert built from synthetic/mock inputs', synthetic: true, products: new Set(['alert']) });
  }
  const sources = [...bySource.values()].map((s) => ({ label: s.label, synthetic: s.synthetic, products: [...s.products].sort() }));
  if (!shown.length) {
    return {
      state: alertSynthetic ? 'simulated' : 'none',
      headline: alertSynthetic ? 'SIMULATED DATASET' : 'NO MATRIX DATA LOADED',
      sources,
    };
  }
  const anySynthetic = sources.some((s) => s.synthetic);
  return {
    state: anySynthetic ? 'simulated' : 'authentic',
    headline: anySynthetic ? 'SIMULATED DATASET' : 'AUTHENTIC SATELLITE OBSERVATION',
    sources,
  };
}

/** Distinct OPERA epochs across time-dependent layers, tagged by product. */
export interface Epoch {
  time: string;
  products: string[];
}

const EPOCH_PRODUCT: Record<string, string> = {
  displacement: 'DISP-S1',
  velocity: 'DISP-S1',
  acceleration: 'DISP-S1',
  water_class: 'DSWx',
  hydraulic_distance: 'DSWx',
};

export function operaEpochs(layers: LayerInfo[]): Epoch[] {
  const map = new Map<string, Set<string>>();
  for (const l of layers) {
    if (!l.available || !l.time_dependent) continue;
    const product = EPOCH_PRODUCT[l.layer] ?? l.layer;
    for (const t of l.times) {
      const key = normaliseTime(t);
      if (!map.has(key)) map.set(key, new Set());
      map.get(key)!.add(product);
    }
  }
  return [...map.entries()]
    .sort(([a], [b]) => Date.parse(a) - Date.parse(b))
    .map(([time, products]) => ({ time, products: [...products].sort() }));
}

/** Backend datetimes are naive UTC; make them explicit so Date parsing is timezone-independent. */
export function normaliseTime(t: string): string {
  return /[zZ]|[+-]\d\d:?\d\d$/.test(t) ? t : `${t}Z`;
}

export function formatDate(t: string | null | undefined): string {
  if (!t) return 'n/a';
  const d = new Date(normaliseTime(t));
  return Number.isNaN(d.getTime()) ? t : d.toISOString().slice(0, 10);
}
