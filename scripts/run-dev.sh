#!/usr/bin/env sh
set -eu

PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PORT=${PORT:-8765}

exec "$PROJECT_ROOT/.venv/bin/evoagent-os" --host 127.0.0.1 --port "$PORT" --reload
