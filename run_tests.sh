#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
echo "[1/4] Compile Python modules"
python3 -m compileall -q server tests
echo "[2/4] Run unit and HTTP API tests"
python3 -m unittest discover -s tests -v
echo "[3/4] Check shell script syntax"
bash -n start_nane.sh
echo "[4/4] Check legacy-compatible browser JavaScript"
if command -v node >/dev/null 2>&1; then node --check web/js/app.js; else echo "Node.js absent; JS syntax check skipped."; fi
echo "All available NANE checks passed."
