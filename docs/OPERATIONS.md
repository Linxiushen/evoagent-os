# Operations Guide

Status: v0.1 single-host reference operations  
Last reviewed: 2026-08-10

This guide describes the repository's development topology. It is not a substitute for a platform-specific runbook.

## Reference topology

| Service | Port | Liveness/readiness | Durable paths |
| --- | ---: | --- | --- |
| Control plane | 8800 | `GET /health` | `/state/control.sqlite`, embedded Runtime/Fleet stores, workspace, artifacts and registry |
| Runtime | 8811 | `GET /health` | `/state/runtime.sqlite`, `/workspace` |
| Forge | 8822 | `GET /health` | `/registry` |
| Fleet | 8833 | `GET /health` | `/state/fleet.sqlite`, `/state/artifacts` |
| HarnessLab | 4318 | `GET /healthz` | Exported trace artifacts if mounted |
| EchoWeave | 8765 | `GET /api/health/live`, `GET /api/health/ready` | Persona/consent state and optional runtime cache |

The default Compose profile starts the integrated control plane and HarnessLab. Standalone Runtime/Fleet/Forge and EchoWeave use optional profiles and independent volumes. Every published port binds to `127.0.0.1`. A successful liveness response proves that a process is responding, not that every external model or worker dependency is ready.

## Start and verify

```bash
cp deploy/.env.example deploy/.env
# Replace all CHANGE_ME values.
docker compose --env-file deploy/.env -f deploy/compose.demo.yml up --build -d
docker compose --env-file deploy/.env -f deploy/compose.demo.yml ps
```

Verify the core services:

```bash
curl --fail http://127.0.0.1:8811/health
curl --fail http://127.0.0.1:8822/health
curl --fail http://127.0.0.1:8833/health
curl --fail http://127.0.0.1:4318/healthz
curl --fail http://127.0.0.1:8765/api/health/ready
```

Use `docker compose ... logs --since 10m <service>` for startup diagnosis. Do not paste logs into a public issue until credentials, prompt content, identity data and private URLs have been removed.

## State ownership and backup

Runtime, Fleet and Forge use SQLite in the reference topology. Copying only a live `.sqlite` file while WAL writes are active can produce an inconsistent backup.

Preferred order:

1. Quiesce new requests and wait for in-flight work to reach a known state.
2. Stop the owning service or use SQLite's online backup API from a trusted maintenance job.
3. Snapshot the database together with its owned artifact/workspace volume.
4. Encrypt the backup and record the application commit, schema owner and SHA-256 digest.
5. Restore into an isolated environment and run health plus representative read checks.

Back up Forge public trust metadata and registry artifacts. Private signing keys need a separate, access-controlled key-management and recovery process; do not store them in the registry backup.

## Restore validation

After restoration:

- Runtime: list recent runs, verify session history and inspect pending approvals before resuming schedules.
- Fleet: inspect non-terminal workflows, active/expired leases and artifact hashes. Do not blindly replay leased nodes.
- Forge: query releases and verify a sampled artifact against its expected digest and trusted public key.
- HarnessLab: verify retained baseline traces before accepting new candidate runs.
- EchoWeave: verify consent state, persona allowlist and token rotation before accepting real-person sessions.

## Safe upgrades

1. Read component changelogs and pin the target commit/image digest.
2. Back up state and export a Trace Contract baseline for critical workflows.
3. Upgrade an isolated copy and run the full Python 3.11/3.12 test matrix.
4. Run representative offline demos and compare traces.
5. Deploy one instance, verify health and inspect error/latency trends.
6. Roll back code and state together if a schema migration is not backward-compatible.

v0.1 has no universal database migration coordinator. Inspect each service before upgrading persisted state.

## Operational signals

Minimum signals to collect in a real deployment:

- Request count, error rate and latency by service/route
- Runtime run states, approval queue age, provider failures and schedule lag
- Fleet queued/leased/failed nodes, lease expiry rate, attempts, declared tokens/cost and worker heartbeat age
- Forge scan blocks, verification failures and signing-key fingerprints used for release
- HarnessLab invariant violations and protocol/content fingerprint changes
- EchoWeave connection readiness, queue pressure, cancellation, first-token and end-to-end latency
- Disk usage, SQLite WAL growth, backup age and restore-test result

Repository metrics are not yet a full OpenTelemetry implementation. Integrators should add correlation IDs at the edge and carry them through worker/model calls.

## Initial SLO worksheet

The following are measurement prompts, not v0.1 promises:

| Journey | Candidate indicator | Baseline needed before target |
| --- | --- | --- |
| Submit durable run | Accepted requests / valid requests | Seven days on the intended host/store |
| Complete workflow node | Terminal nodes without infrastructure failure | Representative worker mix and side-effect policy |
| Approval response | Age of oldest actionable approval | Actual operator coverage hours |
| Realtime turn | p50/p95/p99 first token and end-to-end latency | Exact ASR/LLM/TTS/avatar models and hardware |
| Recover state | Successful restore exercises | At least two isolated restore tests |

## Incident playbooks

### Suspected credential exposure

1. Revoke and rotate the credential at its source.
2. Stop affected ingress/worker paths if use is ongoing.
3. Preserve restricted audit evidence without copying the secret further.
4. Search Git history, CI logs, traces and artifacts for exposure scope.
5. Redeploy with new credentials and document the prevention action.

### Stuck or duplicate Fleet work

1. Inspect workflow/node state and event sequence.
2. Confirm lease token and expiry; do not accept a late completion manually.
3. Check whether an external side effect already occurred using its idempotency key.
4. Requeue only after the side effect is reconciled.
5. Preserve the worker and workflow evidence for a regression case.

### Unexpected tool execution

1. Disable the tool/Skill or isolate the worker.
2. Preserve the run, approval and event records.
3. Determine whether risk classification or approval ordering failed.
4. Add a HarnessLab invariant/regression fixture before re-enabling it.
5. Report a security issue privately if authorization was bypassed.

### Persona consent withdrawal

1. Revoke the persona consent record immediately.
2. Block new sessions and invalidate outstanding session tokens.
3. Stop downstream media workers from using the affected references.
4. Apply the documented retention/deletion policy to authorized material and derived artifacts.
5. Record the completed actions without retaining unnecessary biometric data.

## Production gaps

Before production, add enterprise authentication/authorization, workload identity, trusted usage metering, distributed rate limits, shared transactional state, object storage, external audit retention, secret management, image signing/SBOMs, tested migrations, capacity plans and platform-specific disaster recovery.
