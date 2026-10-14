# LithoCreep

Satellite screening of mine tailings facilities. It combines three sources:

* NASA EMIT spectroscopy (seepage alteration);
* OPERA DISP-S1 InSAR kinematics (creep and inverse-velocity trends);
* OPERA DSWx hydrology (pond-to-crest setback).

From these it builds a 4-regime hazard classification, a FastAPI service, a Claude-based GISTM audit copilot, and a Next.js / deck.gl dashboard.

> **Screening only.** Outputs indicate where to look. They are not structural safety certifications, failure forecasts or compliance determinations. Trend-change dates are statistical extrapolations with 95 % bounds and run late in tests.

| Path | What |
|------|------|
| `backend/` | Python 3.11 pipelines (`app/pipelines`), API (`app/api`), audit agent (`app/agent`), CLI (`app/main.py`), tests |
| `frontend/` | Next.js 14 + TypeScript + Tailwind map, inspector, scenarios and copilot UI (see `frontend/README.md`) |
| `data/` | Facility registry (`global_tailings.geojson`) and spectral reference files; pipeline outputs go to `DATA_DIR/cache` (not committed) |
| `scripts/` | `setup.sh`, `run_demo.sh`, `verify.sh` |
| `IMPLEMENTATION_STATUS.md` | Phase-by-phase record: what was built, how it was verified, known gaps |
| `DECISIONS.md` | Design decisions |

## Quick start (local)

Requirements: Python 3.9–3.11 (duckdb 0.9.2 has no 3.12 wheel) and Node ≥ 18.18.

```bash
scripts/setup.sh       # backend/.venv with the exact pins, frontend npm ci, .env templates
scripts/run_demo.sh    # SYNTHETIC demo site -> http://127.0.0.1:3000 (API on :8000)
scripts/verify.sh      # every check CI runs (SKIP_E2E=1 to skip the browser tests)
```

The demo uses a **synthetic** ~1 km site inside the Sierrita bbox. The demo Brumadinho CSV is a labelled synthetic stand-in. The map watermark, the backtest badge and every API `source` field say so.

## Real data

```bash
cd backend && source .venv/bin/activate
export EARTHDATA_USERNAME="your_nasa_username"
export EARTHDATA_PASSWORD="your_nasa_password"
python app/main.py ingest --facility Sierrita --bbox -111.12 31.83 -111.04 31.90
python app/main.py unmix --facility Sierrita
python app/main.py kinematics --facility Sierrita --download
python app/main.py hydrology --facility Sierrita --download   # needs a crest line: DATA_DIR/reference/Sierrita_crest.geojson
python app/main.py couple --facility Sierrita
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Then `cd frontend && npm run build && npm start`. The browser calls `/api/*`, which Next.js proxies to `LITHOCREEP_API_URL` (default `http://127.0.0.1:8000`).

* **Brumadinho backtest.** OPERA DISP-S1 does not cover South America. Supply the published pre-collapse InSAR point series as `DATA_DIR/brumadinho_historic.csv`, with columns `date, displacement_mm[, point_id, temporal_coherence, longitude, latitude, zone, source]`. It is not bundled, and the app does not reconstruct it.
* **Optional credentials.** All are read from the environment only:
  * `MAPBOX_ACCESS_TOKEN` for the Mapbox basemap and terrain; otherwise MapLibre GL and open tiles are used.
  * `ANTHROPIC_API_KEY` for the Claude audit copilot; otherwise the deterministic template is used.

## Checks

* **Backend:** `cd backend && pytest`. This covers pipelines, API, agent and scenarios, plus a repository secrets scan and data-boundary tests.
* **Frontend:**
  * `npm run typecheck && npm run lint && npm test && npm run build && npm run check:bundle`;
  * browser tests: `E2E_PYTHON=… E2E_DATA_DIR=… npm run test:e2e`.
* **CI:** `.github/workflows/ci.yml` runs all of the above on every push.
