#!/usr/bin/env sh
set -eu

: "${EVOAGENT_OS_TOKEN:?Set EVOAGENT_OS_TOKEN to the local control-plane token}"
control_plane_url="${EVOAGENT_OS_URL:-http://127.0.0.1:8800}"

curl --fail-with-body --silent --show-error \
  -X POST "${control_plane_url%/}/api/v1/demo/launch" \
  -H "Authorization: Bearer ${EVOAGENT_OS_TOKEN}" \
  -H "Idempotency-Key: market-research-demo-v1"
printf '\n'
