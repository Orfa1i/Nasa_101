/**
 * Responsive layout on standard display sizes: the Tailwind breakpoints put the sidebar beside
 * the map from `lg` (1024 px) up and below it on smaller screens; nothing may overflow or overlap.
 */
import { expect, test, type Locator, type Page } from '@playwright/test';

import { isolateNetwork, layerIds } from './helpers';

const SCREENS = [
  { name: 'full-hd', width: 1920, height: 1080, sideBySide: true },
  { name: 'laptop-1440', width: 1440, height: 900, sideBySide: true },
  { name: 'laptop-1366', width: 1366, height: 768, sideBySide: true },
  { name: 'hd-720', width: 1280, height: 720, sideBySide: true },
  { name: 'tablet-landscape', width: 1024, height: 768, sideBySide: true },
  { name: 'tablet-portrait', width: 768, height: 1024, sideBySide: false },
  { name: 'phone', width: 390, height: 844, sideBySide: false },
];

type Box = { x: number; y: number; width: number; height: number };

const overlapArea = (a: Box, b: Box) =>
  Math.max(0, Math.min(a.x + a.width, b.x + b.width) - Math.max(a.x, b.x)) * Math.max(0, Math.min(a.y + a.height, b.y + b.height) - Math.max(a.y, b.y));

const inside = (inner: Box, outer: Box, tol = 1) =>
  inner.x >= outer.x - tol && inner.y >= outer.y - tol && inner.x + inner.width <= outer.x + outer.width + tol && inner.y + inner.height <= outer.y + outer.height + tol;

async function box(l: Locator): Promise<Box> {
  const b = await l.boundingBox();
  expect(b, 'element has a layout box').not.toBeNull();
  return b!;
}

async function openSierrita(page: Page) {
  await isolateNetwork(page, { terrain: false });
  await page.goto('/');
  await page.getByTestId('facility-select').selectOption('sierrita');
  await expect.poll(async () => (await layerIds(page)).some((l) => l.startsWith('matrix-anomaly_regime'))).toBe(true);
  await page.getByTestId('toggle-sai').check(); // two legends: the tallest common HUD
  await expect(page.getByTestId('legend')).toBeVisible();
  await page.waitForTimeout(2200); // camera fly-to
}

for (const screen of SCREENS) {
  test.describe(`${screen.name} ${screen.width}x${screen.height}`, () => {
    test.use({ viewport: { width: screen.width, height: screen.height } });

    test('layout fits the screen without overflow or overlap', async ({ page }, info) => {
      await openSierrita(page);

      const doc = await page.evaluate(() => ({
        scrollW: document.documentElement.scrollWidth,
        scrollH: document.documentElement.scrollHeight,
        w: window.innerWidth,
        h: window.innerHeight,
      }));
      expect(doc.scrollW, 'no horizontal page scroll').toBeLessThanOrEqual(doc.w);
      expect(doc.scrollH, 'app fills the viewport exactly').toBeLessThanOrEqual(doc.h);

      const viewport = { x: 0, y: 0, width: screen.width, height: screen.height };
      const map = await box(page.getByTestId('map-viewer'));
      const panel = await box(page.locator('#dashboard-panel'));
      expect(inside(map, viewport)).toBe(true);
      expect(inside(panel, viewport)).toBe(true);
      expect(overlapArea(map, panel), 'sidebar and map do not overlap').toBe(0);

      if (screen.sideBySide) {
        expect(panel.x + panel.width).toBeLessThanOrEqual(map.x + 1);
        expect(panel.height).toBeGreaterThanOrEqual(screen.height - 1);
        expect(panel.width).toBeGreaterThanOrEqual(352);
        expect(map.width).toBeGreaterThanOrEqual(screen.width * 0.6);
        await expect(page.getByTestId('panel-toggle')).toBeHidden();
      } else {
        expect(map.y + map.height).toBeLessThanOrEqual(panel.y + 1);
        expect(panel.width).toBeGreaterThanOrEqual(screen.width - 1);
        expect(map.height).toBeGreaterThanOrEqual(screen.height * 0.5);
        await expect(page.getByTestId('panel-toggle')).toBeVisible();
      }

      // Sidebar content wraps instead of clipping sideways.
      const sidebarOverflow = await page.locator('#dashboard-panel').evaluate((el) => el.scrollWidth - el.clientWidth);
      expect(sidebarOverflow).toBeLessThanOrEqual(0);

      // HUD panels stay inside the map and never cover each other.
      const hud: [string, Box][] = [];
      const ids = ['watermark', 'quality-panel', 'legend', 'attribution', ...(screen.sideBySide ? [] : ['panel-toggle'])];
      if (!screen.sideBySide) await expect(page.getByTestId('legend-toggle')).toHaveAttribute('aria-expanded', screen.width >= 640 ? 'true' : 'false');
      for (const id of ids) {
        const l = page.getByTestId(id);
        await expect(l, `${id} visible`).toBeVisible();
        const b = await box(l);
        expect(inside(b, map), `${id} inside the map`).toBe(true);
        hud.push([id, b]);
      }
      for (let i = 0; i < hud.length; i++)
        for (let j = i + 1; j < hud.length; j++) expect(overlapArea(hud[i][1], hud[j][1]), `${hud[i][0]} vs ${hud[j][0]}`).toBe(0);

      // Text stays legible: no HUD or sidebar text below 10 px.
      const smallest = await page.evaluate(() => {
        let min = Infinity;
        for (const el of document.querySelectorAll('[data-testid=sidebar] *, [data-testid=map-viewer] p, [data-testid=map-viewer] li, [data-testid=map-viewer] td'))
          if ((el as HTMLElement).innerText?.trim()) min = Math.min(min, parseFloat(getComputedStyle(el).fontSize));
        return min;
      });
      expect(smallest).toBeGreaterThanOrEqual(10);

      await info.attach(`${screen.name}.png`, { body: await page.screenshot(), contentType: 'image/png' });
    });
  });
}

test.describe('phone controls', () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test('the controls panel collapses to give the map the full screen', async ({ page }) => {
    await isolateNetwork(page, { terrain: false });
    await page.goto('/');
    const before = (await page.getByTestId('map-viewer').boundingBox())!;
    await page.getByTestId('panel-toggle').click();
    await expect(page.locator('#dashboard-panel')).toBeHidden();
    await expect.poll(async () => (await page.getByTestId('map-viewer').boundingBox())!.height).toBeGreaterThan(before.height + 200);
    await page.getByTestId('panel-toggle').click();
    await expect(page.getByTestId('facility-select')).toBeVisible();
  });
});
