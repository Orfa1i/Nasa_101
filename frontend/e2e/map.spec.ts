/**
 * MapViewer + Sidebar against the real stack (next start -> FastAPI -> SYNTHETIC demo products).
 * See playwright.config.ts for how the servers are started.
 */
import { expect, test, type Page } from '@playwright/test';

import {
  BINGHAM,
  SIERRITA,
  collectPageErrors,
  isolateNetwork,
  layerIds,
  mapAttr,
  meanDifference,
  pixelFraction,
  screenPoint,
  waitForCamera,
  zoomInAt,
} from './helpers';

type MatrixJson = { bounds: [number, number, number, number]; shape: [number, number]; values: (number | null)[][]; available_times: string[] };

async function selectFacility(page: Page, id: string) {
  await page.getByTestId('facility-select').selectOption(id);
}

/** Wait until every id is in the deck.gl layer stack (flat or draped variant). */
async function waitForLayers(page: Page, ...ids: string[]) {
  await expect
    .poll(async () => {
      const have = await layerIds(page);
      return ids.every((id) => have.some((h) => h === id || h === `${id}@draped`));
    })
    .toBe(true);
}

async function terrainOff(page: Page) {
  await page.getByTestId('toggle-terrain').uncheck();
  await expect.poll(() => mapAttr(page, 'terrain')).toBe('off');
}

/** Screenshot of the synthetic site (the processed ~1 km block) on a pitch-0 map. */
async function siteShot(page: Page, matrix: MatrixJson) {
  const [x0, y0, x1, y1] = siteExtent(matrix);
  const a = await screenPoint(page, x0, y1);
  const b = await screenPoint(page, x1, y0);
  return page.screenshot({ clip: { x: a.x, y: a.y, width: Math.max(4, b.x - a.x), height: Math.max(4, b.y - a.y) }, animations: 'disabled' });
}

/** lon/lat extent of the non-null cells of a matrix. */
function siteExtent(m: MatrixJson): [number, number, number, number] {
  const [xmin, ymin, xmax, ymax] = m.bounds;
  const [rows, cols] = m.shape;
  let r0 = rows, r1 = -1, c0 = cols, c1 = -1;
  m.values.forEach((row, r) =>
    row.forEach((v, c) => {
      if (v === null) return;
      r0 = Math.min(r0, r); r1 = Math.max(r1, r); c0 = Math.min(c0, c); c1 = Math.max(c1, c);
    }),
  );
  const dx = (xmax - xmin) / cols;
  const dy = (ymax - ymin) / rows;
  return [xmin + c0 * dx, ymax - (r1 + 1) * dy, xmin + (c1 + 1) * dx, ymax - r0 * dy];
}

function cellCentre(m: MatrixJson, predicate: (v: number) => boolean): [number, number] {
  const [xmin, , xmax, ymax] = m.bounds;
  const [rows, cols] = m.shape;
  const dx = (xmax - xmin) / cols;
  const dy = (ymax - m.bounds[1]) / rows;
  // Prefer the cell with the most 4-neighbours sharing the value, so a click lands on it robustly.
  let best: { score: number; lonlat: [number, number] | null } = { score: -1, lonlat: null };
  for (let r = 1; r < rows - 1; r++)
    for (let c = 1; c < cols - 1; c++) {
      const v = m.values[r][c];
      if (v === null || !predicate(v)) continue;
      const n = [m.values[r - 1][c], m.values[r + 1][c], m.values[r][c - 1], m.values[r][c + 1]];
      const score = n.filter((x) => x !== null && predicate(x)).length;
      if (score > best.score) best = { score, lonlat: [xmin + (c + 0.5) * dx, ymax - (r + 0.5) * dy] };
    }
  if (!best.lonlat) throw new Error('no matching cell');
  return best.lonlat;
}

async function matrix(page: Page, layer: string): Promise<MatrixJson> {
  const r = await page.request.get(`/api/facilities/sierrita/matrices?layer=${layer}`);
  expect(r.ok()).toBe(true);
  return r.json();
}

test.describe('map viewer', () => {
  test('renders facilities coloured by alert, hover tooltips and the provenance watermark', async ({ page }) => {
    const errors = collectPageErrors(page);
    await isolateNetwork(page, { terrain: false });
    await page.goto('/');

    const list = page.getByRole('list', { name: 'Facilities by alert status' });
    await expect(list.getByRole('button')).toHaveCount(4);
    const sierrita = list.getByRole('button', { name: /Sierrita/ });
    await expect(sierrita.locator('[data-alert]')).toHaveAttribute('data-alert', 'red');
    await expect(list.getByRole('button', { name: /Mission/ }).locator('[data-alert]')).toHaveAttribute('data-alert', 'unknown');

    await expect(page.getByTestId('watermark')).toHaveAttribute('data-provenance', 'none');
    await expect(page.getByTestId('watermark-headline')).toHaveText('NO MATRIX DATA LOADED');
    await waitForLayers(page, 'facilities', 'facility-markers');

    // Hover the Sierrita marker in the world view (pitch 0).
    const p = await screenPoint(page, SIERRITA.centre[0], SIERRITA.centre[1]);
    await page.mouse.move(p.x, p.y);
    const tooltip = page.getByTestId('facility-tooltip');
    await expect(tooltip).toContainText('Sierrita');
    await expect(tooltip).toContainText('CRITICAL_TIER3');
    await expect(tooltip).toContainText('copper');
    await expect(tooltip).toContainText('SYNTHETIC/MOCK');
    expect(errors).toEqual([]);
  });

  test('selecting a facility flies the 3D camera to its bbox and drapes matrices on the terrain', async ({ page }) => {
    const errors = collectPageErrors(page);
    const net = await isolateNetwork(page, { terrain: true });
    await page.goto('/');
    await selectFacility(page, 'sierrita');
    await waitForCamera(page, SIERRITA.centre[0], SIERRITA.centre[1], { pitch: 50 });
    expect(Number(await mapAttr(page, 'zoom'))).toBeGreaterThan(11);

    await expect.poll(() => mapAttr(page, 'terrain')).toBe('ok');
    expect(net.terrainTiles).toBeGreaterThan(0);
    await waitForLayers(page, 'terrain', 'matrix-anomaly_regime', 'observability-mask', 'facilities');
    expect(await layerIds(page)).toContain('matrix-anomaly_regime@draped');

    await expect(page.getByTestId('watermark')).toHaveAttribute('data-provenance', 'simulated');
    await expect(page.getByTestId('watermark-headline')).toHaveText('SIMULATED DATASET');
    await expect(page.getByTestId('simulated-backdrop')).toBeVisible();

    // The draped regime raster is actually drawn on the (grey, lit) terrain: Regime-1 green
    // appears, and disappears when the overlay is switched off.
    const greenish = (r: number, g: number, b: number) => g > r + 25 && g > b + 10;
    await page.waitForTimeout(800);
    const withRegimes = pixelFraction(await page.getByTestId('map-viewer').screenshot(), greenish);
    expect(withRegimes).toBeGreaterThan(0.002);
    await page.getByTestId('toggle-anomaly_regime').uncheck();
    await expect.poll(async () => (await layerIds(page)).some((l) => l.startsWith('matrix-'))).toBe(false);
    await page.waitForTimeout(500);
    expect(pixelFraction(await page.getByTestId('map-viewer').screenshot(), greenish)).toBeLessThan(withRegimes / 10);
    await page.getByTestId('toggle-anomaly_regime').check();

    // Re-centres on another facility.
    await selectFacility(page, 'bingham');
    await waitForCamera(page, BINGHAM.centre[0], BINGHAM.centre[1], { pitch: 50 });
    // Bingham has no products: nothing is shown and the watermark says so.
    await expect(page.getByTestId('watermark')).toHaveAttribute('data-provenance', 'none');
    await expect(page.getByTestId('status-anomaly_regime')).toHaveText('n/a');
    expect(errors).toEqual([]);
  });

  test('falls back to flat overlays when terrain tiles are unreachable', async ({ page }) => {
    await isolateNetwork(page, { terrain: false });
    await page.goto('/');
    await selectFacility(page, 'sierrita');
    await expect.poll(() => mapAttr(page, 'terrain')).toBe('failed');
    await expect(page.getByTestId('terrain-fallback')).toBeVisible();
    // the fallback is reported in the issues log
    await page.getByTestId('issues-button').click();
    await expect(page.getByTestId('issues-list')).toContainText('fallback: flat overlays (no 3D terrain)');
    await waitForLayers(page, 'matrix-anomaly_regime', 'facilities');
    expect(await layerIds(page)).not.toContain('terrain');
  });

  test('switches overlay matrices, legends and the OPERA epoch', async ({ page }) => {
    const errors = collectPageErrors(page);
    await isolateNetwork(page, { terrain: false });
    await page.goto('/');
    await terrainOff(page);
    await selectFacility(page, 'sierrita');
    await waitForCamera(page, SIERRITA.centre[0], SIERRITA.centre[1], { pitch: 0 });
    await waitForLayers(page, 'matrix-anomaly_regime');
    await expect(page.getByTestId('legend-anomaly_regime')).toBeVisible();
    const regime = await matrix(page, 'anomaly_regime');
    const before = await siteShot(page, regime);

    // SAI on top of the regimes.
    const saiResponse = page.waitForResponse((r) => r.url().includes('/matrices?layer=sai') && r.ok());
    await page.getByTestId('toggle-sai').check();
    await saiResponse;
    await waitForLayers(page, 'matrix-sai', 'matrix-anomaly_regime');
    await expect(page.getByTestId('legend-sai')).toContainText('SAI 0.25');
    await page.waitForTimeout(300);
    const withSai = await siteShot(page, regime);
    expect(meanDifference(before, withSai)).toBeGreaterThan(2);

    // Regimes off: layer and legend disappear.
    await page.getByTestId('toggle-anomaly_regime').uncheck();
    await expect.poll(async () => (await layerIds(page)).includes('matrix-anomaly_regime')).toBe(false);
    await expect(page.getByTestId('legend-anomaly_regime')).toHaveCount(0);
    await page.getByTestId('toggle-sai').uncheck();
    await expect.poll(async () => (await layerIds(page)).some((l) => l.startsWith('matrix-'))).toBe(false);
    await expect(page.getByTestId('legend')).toHaveCount(0);

    // Regimes back on: same pixels as before (switching is reversible).
    await page.getByTestId('toggle-anomaly_regime').check();
    await waitForLayers(page, 'matrix-anomaly_regime');
    await page.waitForTimeout(300);
    expect(meanDifference(before, await siteShot(page, regime))).toBeLessThan(1);

    // Displacement + timeline: moving to the first epoch requests that epoch.
    await page.getByTestId('toggle-displacement').check();
    await waitForLayers(page, 'matrix-displacement');
    await expect(page.getByTestId('legend-displacement')).toBeVisible();
    const disp = await matrix(page, 'displacement');
    const first = disp.available_times[0].slice(0, 10);
    const epochReq = page.waitForRequest((r) => r.url().includes('layer=displacement') && r.url().includes('&t='));
    const slider = page.getByTestId('epoch-slider');
    await slider.focus();
    await page.keyboard.press('Home');
    await epochReq;
    await expect(page.getByTestId('epoch-label')).toHaveText(first);
    await expect(page.getByTestId('legend-displacement')).toContainText(`epoch ${first}`);
    await page.getByRole('button', { name: 'Latest' }).click();
    await expect(page.getByTestId('legend-displacement')).toContainText(`epoch ${disp.available_times.at(-1)!.slice(0, 10)}`);
    expect(errors).toEqual([]);
  });

  test('masks unobservable vegetated / decorrelated cells', async ({ page }) => {
    await isolateNetwork(page, { terrain: false });
    await page.goto('/');
    await terrainOff(page);
    await selectFacility(page, 'sierrita');
    await waitForCamera(page, SIERRITA.centre[0], SIERRITA.centre[1], { pitch: 0 });
    await page.getByTestId('toggle-anomaly_regime').uncheck();
    await page.getByTestId('toggle-displacement').check();
    await waitForLayers(page, 'matrix-displacement', 'observability-mask');
    await expect(page.getByTestId('legend-mask')).toContainText('decorrelated');
    await expect(page.getByTestId('quality-panel')).toContainText(/Masked: \d+ vegetated, \d+ decorrelated/);

    const mask = await matrix(page, 'observability_mask');
    const [lon, lat] = cellCentre(mask, (v) => v === 2);
    await zoomInAt(page, lon, lat, 15); // ~20 px per 30 m cell
    const p = await screenPoint(page, lon, lat);
    const clip = { x: p.x - 3, y: p.y - 3, width: 6, height: 6 };
    const masked = await page.screenshot({ clip });
    await page.getByTestId('toggle-mask').uncheck();
    await expect.poll(async () => (await layerIds(page)).includes('observability-mask')).toBe(false);
    await expect(page.getByTestId('legend-mask')).toHaveCount(0);
    await page.waitForTimeout(300);
    const unmasked = await page.screenshot({ clip });
    // Without the mask the decorrelated cell shows its displacement colour instead of the hatch.
    expect(meanDifference(masked, unmasked)).toBeGreaterThan(5);
  });

  test('clicking a cell loads it into the sidebar; RMSE and coherence are shown', async ({ page }) => {
    await isolateNetwork(page, { terrain: false });
    await page.goto('/');
    await terrainOff(page);
    await selectFacility(page, 'sierrita');
    await waitForCamera(page, SIERRITA.centre[0], SIERRITA.centre[1], { pitch: 0 });
    await waitForLayers(page, 'matrix-anomaly_regime');

    const quality = page.getByTestId('quality-panel');
    await expect(page.getByTestId('quality-rmse')).toContainText(/\d\.\d{4}/);
    await expect(page.getByTestId('quality-coherence')).toContainText(/0\.\d\d/);
    await expect(page.getByTestId('quality-coherence')).not.toContainText('0.0 %'); // the decorrelated patch is flagged

    const regime = await matrix(page, 'anomaly_regime');
    const [lon, lat] = cellCentre(regime, (v) => v === 4);
    await zoomInAt(page, lon, lat, 15); // ~20 px per 30 m cell
    const p = await screenPoint(page, lon, lat);
    await page.mouse.move(p.x, p.y);
    await expect(page.getByTestId('quality-hover')).toContainText('cursor RMSE');
    await expect(page.getByTestId('matrix-tooltip')).toContainText('Anomaly regimes: 4');

    const pixelReq = page.waitForResponse((r) => r.url().includes('/pixel?') && r.ok());
    await page.mouse.click(p.x, p.y);
    const res = await pixelReq;
    // The request carries the clicked coordinate (within one 30 m cell of the cell centre).
    const q = new URL(res.url()).searchParams;
    expect(Math.abs(Number(q.get('lon')) - lon)).toBeLessThan(0.0003);
    expect(Math.abs(Number(q.get('lat')) - lat)).toBeLessThan(0.0003);
    const panel = page.getByTestId('pixel-panel');
    await expect(panel.getByTestId('pixel-coords')).toHaveText(`${Number(q.get('lat')).toFixed(5)}, ${Number(q.get('lon')).toFixed(5)}`);
    await expect(panel.getByTestId('pixel-regime')).toContainText('4 · regime4-active-instability');
    await expect(panel).toContainText('not a failure forecast');
    // ...and populated the inspector charts.
    await expect(page.getByTestId('chart-spectrum')).toBeVisible();
    await expect(page.getByTestId('pixel-rmse')).toContainText(/\d\.\d{4}/);
    await page.getByTestId('tab-kinematics').click();
    await expect(page.getByTestId('pixel-coherence')).toContainText(/0\.\d\d/);
    await expect(page.getByTestId('chart-fukuzono')).toBeVisible();
    await expect.poll(async () => (await layerIds(page)).includes('picked-point')).toBe(true);
    await expect(quality).toBeVisible();
  });

  test('reports an unreachable backend instead of crashing', async ({ page }) => {
    await isolateNetwork(page, { terrain: false });
    await page.route('**/api/facilities', (route) => route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"database offline"}' }));
    await page.goto('/');
    await expect(page.getByRole('alert').filter({ hasText: 'Backend unavailable' })).toContainText('database offline');
    await expect(page.getByTestId('map-viewer')).toBeVisible();
  });
});
