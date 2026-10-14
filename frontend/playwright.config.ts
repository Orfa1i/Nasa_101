import { defineConfig, devices } from '@playwright/test';

/**
 * Browser tests against the real stack: `next start` (production build) proxying to the real
 * FastAPI backend.
 *
 *   npm run build
 *   E2E_PYTHON=/path/to/venv/bin/python E2E_DATA_DIR=/tmp/lithocreep-demo npm run test:e2e
 *
 * E2E_DATA_DIR must hold the SYNTHETIC demo products: `python backend/scripts/build_demo_cache.py
 * --data-dir $E2E_DATA_DIR`. Without E2E_PYTHON a backend must already be listening on :8000.
 * External tile requests are blocked in the tests, so they never depend on the network.
 */
const PORT = Number(process.env.E2E_PORT || 3100);
const API_PORT = Number(process.env.E2E_API_PORT || 8000);
const python = process.env.E2E_PYTHON;

export default defineConfig({
  testDir: './e2e',
  timeout: 90_000,
  expect: { timeout: 20_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['list']],
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: 'retain-on-failure',
    launchOptions: {
      executablePath: process.env.PW_CHROMIUM_PATH || undefined,
      // Software WebGL2 so deck.gl renders on headless CI machines without a GPU.
      args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'],
    },
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } } }],
  webServer: [
    ...(python
      ? [
          {
            command: `${python} -m uvicorn app.main:app --host 127.0.0.1 --port ${API_PORT}`,
            cwd: '../backend',
            url: `http://127.0.0.1:${API_PORT}/api/health`,
            env: { DATA_DIR: process.env.E2E_DATA_DIR || '' },
            reuseExistingServer: true,
            timeout: 120_000,
          },
        ]
      : []),
    {
      command: `npx next start -p ${PORT} -H 127.0.0.1`,
      url: `http://127.0.0.1:${PORT}`,
      env: { LITHOCREEP_API_URL: `http://127.0.0.1:${API_PORT}` },
      reuseExistingServer: true,
      timeout: 120_000,
    },
  ],
});
