#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
echo "[1/5] Compile Python modules"
python3 -m compileall -q server tests
echo "[2/5] Run unit and HTTP API tests"
python3 -m unittest discover -s tests -v
echo "[3/5] Check shell script syntax"
bash -n start_nane.sh
echo "[4/5] Check legacy-compatible browser JavaScript syntax"
if command -v node >/dev/null 2>&1; then
    node --check web/js/app.js
    echo "[5/5] Run browser client smoke test"
    node tests/test_ui_runtime.js
else
    echo "Node.js absent; browser JavaScript checks skipped."
fi
echo "All available NANE checks passed."
