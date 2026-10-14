/**
 * Raster overlays: which backend matrices the map can show, how they are coloured,
 * and which pixels are masked as unobservable.
 *
 * Matrices come from GET /api/facilities/{id}/matrices on an EPSG:4326 grid whose
 * bounds equal the facility bbox (row 0 = north), so they align with each other and
 * with the observability mask cell for cell.
 */
import type { Matrix } from './api';
import { RAMPS, rampColor, type RGB, type RGBA } from './colormap';

export type OverlayId = 'sai' | 'displacement' | 'flow_accumulation' | 'anomaly_regime' | 'spectral_rmse' | 'coherence';

/** observability_mask codes (backend/app/api/layers.py). */
export const MASK_VEGETATED = 1;
export const MASK_DECORRELATED = 2;
export const MASK_BOTH = 3;

export interface ContinuousScale {
  kind: 'continuous';
  ramp: RGB[];
  /** Fixed domain, or 'symmetric' to use +/- the 98th percentile of |value| (min `minAbs`). */
  domain: [number, number] | 'symmetric';
  minAbs?: number;
  transform?: 'log10';
  /** Values below this are drawn transparent (e.g. tiny flow accumulations). */
  hideBelow?: number;
  /** Alpha ramps in from `fadeFrom` to `fadeTo` (raw units), so background reads as context. */
  fadeFrom?: number;
  fadeTo?: number;
  threshold?: { value: number; label: string };
}

export interface CategoricalScale {
  kind: 'categorical';
  classes: { value: number; label: string; color: RGBA }[];
}

export interface OverlayDef {
  id: OverlayId;
  /** Backend layer name for /matrices?layer= */
  layer: string;
  label: string;
  group: 'screening' | 'quality';
  description: string;
  /** Mask codes that make a pixel transparent for this overlay. */
  maskCodes: number[];
  scale: ContinuousScale | CategoricalScale;
  decimals: number;
}

export const REGIME_CLASSES: CategoricalScale['classes'] = [
  { value: 0, label: 'Unclassified / rejected', color: [148, 163, 184, 140] },
  { value: 1, label: 'Regime 1: benign consolidation', color: [34, 197, 94, 170] },
  { value: 2, label: 'Regime 2: unobservable / anthropogenic', color: [139, 92, 246, 170] },
  { value: 3, label: 'Regime 3: legacy seepage (Tier-1 watch)', color: [250, 204, 21, 220] },
  { value: 4, label: 'Regime 4: active instability (Tier-3)', color: [239, 68, 68, 240] },
  { value: 5, label: 'Kinematic anomaly, low SAI', color: [249, 115, 22, 220] },
];

export const OVERLAYS: OverlayDef[] = [
  {
    id: 'sai',
    layer: 'sai',
    label: 'SAI seepage',
    group: 'screening',
    description: 'Seepage Alteration Index from EMIT unmixing; 0.25 is the screening threshold.',
    maskCodes: [MASK_VEGETATED, MASK_BOTH],
    scale: { kind: 'continuous', ramp: RAMPS.ylOrRd, domain: [0, 0.6], fadeFrom: 0.05, fadeTo: 0.25, threshold: { value: 0.25, label: 'SAI 0.25' } },
    decimals: 3,
  },
  {
    id: 'displacement',
    layer: 'displacement',
    label: 'InSAR cumulative displacement',
    group: 'screening',
    description: 'OPERA DISP-S1 line-of-sight displacement since the reference epoch (GP-smoothed where available).',
    maskCodes: [MASK_DECORRELATED, MASK_BOTH],
    scale: { kind: 'continuous', ramp: RAMPS.rdBu, domain: 'symmetric', minAbs: 5 },
    decimals: 1,
  },
  {
    id: 'flow_accumulation',
    layer: 'flow_accumulation',
    label: 'Flow paths',
    group: 'screening',
    description: 'D-infinity flow accumulation from GLO-30 (cells draining through each cell, log scale).',
    maskCodes: [],
    scale: { kind: 'continuous', ramp: RAMPS.blues, domain: [1, 4], transform: 'log10', hideBelow: 10 },
    decimals: 0,
  },
  {
    id: 'anomaly_regime',
    layer: 'anomaly_regime',
    label: 'Anomaly regimes',
    group: 'screening',
    description: 'Phase 5 co-anomaly classification (spectral x kinematic x hydrological).',
    maskCodes: [],
    scale: { kind: 'categorical', classes: REGIME_CLASSES },
    decimals: 0,
  },
  {
    id: 'spectral_rmse',
    layer: 'spectral_rmse',
    label: 'Unmixing error (RMSE)',
    group: 'quality',
    description: 'NNLS reconstruction RMSE in reflectance; above 0.05 the unmixing is low confidence.',
    maskCodes: [],
    scale: { kind: 'continuous', ramp: RAMPS.magma, domain: [0, 0.1], threshold: { value: 0.05, label: 'RMSE 0.05' } },
    decimals: 4,
  },
  {
    id: 'coherence',
    layer: 'coherence',
    label: 'Radar temporal coherence',
    group: 'quality',
    description: 'DISP-S1 temporal coherence; below 0.65 the pixel is decorrelated and never forecast.',
    maskCodes: [],
    scale: { kind: 'continuous', ramp: RAMPS.viridis, domain: [0, 1], threshold: { value: 0.65, label: 'coherence 0.65' } },
    decimals: 2,
  },
];

export const OVERLAY_BY_ID = Object.fromEntries(OVERLAYS.map((o) => [o.id, o])) as Record<OverlayId, OverlayDef>;

// --------------------------------------------------------------------------- //
// Statistics and lookup
// --------------------------------------------------------------------------- //

export function finiteValues(m: Matrix): number[] {
  const out: number[] = [];
  for (const row of m.values) for (const v of row) if (v !== null && Number.isFinite(v)) out.push(v);
  return out;
}

export function quantile(sorted: number[], q: number): number | null {
  if (!sorted.length) return null;
  const pos = (sorted.length - 1) * q;
  const lo = Math.floor(pos);
  const hi = Math.ceil(pos);
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo);
}

export interface MatrixStats {
  count: number;
  min: number | null;
  max: number | null;
  median: number | null;
  p05: number | null;
  p95: number | null;
}

export function matrixStats(m: Matrix): MatrixStats {
  const v = finiteValues(m).sort((a, b) => a - b);
  return { count: v.length, min: v[0] ?? null, max: v[v.length - 1] ?? null, median: quantile(v, 0.5), p05: quantile(v, 0.05), p95: quantile(v, 0.95) };
}

/** Fraction of valid cells with value above (`above` = true) or below the threshold. */
export function fractionBeyond(m: Matrix, threshold: number, above: boolean): number | null {
  const v = finiteValues(m);
  if (!v.length) return null;
  return v.filter((x) => (above ? x > threshold : x < threshold)).length / v.length;
}

/** Cell (row, col) containing lon/lat, or null outside the matrix bounds. */
export function cellAt(m: Matrix, lon: number, lat: number): [number, number] | null {
  const [xmin, ymin, xmax, ymax] = m.bounds;
  if (lon < xmin || lon > xmax || lat < ymin || lat > ymax) return null;
  const [rows, cols] = m.shape;
  const row = Math.min(rows - 1, Math.floor(((ymax - lat) / (ymax - ymin)) * rows));
  const col = Math.min(cols - 1, Math.floor(((lon - xmin) / (xmax - xmin)) * cols));
  return [row, col];
}

export function valueAt(m: Matrix, lon: number, lat: number): number | null {
  const cell = cellAt(m, lon, lat);
  return cell ? m.values[cell[0]][cell[1]] : null;
}

export function sameGrid(a: Matrix, b: Matrix): boolean {
  return a.shape[0] === b.shape[0] && a.shape[1] === b.shape[1] && a.bounds.every((v, i) => Math.abs(v - b.bounds[i]) < 1e-9);
}

// --------------------------------------------------------------------------- //
// Rendering to RGBA
// --------------------------------------------------------------------------- //

export interface RasterImage {
  width: number;
  height: number;
  data: Uint8ClampedArray;
}

export interface ResolvedDomain {
  domain: [number, number];
  transform?: 'log10';
}

export function resolveDomain(scale: ContinuousScale, m: Matrix | null): ResolvedDomain {
  if (scale.domain !== 'symmetric') return { domain: scale.domain, transform: scale.transform };
  const abs = m ? finiteValues(m).map(Math.abs).sort((a, b) => a - b) : [];
  const hi = Math.max(scale.minAbs ?? 1, quantile(abs, 0.98) ?? 0);
  return { domain: [-hi, hi], transform: scale.transform };
}

export function colorFor(def: OverlayDef, value: number, resolved: ResolvedDomain | null): RGBA | null {
  const scale = def.scale;
  if (scale.kind === 'categorical') {
    const cls = scale.classes.find((c) => c.value === Math.round(value));
    return cls ? cls.color : null;
  }
  if (scale.hideBelow !== undefined && value < scale.hideBelow) return null;
  const { domain, transform } = resolved ?? resolveDomain(scale, null);
  const x = transform === 'log10' ? Math.log10(Math.max(value, 1e-12)) : value;
  const [rgbR, rgbG, rgbB] = rampColor(scale.ramp, (x - domain[0]) / (domain[1] - domain[0]));
  let alpha = 220;
  if (scale.fadeFrom !== undefined && scale.fadeTo !== undefined) {
    const f = (value - scale.fadeFrom) / (scale.fadeTo - scale.fadeFrom);
    alpha = Math.round(40 + 180 * Math.min(1, Math.max(0, f)));
  }
  return [rgbR, rgbG, rgbB, alpha];
}

export interface RenderResult extends RasterImage {
  resolved: ResolvedDomain | null;
  /** Cells drawn transparent because of the observability mask. */
  maskedCells: number;
  drawnCells: number;
}

/**
 * Colour a matrix into an RGBA image (row 0 = north = top of the image). Null cells and cells
 * whose observability-mask code is in `def.maskCodes` are fully transparent.
 */
export function renderMatrix(m: Matrix, def: OverlayDef, mask: Matrix | null = null): RenderResult {
  const [rows, cols] = m.shape;
  const data = new Uint8ClampedArray(rows * cols * 4);
  const useMask = mask && def.maskCodes.length > 0 && sameGrid(m, mask);
  const resolved = def.scale.kind === 'continuous' ? resolveDomain(def.scale, m) : null;
  let maskedCells = 0;
  let drawnCells = 0;
  for (let r = 0; r < rows; r++) {
    const row = m.values[r];
    for (let c = 0; c < cols; c++) {
      const v = row[c];
      if (v === null || !Number.isFinite(v)) continue;
      if (useMask) {
        const code = mask!.values[r][c];
        if (code !== null && def.maskCodes.includes(code)) {
          maskedCells++;
          continue;
        }
      }
      const color = colorFor(def, v, resolved);
      if (!color) continue;
      const i = (r * cols + c) * 4;
      data[i] = color[0];
      data[i + 1] = color[1];
      data[i + 2] = color[2];
      data[i + 3] = color[3];
      drawnCells++;
    }
  }
  return { width: cols, height: rows, data, resolved, maskedCells, drawnCells };
}

export const MASK_STYLE: Record<number, { label: string; color: RGBA }> = {
  [MASK_VEGETATED]: { label: 'Unobservable: vegetated (NDVI > 0.35)', color: [134, 239, 172, 200] },
  [MASK_DECORRELATED]: { label: 'Unobservable: decorrelated (coherence < 0.65)', color: [203, 213, 225, 200] },
  [MASK_BOTH]: { label: 'Vegetated and decorrelated', color: [253, 224, 71, 200] },
};

/**
 * Hatched image of the unobservable zones: each 30 m cell becomes `scale` x `scale` pixels with
 * diagonal stripes, so masked zones read as "no reliable data" rather than as a value.
 */
export function renderMaskHatch(mask: Matrix, scale = 4): RasterImage & { cells: Record<number, number> } {
  const [rows, cols] = mask.shape;
  const width = cols * scale;
  const height = rows * scale;
  const data = new Uint8ClampedArray(width * height * 4);
  const cells: Record<number, number> = { [MASK_VEGETATED]: 0, [MASK_DECORRELATED]: 0, [MASK_BOTH]: 0 };
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      const code = mask.values[r][c];
      const style = code !== null ? MASK_STYLE[code] : undefined;
      if (!style || code === null) continue;
      cells[code] += 1;
      for (let dy = 0; dy < scale; dy++) {
        for (let dx = 0; dx < scale; dx++) {
          const y = r * scale + dy;
          const x = c * scale + dx;
          const i = (y * width + x) * 4;
          const stripe = (x + y) % scale === 0;
          data[i] = style.color[0];
          data[i + 1] = style.color[1];
          data[i + 2] = style.color[2];
          data[i + 3] = stripe ? style.color[3] : 36;
        }
      }
    }
  }
  return { width, height, data, cells };
}
