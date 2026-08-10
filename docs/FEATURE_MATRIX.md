# Feature and Evidence Matrix

Snapshot: 2026-08-10  
Release scope: v0.1 development preview

This document separates implemented behavior from roadmap intent. A checked source path means the behavior is present in the repository; it does not by itself establish production scale, security certification or compatibility with an external product.

## Maturity labels

| Label | Meaning |
| --- | --- |
| Implemented + tested | Source and focused automated tests exist in the component |
| Implemented | Source exists; evidence may be limited to broader component tests or manual paths |
| Demo integration | Works as a local preview; cross-service contract is not stable |
| Planned | Roadmap item, not an available capability |

## Runtime and governance

| Capability | Status | Evidence and qualification |
| --- | --- | --- |
| Persistent sessions/runs/messages | Implemented + tested | [`runtime/store.py`](../services/runtime/src/evoagent_runtime/store.py), [`test_store_and_tools.py`](../services/runtime/tests/test_store_and_tools.py) |
| HTTP and WebSocket gateway | Implemented + tested | [`runtime/api.py`](../services/runtime/src/evoagent_runtime/api.py), [`test_api.py`](../services/runtime/tests/test_api.py) |
| Offline deterministic provider | Implemented + tested | [`runtime/providers.py`](../services/runtime/src/evoagent_runtime/providers.py), component runtime tests |
| OpenAI-compatible model endpoint | Implemented | Provider adapter exists; compatibility depends on the configured endpoint |
| FTS-backed memory | Implemented + tested | Runtime store/tools tests; local SQLite scope |
| Typed tool risk classification | Implemented + tested | [`runtime/tools.py`](../services/runtime/src/evoagent_runtime/tools.py) |
| Approval and resumable high-risk tool call | Implemented + tested | Runtime API/runtime test suites |
| Workspace containment and HTTP allowlist | Implemented + tested | Runtime tools tests; not an OS sandbox |
| Interval schedules | Implemented + tested | Runtime scheduler/API paths; single process reference |
| Feedback-derived prompt candidates | Implemented + tested | [`runtime/evolution.py`](../services/runtime/src/evoagent_runtime/evolution.py), [`test_evolution.py`](../services/runtime/tests/test_evolution.py) |
| Automatic prompt mutation | Intentionally not implemented | Candidate evaluation and explicit promotion are required |

## Multi-agent workflow execution

| Capability | Status | Evidence and qualification |
| --- | --- | --- |
| DAG validation and cycle rejection | Implemented + tested | [`fleet/models.py`](../services/fleet/src/evoagent_fleet/models.py), [`test_orchestrator.py`](../services/fleet/tests/test_orchestrator.py) |
| Capability-matched worker claims | Implemented + tested | [`fleet/orchestrator.py`](../services/fleet/src/evoagent_fleet/orchestrator.py) |
| Expiring lease and heartbeat | Implemented + tested | Fleet orchestrator/API worker tests |
| Retry and late-result rejection | Implemented + tested | Fleet orchestrator tests |
| Per-node token/dollar/time declarations | Implemented + tested | Completion enforces declared token/dollar usage; time is a declared budget, not yet a trusted remote kill switch |
| Approval-gated nodes | Implemented + tested | Fleet orchestrator/API tests |
| Content-addressed artifacts | Implemented + tested | SHA-256 artifacts in Fleet orchestrator tests |
| Route observations | Implemented + tested | Success/quality/cost/latency aggregates are recorded |
| Evaluation-driven automatic routing | Planned | v0.1 records route metrics but does not use the score to choose among eligible workers |
| Trusted worker identity/metering | Planned | Reference registration and usage are not sufficient for hostile multi-tenant workers |

## Skill supply chain

| Capability | Status | Evidence and qualification |
| --- | --- | --- |
| Typed `SKILL.md` manifest | Implemented + tested | [`forge/skill.py`](../services/forge/src/evoagent_forge/skill.py), supply-chain tests |
| Static security scanning | Implemented + tested | [`forge/scanner.py`](../services/forge/src/evoagent_forge/scanner.py) |
| Deterministic `.evoskill` package | Implemented + tested | [`forge/package.py`](../services/forge/src/evoagent_forge/package.py) |
| Ed25519 sign/verify | Implemented + tested | [`forge/signing.py`](../services/forge/src/evoagent_forge/signing.py) |
| Registry search/immutable releases | Implemented + tested | [`forge/registry.py`](../services/forge/src/evoagent_forge/registry.py), registry API tests |
| Executable evaluation cases | Implemented + tested | [`forge/evolution.py`](../services/forge/src/evoagent_forge/evolution.py) |
| Automatic safe installation | Intentionally not implemented | Operators must pin a trusted key, inspect capabilities and sandbox execution |
| Key transparency/revocation service | Planned | Trusted fingerprint distribution is out of band in v0.1 |

## Observability and evaluation

| Capability | Status | Evidence and qualification |
| --- | --- | --- |
| Portable `harnesslab.trace/v1` artifact | Implemented + tested | [`TRACE_CONTRACT.md`](../services/observability/docs/TRACE_CONTRACT.md), trace tests |
| Lifecycle/tool/policy invariants | Implemented + tested | HarnessLab trace-contract and conformance tests |
| Protocol/content fingerprints | Implemented + tested | Volatile values are excluded according to the contract |
| Structural compare with CI exit code | Implemented + tested | HarnessLab CLI/runtime tests |
| SSE run events and workbench | Implemented + tested | HarnessLab API tests |
| DeepSeek Harness compatibility | Not claimed | No public protocol fixture is assumed; adapter boundary is prepared only |
| Full distributed telemetry backend | Planned | v0.1 is a regression harness, not a replacement for metrics/log/trace storage |

## Realtime voice and avatar

| Capability | Status | Evidence and qualification |
| --- | --- | --- |
| Versioned WebSocket/media protocol | Implemented + tested | [`realtime/protocol.py`](../services/realtime/src/echoweave/protocol.py), protocol/JS contract tests |
| Endpointing, interruption and bounded queues | Implemented + tested | Realtime pipeline, VAD and frontend lifecycle tests |
| Adapter-isolated ASR/LLM/TTS/avatar path | Implemented + tested | HTTP adapter and resilience tests |
| Offline synthetic demo | Implemented + tested | No GPU, model key or real-person media required |
| Consent manifest and revocation checks | Implemented + tested | Realtime persona/auth/security tests |
| Real-person authorization verification | Operator responsibility | Software records/enforces supplied consent; it cannot establish legal rights by itself |
| Named-model production latency | Not claimed | Must be benchmarked on the exact model revisions and hardware |

## Integration and production readiness

| Capability | Status | Evidence and qualification |
| --- | --- | --- |
| Unified local Runtime/Fleet/Forge operator console | Implemented + tested | [`apps/control-plane`](../apps/control-plane), [`test_control_plane.py`](../tests/test_control_plane.py); HarnessLab/EchoWeave remain separate services |
| Versioned Python contracts | Implemented + tested | [`packages/contracts`](../packages/contracts) and its schema/model tests |
| Typed Python SDK | Implemented + tested | [`sdk/python`](../sdk/python) |
| TypeScript SDK on platform `fetch` | Implemented + tested | [`sdk/typescript`](../sdk/typescript); Node 20 build and tests run in CI |
| Offline market-research workflow contract | Demo integration | [`examples/market-research`](../examples/market-research) |
| Local Docker Compose topology | Demo integration | [`deploy/compose.demo.yml`](../deploy/compose.demo.yml) |
| Python 3.11/3.12 Ruff + pytest matrix | Implemented | [CI workflow](../.github/workflows/ci.yml) |
| Enterprise SSO/RBAC/tenancy | Planned | Required before a shared production deployment |
| Multi-replica transactional state | Planned | SQLite reference is single-host |
| External tamper-resistant audit sink | Planned | Local events are useful evidence, not immutable external audit storage |
| Published production SLO | Not claimed | Operations guide provides a measurement worksheet only |

## How to verify

Run the CI workflow from a clean checkout. For a focused component:

```bash
python -m pip install -e "services/fleet[dev]"
ruff check services/fleet
ruff format --check services/fleet
pytest -q services/fleet/tests
```

Then run the deterministic end-to-end contract demo described in [`examples/market-research`](../examples/market-research). Passing tests prove the covered behavior under those fixtures, not production readiness outside the documented boundary.
