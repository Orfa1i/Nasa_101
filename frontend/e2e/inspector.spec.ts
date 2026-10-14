/**
 * Double Inspector against the real backend (stamped SYNTHETIC demo site): cells are picked
 * with the coordinate form, and the charts are compared with the API's own pixel response.
 */
import { expect, test, type Page } from '@playwright/test';

import { isolateNetwork } from './helpers';

type MatrixJson = { bounds: [number, number, number, number]; shape: [number, number]; values: (number | null)[][] };

async function openFacility(page: Page, id: string) {
  await isolateNetwork(page, { terrain: false });
  await page.goto('/');
  await page.getByTestId('toggle-terrain').uncheck();
  await page.getByTestId('facility-select').selectOption(id);
  await page.getByTestId('sidebar-tab-inspector').click();
  await expect(page.getByTestId('coordinate-picker')).toBeVisible();
}

/** Centre of the cell with the most same-valued 4-neighbours among cells matching `pred`. */
async function cell(page: Page, layer: string, pred: (v: number) => boolean): Promise<[number, number]> {
  const m: MatrixJson = await (await page.request.get(`/api/facilities/sierrita/matrices?layer=${layer}`)).json();
  const [xmin, ymin, xmax, ymax] = m.bounds;
  const [rows, cols] = m.shape;
  let best = { score: -1, lat: 0, lon: 0 };
  for (let r = 1; r < rows - 1; r++)
    for (let c = 1; c < cols - 1; c++) {
      const v = m.values[r][c];
      if (v === null || !pred(v)) continue;
      const score = [m.values[r - 1][c], m.values[r + 1][c], m.values[r][c - 1], m.values[r][c + 1]].filter((x) => x !== null && pred(x)).length;
      if (score > best.score) best = { score, lon: xmin + ((c + 0.5) * (xmax - xmin)) / cols, lat: ymax - ((r + 0.5) * (ymax - ymin)) / rows };
    }
  if (best.score < 0) throw new Error(`no ${layer} cell matches`);
  return [Number(best.lat.toFixed(6)), Number(best.lon.toFixed(6))];
}

async function inspect(page: Page, lat: number, lon: number) {
  await page.getByTestId('input-lat').fill(String(lat));
  await page.getByTestId('input-lon').fill(String(lon));
  const res = page.waitForResponse((r) => r.url().includes('/pixel?'));
  await page.getByRole('button', { name: 'Inspect' }).click();
  return res;
}

const day = (t: string) => t.slice(0, 10);

test.describe('double inspector', () => {
  test('charts a Regime-4 cell from the coordinate endpoint', async ({ page }, info) => {
    await openFacility(page, 'sierrita');
    const [lat, lon] = await cell(page, 'anomaly_regime', (v) => v === 4);
    const res = await inspect(page, lat, lon);
    expect(res.ok()).toBe(true);
    const q = new URL(res.url()).searchParams;
    expect([Number(q.get('lat')), Number(q.get('lon'))]).toEqual([lat, lon]);
    const api = await res.json();
    await expect(page.getByTestId('pixel-coords')).toHaveText(`${lat.toFixed(5)}, ${lon.toFixed(5)}`);

    // Spectroscopy: 285-band spectrum with two gaps, the NNLS fit, fractions, RMSE and SAI.
    const spectrum = page.getByTestId('chart-spectrum');
    await expect(spectrum).toBeVisible();
    const paths = await spectrum.locator('.recharts-line-curve').evaluateAll((els) => els.map((e) => e.getAttribute('d') ?? ''));
    expect(paths).toHaveLength(2);
    for (const d of paths) expect(d.match(/M/g)).toHaveLength(3);
    await expect(spectrum.getByText('H₂O')).toHaveCount(2);
    const inactive = api.spectral.spectrum.active.filter((a: boolean) => !a).length;
    expect(inactive).toBe(40);
    await expect(spectrum).toContainText('40 water-vapour channels (1340–1460 nm, 1790–1970 nm)');
    await expect(page.getByTestId('chart-abundance').locator('.recharts-bar-rectangle')).toHaveCount(5);
    await expect(page.getByTestId('chart-abundance')).toContainText(`${(api.spectral.spectrum.abundance.Jarosite * 100).toFixed(1)} %`);
    await expect(page.getByTestId('pixel-sai')).toContainText(api.spectral.spectrum.sai.toFixed(3));
    await expect(page.getByTestId('pixel-sai')).toContainText('Above threshold');
    await expect(page.getByTestId('pixel-rmse')).toContainText(api.spectral.spectrum.rmse.toFixed(4));
    await info.attach('spectroscopy.png', { body: await page.locator('#dashboard-panel').screenshot(), contentType: 'image/png' });

    // Kinematics: displacement, GP velocity/acceleration with bands, Fukuzono trend.
    await page.getByTestId('tab-kinematics').click();
    for (const id of ['chart-displacement', 'chart-velocity', 'chart-acceleration', 'chart-fukuzono']) await expect(page.getByTestId(id)).toBeVisible();
    await expect(page.getByTestId('chart-displacement').locator('.recharts-scatter-symbol')).toHaveCount(api.kinematics.times.length);
    await expect(page.getByTestId('chart-velocity').locator('.recharts-area-area')).toHaveCount(1);
    const f = api.kinematics.fukuzono;
    const fk = page.getByTestId('chart-fukuzono');
    await expect(fk.getByTestId('fukuzono-label')).toContainText('not a guaranteed failure date');
    await expect(fk).toContainText(`Estimated change in velocity: ${day(f.crossing_estimate)} (95 %: ${day(f.crossing_lower)} – ${day(f.crossing_upper)})`);
    await expect(fk.locator('.recharts-reference-area')).toHaveCount(1);
    await expect(page.getByTestId('coherence-alert')).toHaveCount(0);
    await info.attach('kinematics.png', { body: await page.locator('#dashboard-panel').screenshot(), contentType: 'image/png' });

    // Hydrology: D_crest history with alert levels and GISTM checks.
    await page.getByTestId('tab-hydrology').click();
    const setback = page.getByTestId('chart-setback');
    await expect(setback).toBeVisible();
    await expect(setback.locator('.recharts-line-dots circle')).toHaveCount(2);
    await expect(page.getByTestId('setback-table').getByRole('row')).toHaveCount(3);
    const checks = page.getByTestId('gistm-checks');
    await expect(checks.locator('[data-check="proximity"]')).toHaveAttribute('data-level', 'good'); // 90 m > 15 m
    await expect(checks.locator('[data-check="shrink-baseline"]')).toHaveAttribute('data-level', 'serious'); // -60 m
    const dash = await (await page.request.get('/api/facilities/sierrita/dashboard')).json();
    await expect(page.getByTestId('gistm-issues').getByRole('listitem')).toHaveCount(dash.gistm_screening.candidate_issues.length);
    await info.attach('hydrology.png', { body: await page.locator('#dashboard-panel').screenshot(), contentType: 'image/png' });
  });

  test('alerts on low temporal coherence for a decorrelated cell', async ({ page }) => {
    await openFacility(page, 'sierrita');
    const [lat, lon] = await cell(page, 'observability_mask', (v) => v === 2);
    expect((await inspect(page, lat, lon)).ok()).toBe(true);
    await page.getByTestId('tab-kinematics').click();
    await expect(page.getByTestId('coherence-alert')).toContainText('< 0.65');
    await expect(page.getByTestId('pixel-coherence')).toContainText('Decorrelated');
    await expect(page.getByTestId('fukuzono-empty')).toBeVisible();
  });

  test('handles a facility with no products', async ({ page }) => {
    await openFacility(page, 'mission');
    const res = await inspect(page, 31.975, -111.04);
    expect(res.status()).toBe(404);
    await expect(page.getByTestId('spectral-empty')).toContainText('Mission_spectral.zarr not found');
    await page.getByTestId('tab-kinematics').click();
    await expect(page.getByTestId('kinematics-empty')).toContainText('Mission_kinematics.zarr not found');
    await page.getByTestId('tab-hydrology').click();
    await expect(page.getByTestId('setback-empty')).toBeVisible();
    await expect(page.getByTestId('gistm-checks').locator('[data-check]')).toHaveCount(1);
    await expect(page.getByTestId('gistm-checks').locator('[data-check]')).toHaveAttribute('data-level', 'unknown');
  });
});
