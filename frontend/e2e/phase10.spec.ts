/**
 * Phase 10 against the real backend: Brumadinho backtest (the demo DATA_DIR holds a SYNTHETIC
 * stand-in CSV, so it must be badged as such), the stress-test simulator, the Compliance Copilot
 * with print/PDF export, and outage fallbacks.
 */
import { expect, test, type Page } from '@playwright/test';

import { isolateNetwork, layerIds } from './helpers';

type MatrixJson = { bounds: [number, number, number, number]; shape: [number, number]; values: (number | null)[][] };

async function open(page: Page) {
  await isolateNetwork(page, { terrain: false });
  await page.goto('/');
  await page.getByTestId('toggle-terrain').uncheck();
}

async function regimeCell(page: Page, code: number): Promise<[number, number]> {
  const m: MatrixJson = await (await page.request.get('/api/facilities/sierrita/matrices?layer=anomaly_regime')).json();
  const [xmin, ymin, xmax, ymax] = m.bounds;
  const [rows, cols] = m.shape;
  for (let r = 1; r < rows - 1; r++)
    for (let c = 1; c < cols - 1; c++)
      if ([m.values[r][c], m.values[r - 1][c], m.values[r + 1][c], m.values[r][c - 1], m.values[r][c + 1]].every((v) => v === code))
        return [Number((ymax - ((r + 0.5) * (ymax - ymin)) / rows).toFixed(6)), Number((xmin + ((c + 0.5) * (xmax - xmin)) / cols).toFixed(6))];
  throw new Error('no cell');
}

test.describe('scenarios', () => {
  test('Brumadinho backtest: badged stand-in, 40-day inverse velocity, re-issued forecasts', async ({ page }) => {
    await open(page);
    const first = page.waitForResponse((r) => r.url().includes('/api/backtest/brumadinho'));
    await page.getByTestId('sidebar-tab-scenarios').click();
    const api = await (await first).json();
    const badge = page.getByTestId('backtest-badge');
    await expect(badge).toHaveAttribute('data-kind', 'synthetic'); // the demo CSV is a labelled stand-in
    await expect(badge).toContainText('SYNTHETIC STAND-IN');
    expect(api.historical).toBe(false);
    await expect(page.getByTestId('chart-backtest-lambda')).toContainText('40-day pre-collapse inverse velocity');
    await expect(page.getByTestId('chart-backtest-lambda').locator('.recharts-reference-line')).toHaveCount(2);
    await expect(page.getByTestId('backtest-estimate')).toContainText(api.outcome.estimate.slice(0, 10));
    await expect(page.getByTestId('backtest-sweep').getByRole('row')).toHaveCount(api.sweep.length + 1);

    const slider = page.getByTestId('backtest-cutoff');
    await slider.focus();
    const reissued = page.waitForResponse((r) => r.url().includes('cutoff_days=10'));
    for (let i = 0; i < 10; i++) await page.keyboard.press('ArrowRight');
    const later = await (await reissued).json();
    expect(new Date(later.outcome.issued) <= new Date('2019-01-15')).toBe(true);
    await expect(page.getByTestId('backtest-issued')).toContainText(later.outcome.issued.slice(0, 10));
  });

  test('stress test: simulated scenario re-runs the pipelines and is drawn as a labelled test overlay', async ({ page }) => {
    await open(page);
    await page.getByTestId('facility-select').selectOption('sierrita');
    const [lat, lon] = await regimeCell(page, 1);
    await page.getByTestId('sidebar-tab-inspector').click();
    await page.getByTestId('input-lat').fill(String(lat));
    await page.getByTestId('input-lon').fill(String(lon));
    await page.getByRole('button', { name: 'Inspect' }).click();
    await expect(page.getByTestId('chart-spectrum')).toBeVisible();

    await page.getByTestId('sidebar-tab-scenarios').click();
    await page.getByTestId('scenario-tab-stress').click();
    await expect(page.getByTestId('stress-warning')).toContainText('SIMULATED HAZARD SCENARIO');
    await expect(page.getByTestId('stress-cell')).toHaveText(`${lat.toFixed(5)}, ${lon.toFixed(5)}`);
    await expect(page.getByTestId('stress-status')).toHaveAttribute('data-state', 'ready', { timeout: 30_000 });

    const full = page.waitForResponse((r) => r.url().includes('/stress-test?') && r.url().includes('intensity=1'), { timeout: 30_000 });
    await page.getByTestId('stress-slider').focus();
    await page.keyboard.press('End');
    const res = await full;
    const api = await res.json();
    expect(new URL(res.url()).searchParams.get('lat')).toBe(String(lat));
    await expect(page.getByTestId('stress-status')).toHaveAttribute('data-state', 'ready', { timeout: 30_000 });
    await expect(page.getByTestId('stress-sai')).toHaveText(api.scenario.sai.toFixed(3));
    await expect(page.getByTestId('stress-hazard')).toHaveText(api.scenario_hazard_level);
    expect(api.scenario.sai).toBeGreaterThan(api.baseline.sai);
    expect(api.setback.scenario_d_crest_m.at(-1)).toBeCloseTo(5, 5);

    // map: labelled test overlay + simulated watermark
    await expect(page.getByTestId('stress-overlay-badge')).toContainText('TEST OVERLAY · SIMULATED STRESS TEST (100 %)');
    await expect.poll(async () => (await layerIds(page)).filter((l) => l.startsWith('stress-'))).toEqual(['stress-regime', 'stress-footprint']);
    await expect(page.getByTestId('watermark')).toHaveAttribute('data-provenance', 'simulated');
    await expect(page.getByTestId('watermark')).toContainText('SIMULATED STRESS TEST (test overlay)');
    await page.getByTestId('stress-overlay-toggle').uncheck();
    await expect(page.getByTestId('stress-overlay-badge')).toHaveCount(0);
    await expect.poll(async () => (await layerIds(page)).some((l) => l.startsWith('stress-'))).toBe(false);
  });

  test('stress test refuses facilities outside Arizona', async ({ page }) => {
    await open(page);
    await page.getByTestId('facility-select').selectOption('bingham');
    await page.getByTestId('sidebar-tab-scenarios').click();
    await page.getByTestId('scenario-tab-stress').click();
    await expect(page.getByTestId('stress-not-az')).toContainText('limited to Arizona');
  });
});

test.describe('compliance copilot', () => {
  test('runs an audit, shows the GISTM report and exports a PDF of the report only', async ({ page }) => {
    await open(page);
    await page.getByTestId('facility-select').selectOption('sierrita');
    await page.getByTestId('sidebar-tab-audit').click();
    await expect(page.getByTestId('audit-copilot-status')).toContainText('Claude copilot not configured');
    await page.getByTestId('audit-mode').selectOption('deterministic');
    const posted = page.waitForResponse((r) => r.url().endsWith('/api/facilities/sierrita/audit') && r.request().method() === 'POST');
    await page.getByTestId('audit-run').click();
    const res = await posted;
    expect(res.status()).toBe(200);
    expect(res.request().postDataJSON()).toEqual({ mode: 'deterministic', query: null });
    const audit = await res.json();
    await expect(page.getByTestId('audit-run-status')).toHaveAttribute('data-state', 'done');
    const report = page.getByTestId('audit-report');
    await expect(report.getByTestId('audit-risk')).toHaveText(audit.brief.risk_classification);
    await expect(report.getByTestId('audit-validation')).toHaveText('passed');
    await expect(report.getByTestId('audit-issues').getByRole('listitem')).toHaveCount(audit.brief.compliance_issues.length);

    // Print media shows only the report: the app root is hidden.
    await page.emulateMedia({ media: 'print' });
    await expect(page.locator('#__next')).toBeHidden();
    await expect(page.getByTestId('audit-report-print')).toBeVisible();
    await expect(page.getByTestId('audit-report-print')).toContainText(audit.brief.facility);
    const pdf = await page.pdf({ format: 'A4' });
    const pages = (pdf.toString('latin1').match(/\/Type\s*\/Page[^s]/g) ?? []).length;
    expect(pdf.subarray(0, 4).toString()).toBe('%PDF');
    expect(pages).toBeGreaterThanOrEqual(1);
    expect(pages).toBeLessThanOrEqual(3);
    await page.emulateMedia({ media: 'screen' });
    await expect(page.locator('#__next')).toBeVisible();
  });
});

test.describe('outages', () => {
  test('API outage: cached registry, outage banner and issues log; recovers on retry', async ({ page }) => {
    await isolateNetwork(page, { terrain: false });
    await page.goto('/');
    await expect(page.getByRole('list', { name: 'Facilities by alert status' }).getByRole('button')).toHaveCount(4); // cached now

    await page.route('**/api/**', (route) => route.abort('connectionrefused'));
    await page.reload();
    await expect(page.getByTestId('registry-stale')).toContainText('Backend unreachable');
    await expect(page.getByRole('list', { name: 'Facilities by alert status' }).getByRole('button')).toHaveCount(4);
    await expect(page.getByTestId('api-down-banner')).toContainText('Backend unreachable since');
    await page.getByTestId('issues-button').click();
    await expect(page.getByTestId('issues-list')).toContainText('fallback: cached copy from');
    await page.keyboard.press('Escape');
    await expect(page.getByTestId('issues-list')).toHaveCount(0);

    await page.unroute('**/api/**');
    await page.getByTestId('registry-stale').getByRole('button', { name: 'Retry' }).click();
    await expect(page.getByTestId('registry-stale')).toHaveCount(0);
  });

  test('unreadable cached product: the layer reports an error, the rest of the map keeps working', async ({ page }) => {
    await open(page);
    await page.route('**/api/facilities/sierrita/matrices?layer=sai*', (route) =>
      route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"Cached product unreadable: Sierrita_coupled.zarr"}' }),
    );
    await page.getByTestId('facility-select').selectOption('sierrita');
    await expect.poll(async () => (await layerIds(page)).includes('matrix-anomaly_regime')).toBe(true);
    await page.getByTestId('toggle-sai').check();
    await expect(page.getByTestId('status-sai')).toHaveText('error');
    expect(await layerIds(page)).toContain('matrix-anomaly_regime');
    await page.getByTestId('issues-button').click();
    await expect(page.getByTestId('issues-list')).toContainText('503 Cached product unreadable');
  });
});
