/** Colour ramps (sampled from matplotlib / ColorBrewer) and helpers to map values to RGBA. */

export type RGB = [number, number, number];
export type RGBA = [number, number, number, number];

export const RAMPS: Record<string, RGB[]> = {
  viridis: [[68, 1, 84], [72, 40, 120], [62, 74, 137], [49, 104, 142], [38, 130, 142], [31, 158, 137], [53, 183, 121], [109, 205, 89], [180, 222, 44], [253, 231, 37]],
  magma: [[0, 0, 4], [28, 16, 68], [79, 18, 123], [129, 37, 129], [181, 54, 122], [229, 80, 100], [251, 135, 97], [254, 194, 135], [252, 253, 191]],
  ylOrRd: [[255, 255, 178], [254, 217, 118], [254, 178, 76], [253, 141, 60], [240, 59, 32], [189, 0, 38]],
  blues: [[198, 219, 239], [158, 202, 225], [107, 174, 214], [66, 146, 198], [33, 113, 181], [8, 69, 148]],
  // Diverging: blue (negative) -> white -> red (positive)
  rdBu: [[33, 102, 172], [103, 169, 207], [209, 229, 240], [247, 247, 247], [253, 219, 199], [239, 138, 98], [178, 24, 43]],
};

/** Colour at fraction `t` (clamped to [0, 1]) along a ramp. */
export function rampColor(ramp: RGB[], t: number): RGB {
  const x = Math.min(1, Math.max(0, Number.isFinite(t) ? t : 0)) * (ramp.length - 1);
  const i = Math.min(ramp.length - 2, Math.floor(x));
  const f = x - i;
  const a = ramp[i];
  const b = ramp[i + 1];
  return [Math.round(a[0] + (b[0] - a[0]) * f), Math.round(a[1] + (b[1] - a[1]) * f), Math.round(a[2] + (b[2] - a[2]) * f)];
}

export function cssColor(c: RGB | RGBA): string {
  return c.length === 4 ? `rgba(${c[0]},${c[1]},${c[2]},${(c[3] / 255).toFixed(3)})` : `rgb(${c[0]},${c[1]},${c[2]})`;
}

/** CSS linear-gradient for a legend bar. */
export function rampGradient(ramp: RGB[]): string {
  const stops = ramp.map((c, i) => `${cssColor(c)} ${((i / (ramp.length - 1)) * 100).toFixed(1)}%`);
  return `linear-gradient(to right, ${stops.join(', ')})`;
}
