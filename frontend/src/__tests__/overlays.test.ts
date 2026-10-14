import {
  MASK_DECORRELATED,
  MASK_VEGETATED,
  OVERLAY_BY_ID,
  REGIME_CLASSES,
  cellAt,
  colorFor,
  fractionBeyond,
  matrixStats,
  renderMaskHatch,
  renderMatrix,
  resolveDomain,
  valueAt,
} from '@/utils/overlays';

import { MATRICES, matrix } from './fixtures';

const alphaAt = (img: { data: Uint8ClampedArray; width: number }, r: number, c: number) => img.data[(r * img.width + c) * 4 + 3];
const rgbAt = (img: { data: Uint8ClampedArray; width: number }, r: number, c: number) => {
  const i = (r * img.width + c) * 4;
  return [img.data[i], img.data[i + 1], img.data[i + 2]];
};

describe('matrix lookup', () => {
  const m = MATRICES.anomaly_regime(null);

  it('row 0 is the northern edge and columns run west to east', () => {
    // top-left cell (NW corner) holds regime 4
    expect(cellAt(m, -111.119, 31.899)).toEqual([0, 0]);
    expect(valueAt(m, -111.119, 31.899)).toBe(4);
    expect(cellAt(m, -111.041, 31.831)).toEqual([3, 4]);
    expect(valueAt(m, -111.041, 31.831)).toBeNull();
  });

  it('returns null outside the bounds and clamps the far edges', () => {
    expect(cellAt(m, -111.2, 31.86)).toBeNull();
    expect(cellAt(m, -111.04, 31.83)).toEqual([3, 4]);
  });

  it('computes statistics over valid cells only', () => {
    const rmse = MATRICES.spectral_rmse(null);
    const s = matrixStats(rmse);
    expect(s.count).toBe(16);
    expect(s.min).toBeCloseTo(0.01);
    expect(s.max).toBeCloseTo(0.08);
    expect(fractionBeyond(rmse, 0.05, true)).toBeCloseTo(4 / 16);
    expect(fractionBeyond(MATRICES.coherence(null), 0.65, false)).toBeCloseTo(4 / 16);
    expect(fractionBeyond(matrix('x', [[null]], { shape: [1, 1] }), 0.5, true)).toBeNull();
  });
});

describe('renderMatrix', () => {
  it('colours categorical regimes with the legend colours and leaves nulls transparent', () => {
    const m = MATRICES.anomaly_regime(null);
    const img = renderMatrix(m, OVERLAY_BY_ID.anomaly_regime);
    expect([img.width, img.height]).toEqual([5, 4]);
    expect(rgbAt(img, 0, 0)).toEqual(REGIME_CLASSES[4].color.slice(0, 3));
    expect(rgbAt(img, 1, 1)).toEqual(REGIME_CLASSES[1].color.slice(0, 3));
    expect(alphaAt(img, 0, 4)).toBe(0);
    expect(img.drawnCells).toBe(16);
  });

  it('makes masked cells transparent only for overlays that the mask applies to', () => {
    const mask = MATRICES.observability_mask(null);
    const sai = renderMatrix(MATRICES.sai(null), OVERLAY_BY_ID.sai, mask);
    expect(alphaAt(sai, 1, 0)).toBe(0); // vegetated: SAI unobservable
    expect(alphaAt(sai, 0, 2)).toBeGreaterThan(0); // decorrelated does not affect spectra
    expect(sai.maskedCells).toBe(1);

    const disp = renderMatrix(MATRICES.displacement(null), OVERLAY_BY_ID.displacement, mask);
    expect(alphaAt(disp, 0, 2)).toBe(0); // decorrelated: InSAR unobservable
    expect(alphaAt(disp, 1, 0)).toBeGreaterThan(0);
    expect(disp.maskedCells).toBe(4);

    const regimes = renderMatrix(MATRICES.anomaly_regime(null), OVERLAY_BY_ID.anomaly_regime, mask);
    expect(regimes.maskedCells).toBe(0);
  });

  it('ignores a mask on a different grid instead of misaligning it', () => {
    const otherGrid = matrix('observability_mask', [[1, 1], [1, 1]], { shape: [2, 2] });
    expect(renderMatrix(MATRICES.sai(null), OVERLAY_BY_ID.sai, otherGrid).maskedCells).toBe(0);
  });

  it('fades low SAI and hides tiny flow accumulations', () => {
    const def = OVERLAY_BY_ID.sai;
    const low = colorFor(def, 0.02, null)!;
    const high = colorFor(def, 0.5, null)!;
    expect(low[3]).toBeLessThan(high[3]);
    expect(colorFor(OVERLAY_BY_ID.flow_accumulation, 2, null)).toBeNull();
    expect(colorFor(OVERLAY_BY_ID.flow_accumulation, 500, null)).not.toBeNull();
  });

  it('uses a symmetric domain for displacement', () => {
    const d = resolveDomain(OVERLAY_BY_ID.displacement.scale as never, MATRICES.displacement(null));
    expect(d.domain[0]).toBe(-d.domain[1]);
    expect(d.domain[1]).toBeGreaterThanOrEqual(5);
  });
});

describe('renderMaskHatch', () => {
  it('draws striped cells per mask class and counts them', () => {
    const img = renderMaskHatch(MATRICES.observability_mask(null), 4);
    expect([img.width, img.height]).toEqual([20, 16]);
    expect(img.cells[MASK_VEGETATED]).toBe(1);
    expect(img.cells[MASK_DECORRELATED]).toBe(4);
    // observable cell (0,0) is empty; vegetated cell (1,0) has opaque stripes and faint fill
    expect(img.data[3]).toBe(0);
    const alphas = new Set<number>();
    for (let y = 4; y < 8; y++) for (let x = 0; x < 4; x++) alphas.add(img.data[(y * img.width + x) * 4 + 3]);
    expect([...alphas].sort()).toEqual([200, 36].sort());
  });
});
