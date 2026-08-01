# EchoWeave operations and SLO guide

This guide defines the production signals, service-level objectives, benchmark
procedure and incident actions for the EchoWeave gateway and its model workers.
The numbers below are starting objectives, not benchmark claims. Establish a
seven-day staging baseline on production-shaped hardware before committing them
to customers.

## Signal model

`echoweave.observability` is a standard-library-only foundation for structured
process metrics and dependency health:

```python
from echoweave.observability import HealthStatus, Observability

telemetry = Observability()
telemetry.health.register("runtime", required=True, stale_after_seconds=60)

telemetry.metrics.increment("gateway.sessions.started")
with telemetry.metrics.timer(
    "echoweave.stage_latency",
    labels={"component": "llm_first_token", "outcome": "success"},
):
    call_model()

telemetry.health.record(
    "runtime",
    HealthStatus.HEALTHY,
    metadata={"readiness_scope": "adapter_construction"},
)
structured_snapshot = telemetry.snapshot()
```

Snapshots are deterministic JSON-compatible dictionaries. A metrics snapshot
contains lifetime counter totals, gauges, uptime, and bounded latency samples.
Latency `count`, `sum_ms`, `min_ms` and `max_ms` are lifetime values; p50/p95/p99
are calculated from the most recent `sample_count` observations. The default is
2,048 samples per series and 512 total series, so telemetry cannot grow without
bound. Export or log snapshots on a fixed interval, ideally every 15 seconds.

The library deliberately has no process-global singleton and no network
listener. Each gateway or worker owns one registry. In a multi-process
deployment, attach a registry at process startup and aggregate snapshots in the
logging/metrics backend. Do not sum gauges or percentile values across
processes; aggregate raw counter deltas and histogram observations in the
backend.

The gateway exposes three deliberately different surfaces:

- `/api/health` and `/api/health/live` are process liveness only;
- `/api/ready` and `/api/health/ready` return 200 only when the selected runtime
  adapters constructed, and 503 otherwise;
- `/api/metrics` returns the bounded `MetricRegistry` JSON snapshot. It requires
  `Authorization: Bearer <ECHOWEAVE_ACCESS_TOKEN>` when a token is configured,
  and otherwise accepts only a fully loopback request.

Every surface has `Cache-Control: no-store`. Runtime readiness is intentionally
reported with `readiness_scope=adapter_construction` and
`dependency_reachability=not_probed`; it is not proof that remote model workers
can complete inference. Never expose model URLs, exception strings, tokens,
prompts, transcripts, persona IDs or biometric asset paths in health or metrics.

### Recommended metric series

Use low-cardinality labels such as `backend`, `stage`, `outcome`, `codec`, and a
small fixed `error_class`. Never use session IDs, user IDs, request IDs, prompt
text, transcript text or exception messages as labels.

The 0.2 gateway currently emits these series:

| Metric | Type | Labels / meaning |
|---|---|---|
| `gateway.sessions.total` | counter | WebSocket sessions accepted by the gateway |
| `gateway.sessions.active` | gauge | Current accepted WebSocket sessions |
| `gateway.sessions.started` | counter | Sessions completing authorization/runtime start |
| `gateway.sessions.rejected` | counter | fixed `reason`: capacity, origin, authorization, etc. |
| `gateway.errors.total` | counter | fixed normalized error `category` |
| `gateway.transport_failures.total` | counter | fixed `reason`: slow client, send timeout, disconnect |
| `gateway.send_timeouts.total` | counter | outbound WebSocket sends exceeding their budget |
| `echoweave.stage_latency` | latency | fixed `component` and `outcome` for VAD/ASR/LLM/TTS/avatar/turn |
| `echoweave.events` | counter | fixed backpressure/cancellation `component` and `outcome` |

The session engine also emits a private `turn.metrics` WebSocket event to the
current client with `first_token_ms`, `text_complete_ms`, `end_to_end_ms` and
`speech_queue_capacity`. It is not included in `/api/metrics` as a per-session
series because session IDs would create unbounded cardinality.

Recommended additions in the deployment telemetry layer include first playable
audio, barge-in latency, audio seconds, worker queue depth and declared fallback
counters. Do not claim these are present in the built-in registry until they are
wired and tested.

Built-in stage latency uses the fixed outcomes `success`, `error` and `timeout`;
built-in backpressure/cancellation events use `wait`, `requested` and `timeout`.
For deployment-added request metrics, define another small fixed vocabulary such
as `ok`, `rejected`, `cancelled`, `timeout`, `dependency_error` and
`internal_error`. Keep HTTP status, WebSocket close code and model-specific
details in structured log fields rather than labels.

### Health semantics

The gateway registers the runtime as a required health component. Deployments
should additionally probe the consent authority, ASR, LLM and TTS with cheap,
bounded checks. Register avatar rendering as optional only when audio-only
fallback is an intentional, disclosed product behavior.

- `healthy`: the latest bounded probe succeeded.
- `degraded`: usable, but slower or on an approved fallback.
- `unhealthy`: an explicit dependency failure.
- `unknown`: never checked or the last check is stale.

Aggregate `ready` is false when a required component is unknown, stale, or
unhealthy. A required explicit failure makes aggregate status `unhealthy`;
staleness or an optional failure makes it `degraded`. Health metadata accepts
only bounded JSON scalars and rejects names that look like credentials.

Health probes must be cheap and bounded. Use worker `/health` endpoints or a
small non-generating model readiness probe; do not synthesize cloned speech or
render a face every few seconds. Set probe timeout below the probe interval and
`stale_after_seconds` to at least twice the interval. Keep an external synthetic
turn separate from liveness/readiness so model degradation cannot cause a
restart loop.

## Initial service-level objectives

Measure over a rolling 28-day window, split by production region and backend
configuration. Exclude announced maintenance and requests rejected before
authentication. Do not exclude internal failures, dependency failures, degraded
fallbacks, or capacity shedding.

| SLI | Initial SLO | Measurement |
|---|---:|---|
| Session-start availability | 99.9% | authorized sessions reaching `session.ready` / valid start attempts |
| Turn success | 99.0% | turns reaching final `session.state=listening` without `error` / submitted turns |
| First text token latency | p95 <= 1.5 s, p99 <= 3.0 s | text submission (or `asr.final`) to first `assistant.delta` |
| First playable audio latency | p95 <= 2.5 s, p99 <= 5.0 s | turn start to first kind-2 audio packet |
| Text-turn completion | p95 <= 8.0 s | text submission to final listening state, using a fixed <= 120-character corpus |
| Barge-in response | p95 <= 250 ms | `vad.speech_started` to `playout.clear` observed by the client |
| Consent enforcement | 100% | unauthorized, expired, withdrawn, or hash-mismatched personas rejected |

Long answers dominate completion time, so completion SLO comparisons are valid
only for the versioned benchmark corpus. Report audio turns separately: endpoint
detection and ASR add latency that text turns do not include. Report SoulX video
and audio-only fallback separately rather than averaging them.

### Error-budget policy

A 99.9% monthly availability objective has roughly 40 minutes of error budget in
28 days. Alert on burn rate, not isolated errors:

- page when the 1-hour burn rate is above 14.4x and the 5-minute rate confirms;
- create a high-priority ticket when the 6-hour burn is above 6x;
- freeze risky releases after 50% of the monthly budget is consumed;
- require incident review and capacity/remediation work after 100% consumption.

For latency, alert when both p95 breaches for 15 minutes and at least 100 samples
exist. Low-volume percentiles are not stable enough to page on.

## Benchmarking

Run the benchmark against an isolated staging stack with the same GPU class,
model revisions and worker limits as production. It uses real WebSocket sessions
and can generate billable DeepSeek traffic. Put the token in the environment so
it does not appear in shell history:

```powershell
$env:ECHOWEAVE_ACCESS_TOKEN = "<at-least-32-byte-staging-token>"
.\.venv\Scripts\python.exe scripts\benchmark_realtime.py `
  --url ws://127.0.0.1:8765/ws `
  --workers 4 --turns 10 `
  --message "请用一句话说明你是一个 AI 数字人。" `
  --output runtime\benchmark-baseline.json
```

The report includes connection-to-ready, first-token, text-final and full-turn
p50/p95/p99, successful turns per second, degraded events and bounded error
classes. The session token and server error messages are never reported. Exit
code 0 means `--min-success-rate` (default 0.99) was met, 1 means it was missed,
and 2 means setup or connection failed.

Increase load in steps (1, 2, 4, 8, ... workers), allow model caches to warm up,
and hold each level for at least ten minutes. Stop increasing load when one of
these occurs: GPU memory pressure, queues remain saturated, p95 exceeds the SLO,
errors exceed 1%, or throughput stops increasing. The last healthy level is not
the safe production limit; apply at least 30% headroom.

### Fault injection

All injection is off by default. Use only on a disposable staging deployment:

```powershell
.\.venv\Scripts\python.exe scripts\benchmark_realtime.py `
  --workers 4 --turns 20 `
  --inject-delay-ms 250 `
  --inject-invalid-rate 0.05 `
  --inject-disconnect-rate 0.10 `
  --seed 42
```

- delay is included in reported turn latency;
- invalid-message injection sends malformed control JSON before a valid turn and
  verifies that the connection can continue;
- disconnect injection closes and re-establishes a session before the next turn,
  exercising cleanup and admission paths;
- the seed makes selection repeatable, but concurrency and model output remain
  nondeterministic.

Never use `--insecure-tls` outside an isolated staging environment. Never target
production with malformed messages or connection churn without an approved
game-day plan, rollback owner, traffic ceiling and customer-impact window.

## Alert and incident runbook

1. Confirm scope: one process, one worker, one model revision, one region, or all
   traffic. Check sample count and deployment timestamps before trusting p99.
2. Protect consent and disclosure first. If authorization or AI watermarking is
   uncertain, stop new sessions instead of degrading around the control.
3. If session starts fail, inspect gateway saturation, consent-state access and
   worker readiness. Do not log supplied credentials or raw manifests.
4. If first-token latency rises, compare DeepSeek latency, outbound network and
   LLM concurrency. Shed excess sessions before queues grow without bound.
5. If first-audio latency rises, inspect VoxCPM queue depth, GPU memory, reference
   audio validation and synthesis real-time factor.
6. If only avatar latency rises, switch to the explicitly approved audio/client
   lip-sync fallback and record `degraded.total`; do not silently remove the AI
   disclosure watermark.
7. If ASR errors rise, preserve a count by fixed error class only. Audio and
   transcripts are sensitive and must not be attached to incidents by default.
8. Roll back the application/model revision when symptoms correlate with a
   release. Preserve the consent state volume; it is not an ordinary cache.
9. After stabilization, save the structured benchmark, deployment/model hashes,
   UTC timeline and aggregate metrics. Rotate any credential that entered logs or
   shell history.

## Capacity and release checklist

- Pin the application image and every model revision by immutable digest.
- Benchmark cold start and warmed steady state separately.
- Verify worker concurrency limits and gateway admission limits under overload.
- Ensure metrics snapshots reach the backend and stale health changes readiness.
- Exercise dependency timeout, worker restart and client disconnect paths.
- Confirm dashboards contain no prompt, transcript, biometric path or credential.
- Compare candidate and baseline using the same corpus, seed, hardware and load.
- Require zero consent-enforcement failures before promotion.
- Keep benchmark reports in an access-controlled location; even aggregate timing
  and capacity data can be operationally sensitive.
