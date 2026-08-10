#!/usr/bin/env sh
set -eu

PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PYTHON="$PROJECT_ROOT/.venv/bin/python"
cd "$PROJECT_ROOT"

"$PYTHON" -m ruff check apps packages sdk tests
"$PYTHON" -m pytest -q tests
"$PYTHON" -m pytest -q packages/contracts/tests
"$PYTHON" -m pytest -q sdk/python/tests
"$PYTHON" -m pytest -q services/runtime/tests
"$PYTHON" -m pytest -q services/fleet/tests
"$PYTHON" -m pytest -q services/forge/tests
"$PYTHON" -m pytest -q services/observability/tests
"$PYTHON" -m pytest -q services/realtime/tests
if command -v node >/dev/null 2>&1; then
  for test_file in services/realtime/tests/js/*.mjs; do
    node --test "$test_file"
  done
fi
if command -v pnpm >/dev/null 2>&1; then
  (
    cd sdk/typescript
    pnpm install --frozen-lockfile
    pnpm test
  )
fi
