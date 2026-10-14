#!/usr/bin/env bash
# One-time local setup: Python backend venv (exact pins) and frontend node_modules.
#   scripts/setup.sh            # uses python3.11 if available, else python3
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${PYTHON:-$(command -v python3.11 || command -v python3)}"

"$PY" - <<'PYCHECK'
import sys
if not ((3, 9) <= sys.version_info[:2] <= (3, 11)):
    sys.exit(f"Python 3.9-3.11 required (duckdb 0.9.2 has no 3.12 wheel); found {sys.version.split()[0]}")
PYCHECK
node -e 'const [a,b]=process.versions.node.split(".").map(Number); if (a<18||(a===18&&b<18)) { console.error("Node >= 18.18 required, found "+process.versions.node); process.exit(1) }'

echo "==> backend: creating backend/.venv with $("$PY" --version)"
"$PY" -m venv "$ROOT/backend/.venv"
"$ROOT/backend/.venv/bin/python" -m pip install --upgrade pip >/dev/null
"$ROOT/backend/.venv/bin/python" -m pip install -r "$ROOT/backend/requirements.txt"

echo "==> frontend: npm ci"
(cd "$ROOT/frontend" && npm ci --no-audit --no-fund)

[ -f "$ROOT/backend/.env" ] || cp "$ROOT/backend/.env.example" "$ROOT/backend/.env"
[ -f "$ROOT/frontend/.env.local" ] || cp "$ROOT/frontend/.env.example" "$ROOT/frontend/.env.local"
cat <<MSG

Setup complete. Credentials are optional and read from the environment / .env files only:
  backend/.env         EARTHDATA_USERNAME, EARTHDATA_PASSWORD (NASA Earthdata downloads)
  frontend/.env.local  MAPBOX_ACCESS_TOKEN (otherwise MapLibre GL + open tiles)
  shell                ANTHROPIC_API_KEY (otherwise the audit uses the deterministic template)

Next: scripts/run_demo.sh   (SYNTHETIC demo site)   or   scripts/verify.sh   (all checks)
MSG
