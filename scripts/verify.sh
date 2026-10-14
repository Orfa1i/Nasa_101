#!/usr/bin/env bash
# Every verification check, as CI runs them: backend tests (incl. secrets scan and data
# boundaries), frontend typecheck / lint / unit tests / build / bundle check, browser tests.
#   scripts/verify.sh            # all
#   SKIP_E2E=1 scripts/verify.sh # without the Playwright suite
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${E2E_PYTHON:-$ROOT/backend/.venv/bin/python}"

echo "==> backend: pytest"
(cd "$ROOT/backend" && "$PY" -m pytest -q)

echo "==> frontend: typecheck, lint, unit tests, build, bundle check"
cd "$ROOT/frontend"
npm run typecheck
npm run lint
npm test
npm run build
npm run check:bundle

if [ -z "${SKIP_E2E:-}" ]; then
  echo "==> browser tests (Playwright, real backend on the SYNTHETIC demo site)"
  DEMO="${E2E_DATA_DIR:-$ROOT/.demo-data}"
  "$PY" "$ROOT/backend/scripts/build_demo_cache.py" --data-dir "$DEMO" --with-synthetic-brumadinho
  E2E_PYTHON="$PY" E2E_DATA_DIR="$DEMO" npm run test:e2e
fi
echo "All checks passed."
