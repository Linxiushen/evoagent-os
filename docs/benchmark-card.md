# Benchmark Card

The included deterministic suite verifies nine control-plane behaviors: stable sessions, memory retrieval, approval pause/resume, approval denial, path traversal rejection, event decoding, risk defaults, evolution gating, bearer authentication and WebSocket handshake.

Run with `pytest -q`. The suite is a regression contract, not a claim of general intelligence. Model quality must be evaluated with task-specific scenarios supplied to `/v1/evolution/candidates/{id}/evaluate`.

Recommended production gates: task success, forbidden-action rate, approval frequency, p95 run latency, tool failure rate, token cost, rollback rate and operator override rate. Compare candidates on identical frozen cases and report confidence intervals for non-deterministic providers.

