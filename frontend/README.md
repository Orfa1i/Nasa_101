# LithoCreep frontend

Next.js (pages router) + TypeScript + Tailwind CSS map viewer and inspector for the LithoCreep
screening API. deck.gl renders 3D terrain, facility footprints and the backend's raster matrices over a
Mapbox GL basemap (with a token) or MapLibre GL (without); Recharts draws the inspector panels.

## Run

```bash
# backend (from backend/): real products, or the SYNTHETIC demo site
python scripts/build_demo_cache.py --data-dir /tmp/lithocreep-demo
DATA_DIR=/tmp/lithocreep-demo uvicorn app.main:app --port 8000

# frontend
cd frontend
npm ci
npm run build && npm start        # or: npm run dev
```

The browser calls same-origin `/api/*`; `next.config.js` proxies it to `LITHOCREEP_API_URL`
(default `http://127.0.0.1:8000`). See `.env.example` for the optional Mapbox token
(`MAPBOX_ACCESS_TOKEN`, inlined at build time) and basemap style override.

## Test

```bash
npm run typecheck && npm run lint
npm test                           # Vitest + Testing Library (jsdom, API fixtures)
npm run build && npm run check:bundle   # first-load budget, code-splitting, no bundled credentials
E2E_PYTHON=/path/to/venv/bin/python E2E_DATA_DIR=/tmp/lithocreep-demo npm run test:e2e
```

The Playwright tests start the real backend and `next start`, block all external hosts and serve
synthetic terrain tiles, so they run offline. Chromium renders WebGL2 through SwiftShader;
set `PW_CHROMIUM_PATH` to use a pre-installed Chromium.

## Layout

| Path | Purpose |
|------|---------|
| `src/components/MapViewer.tsx` | deck.gl map: TerrainLayer, facility GeoJsonLayer, matrix BitmapLayers, camera fly-to, picking |
| `src/components/MapHud.tsx` | Provenance watermark, data-quality panel (RMSE / coherence), legend |
| `src/components/Sidebar.tsx` | Facility picker and tabs: Layers (toggles, OPERA timeline), Inspector, Scenarios, Audit; issues log |
| `src/components/ScenarioCtrl.tsx` | Brumadinho historical backtest and the SIMULATED stress-test simulator (Arizona facilities) |
| `src/components/AuditAgent.tsx` | Compliance Copilot: run audits, status, GISTM-aligned report, Export PDF / Print, JSON |
| `src/components/ErrorBoundary.tsx`, `src/hooks/useApiHealth.ts`, `src/utils/issues.ts` | Crash containment, backend health polling, issues log (outage fallbacks) |
| `src/components/Inspector.tsx` | Double Inspector (Recharts): coordinate picker; spectroscopy, kinematics (GP bands, Fukuzono trend) and hydrology (D_crest, GISTM checks) panels |
| `src/components/Basemap.tsx` | Mapbox GL / MapLibre GL basemap (code-split) |
| `src/hooks/useDashboard.ts` | Dashboard state and API loading |
| `src/utils/` | API client and types, colour scales, matrix rendering and masks, layer construction, provenance |
