/** Scenario helpers shared with the backend rules (backend/app/api/scenarios.py). */
import type { Facility } from './api';

/** lon/lat extent of the state of Arizona; stress tests are limited to facilities inside it. */
export const ARIZONA_BOUNDS = [-114.82, 31.33, -109.04, 37.0] as const;

export function inArizona(f: Pick<Facility, 'bbox_xmin' | 'bbox_ymin' | 'bbox_xmax' | 'bbox_ymax'>): boolean {
  const [x0, y0, x1, y1] = ARIZONA_BOUNDS;
  return x0 <= f.bbox_xmin && f.bbox_xmax <= x1 && y0 <= f.bbox_ymin && f.bbox_ymax <= y1;
}
