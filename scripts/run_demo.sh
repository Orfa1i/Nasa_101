#!/usr/bin/env bash
# Launch the full stack locally on the SYNTHETIC demo site (clearly labelled in the UI):
#   backend  http://127.0.0.1:8000  (FastAPI, DATA_DIR=.demo-data)
#   frontend http://127.0.0.1:3000  (Next.js production build, proxies /api to the backend)
# Use DATA_DIR=/path/to/real/data scripts/run_demo.sh to serve real pipeline outputs instead.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/backend/.venv/bin/python"
[ -x "$PY" ] || { echo "Run scripts/setup.sh first"; exit 1; }

DATA_DIR="${DATA_DIR:-$ROOT/.demo-data}"
if [ "$DATA_DIR" = "$ROOT/.demo-data" ]; then
  echo "==> building the SYNTHETIC demo DATA_DIR at $DATA_DIR (first run takes ~30 s)"
  "$PY" "$ROOT/backend/scripts/build_demo_cache.py" --data-dir "$DATA_DIR" --with-synthetic-brumadinho
fi

echo "==> backend on :8000"
(cd "$ROOT/backend" && DATA_DIR="$DATA_DIR" "$PY" -m uvicorn app.main:app --host 127.0.0.1 --port 8000) &
API=$!
trap 'kill $API ${WEB:-} 2>/dev/null || true' EXIT INT TERM

echo "==> frontend build + start on :3000"
cd "$ROOT/frontend"
[ -d .next ] || npm run build
LITHOCREEP_API_URL=http://127.0.0.1:8000 npx next start -p 3000 -H 127.0.0.1 &
WEB=$!
echo "Open http://127.0.0.1:3000  (Ctrl-C stops both servers)"
wait
