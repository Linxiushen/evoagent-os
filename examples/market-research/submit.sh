#!/usr/bin/env sh
set -eu

fleet_url="${EVOAGENT_FLEET_URL:-http://127.0.0.1:8833}"
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

curl --fail-with-body --silent --show-error \
  -X POST "${fleet_url%/}/v1/workflows" \
  -H "Content-Type: application/json" \
  --data "@${script_dir}/workflow.json"
printf '\n'
