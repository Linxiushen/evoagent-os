# EvoAgent OS

[![CI](https://github.com/Linxiushen/evoagent-os/actions/workflows/ci.yml/badge.svg)](https://github.com/Linxiushen/evoagent-os/actions/workflows/ci.yml)
[![CodeQL](https://github.com/Linxiushen/evoagent-os/actions/workflows/codeql.yml/badge.svg)](https://github.com/Linxiushen/evoagent-os/actions/workflows/codeql.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-0B6B53)](LICENSE)
[![Status](https://img.shields.io/badge/status-v0.1%20development%20preview-D97706)](docs/ROADMAP.md)

**A local-first control plane for durable, governed agent teams.**

[中文](README.zh-CN.md) | [Architecture](docs/ARCHITECTURE.md) | [Security](docs/SECURITY.md) | [90-second demo](docs/DEMO_SCRIPT_90S.md) | [Feature evidence](docs/FEATURE_MATRIX.md)

EvoAgent OS brings five independently testable agent systems into one repository: a durable runtime, a leased DAG orchestrator, a signed skill supply chain, executable trace-contract regression testing, and a consent-first realtime voice/avatar gateway. The v0.1 control plane composes Runtime, Fleet and Forge into one local operator experience and runs an offline demonstration without a model key; HarnessLab and EchoWeave remain independently deployed services.

> [!IMPORTANT]
> **v0.1 is a development preview.** The component test suites and offline paths are real; the repository has not yet established multi-node production SLOs, enterprise identity federation, or feature parity with broad workplace suites. See [scope and evidence](docs/FEATURE_MATRIX.md) before making deployment or comparison claims.

![EvoAgent OS workflow view with an approval-gated publish node](docs/assets/evoagent-workflow.png)

## Why this exists

An agent that can call tools is easy to demo and difficult to operate. Durable work needs explicit state transitions, bounded execution, approval before consequential actions, provenance for reusable skills, and traces that can fail CI when behavior drifts. EvoAgent OS treats those as first-class control-plane contracts.

```text
request -> durable run or workflow -> capability-matched worker -> approval gate
        -> content-addressed artifact -> trace contract -> regression decision
```

## Implemented surfaces

| Surface | What is implemented in v0.1 | Evidence |
| --- | --- | --- |
| Runtime | Persistent sessions, memory, schedules, typed tools, approval/resume, event ledger, prompt candidates and explicit promotion | [`services/runtime`](services/runtime) |
| Fleet | Validated DAGs, capability matching, expiring leases, heartbeat, retry, budgets, approval nodes, SHA-256-addressed artifacts, route metrics | [`services/fleet`](services/fleet) |
| Forge | Skill manifests, static scanning, deterministic `.evoskill` packages, Ed25519 signing, immutable registry releases and evaluation gates | [`services/forge`](services/forge) |
| Observability | `harnesslab.trace/v1`, lifecycle/tool/policy invariants, stable fingerprints, structural diff, CI exit codes and SSE workbench | [`services/observability`](services/observability) |
| Realtime | Versioned WebSocket protocol, VAD pipeline, interruption, bounded queues, adapter isolation, consent records and synthetic-media disclosure | [`services/realtime`](services/realtime) |
| Operations | Workspace/agent catalog, integrated Runtime/Fleet/Forge state, idempotent demo launch and one local operator console | [`apps/control-plane`](apps/control-plane) |
| Contracts and SDKs | Versioned Pydantic contracts plus typed Python and dependency-free TypeScript clients | [`packages/contracts`](packages/contracts), [`sdk`](sdk) |

These are repository-local service boundaries, not a claim that every capability is exposed through one stable public API. Cross-service contracts remain versioned independently during v0.1.

## Quick start

### Docker Compose

Prerequisites: Docker Engine with Compose v2. GPU models and external model keys are **not** required for the default demo paths.

```bash
cp deploy/.env.example deploy/.env
# Replace all three CHANGE_ME values with independent random tokens.
docker compose --env-file deploy/.env -f deploy/compose.demo.yml up --build
```

PowerShell:

```powershell
Copy-Item deploy/.env.example deploy/.env
# Edit all three CHANGE_ME values before starting the stack.
docker compose --env-file deploy/.env -f deploy/compose.demo.yml up --build
```

The services bind to host loopback by default. The core profile starts the control plane and HarnessLab; standalone components and realtime are opt-in Compose profiles.

| URL | Service | Profile |
| --- | --- | --- |
| `http://127.0.0.1:8800` | Unified control plane | core |
| `http://127.0.0.1:4318` | HarnessLab workbench | core |
| `http://127.0.0.1:8811` | Standalone Runtime console and OpenAPI | `components` |
| `http://127.0.0.1:8833` | Standalone Fleet console and worker API | `components` |
| `http://127.0.0.1:8822` | Standalone Forge registry | `components` |
| `http://127.0.0.1:8765` | EchoWeave fictional realtime demo | `realtime` |

Read [`deploy/README.md`](deploy/README.md) before exposing any port outside localhost.

### Run the integrated deterministic demo

With Python 3.11 or 3.12:

```bash
python -m venv .venv
. .venv/bin/activate  # Windows: .venv\Scripts\activate
python -m pip install -e ".[dev]"
export EVOAGENT_OS_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
evoagent-os --port 8800
```

In another terminal, set the local control-plane token and launch the included offline market-research scenario:

```bash
export EVOAGENT_OS_TOKEN="<your-local-token>"
sh examples/market-research/launch.sh
```

The integrated reference worker completes deterministic research/review fixtures and pauses at `publish` for an operator decision. The fixtures prove orchestration behavior, not research quality. A standalone Fleet request and worker exercise are also available under [`examples/market-research`](examples/market-research).

## Development

Each Python component owns its dependencies and tests. CI runs Ruff and pytest on Python 3.11 and 3.12 for every listed component.

```bash
python -m pip install -e "services/runtime[dev]"
ruff check services/runtime
ruff format --check services/runtime
pytest -q services/runtime/tests
```

The TypeScript SDK requires Node.js 20 and pnpm:

```bash
cd sdk/typescript
pnpm install --frozen-lockfile
pnpm test
```

For the complete matrix, run the GitHub Actions workflow or repeat those commands for the control plane, contracts, Python and TypeScript SDKs, `fleet`, `forge`, `observability` and `realtime`.

## Documentation

- [System architecture and trust boundaries](docs/ARCHITECTURE.md)
- [Threat model](docs/THREAT_MODEL.md)
- [Security and responsible disclosure](docs/SECURITY.md)
- [Operations, backup, recovery and SLO guidance](docs/OPERATIONS.md)
- [Feature matrix and maturity evidence](docs/FEATURE_MATRIX.md)
- [Neutral clean-room comparison with Magic](docs/CLEAN_ROOM_COMPARISON.md)
- [Roadmap](docs/ROADMAP.md)
- [Architecture decisions](docs/adr/README.md)
- [Press kit](docs/PRESS_KIT.md)

## License and provenance

The repository-level work is Apache License 2.0. Imported components retain their original copyright and notices. `services/observability` (HarnessLab) remains MIT-licensed; third-party models and assets retain their own terms. A software license does not grant rights to a person's voice, face, or identity.

Security reports should follow [the private disclosure process](docs/SECURITY.md). General contributions are welcome through focused issues and pull requests.
