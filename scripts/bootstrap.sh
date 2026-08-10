#!/usr/bin/env sh
set -eu

PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PYTHON_BIN=${PYTHON_BIN:-python3}

"$PYTHON_BIN" -m venv "$PROJECT_ROOT/.venv"
VENV_PYTHON="$PROJECT_ROOT/.venv/bin/python"
"$VENV_PYTHON" -m pip install --upgrade pip
"$VENV_PYTHON" -m pip install -e "$PROJECT_ROOT[dev]"
"$VENV_PYTHON" -m pip install -e "$PROJECT_ROOT/packages/contracts"
"$VENV_PYTHON" -m pip install -e "$PROJECT_ROOT/sdk/python"
"$VENV_PYTHON" -m pip install -e "$PROJECT_ROOT/services/runtime"
"$VENV_PYTHON" -m pip install -e "$PROJECT_ROOT/services/fleet"
"$VENV_PYTHON" -m pip install -e "$PROJECT_ROOT/services/forge"
"$VENV_PYTHON" -m pip install -e "$PROJECT_ROOT/services/observability"
"$VENV_PYTHON" -m pip install -e "$PROJECT_ROOT/services/realtime[dev]"

printf 'Environment ready: %s\n' "$PROJECT_ROOT/.venv"
printf 'Run: .venv/bin/evoagent-os --reload\n'
