# EvoAgent Fleet

[![CI](https://github.com/Linxiushen/evoagent-fleet/actions/workflows/ci.yml/badge.svg)](https://github.com/Linxiushen/evoagent-fleet/actions/workflows/ci.yml) [![Release](https://img.shields.io/github/v/release/Linxiushen/evoagent-fleet)](https://github.com/Linxiushen/evoagent-fleet/releases) [![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

**A durable control plane for teams of specialized AI agents.**

[中文文档](README.zh-CN.md) | [Architecture](docs/architecture.md) | [Operations](docs/operations.md) | [Paper](paper/PAPER.md)

EvoAgent Fleet coordinates real work across processes and machines. It validates DAGs, queues ready nodes, matches declared capabilities, issues expiring leases, enforces token/cost budgets, retries failures, gates consequential nodes on human approval, stores content-addressed artifacts, emits an event ledger and learns routing scores from observed success, quality, cost and latency.

It is not a group-chat abstraction. A worker can disappear, retry, reconnect or be replaced without losing workflow state.

## Quick start

```bash
pip install -e ".[dev]"
evoagent-fleet demo --state-dir .demo-fleet
evoagent-fleet serve --state-dir .demo-fleet --port 8833
```

Open `http://127.0.0.1:8833` for the operations console and `/docs` for the worker API.

## Control flow

```text
submit DAG -> unblock roots -> capability match -> lease -> heartbeat
                                                -> completion + artifacts -> unblock children
                                                -> retry / terminal failure
                                                -> approval before consequential node
```

## Worker contract

```python
from evoagent_fleet.models import Completion, WorkerRegistration
from evoagent_fleet.worker import Worker


async def handle(lease):
    # Honor lease["budget"] and return evidence as artifacts.
    return Completion(output={"answer": "verified"}, artifacts={"report.md": "..."})


worker = Worker(
    orchestrator,
    WorkerRegistration(worker_id="researcher-01", capabilities=["web.research"], pool="reasoner"),
    handle,
)
await worker.run_forever()
```

Remote workers use `POST /v1/claims`, `/v1/heartbeats`, `/v1/completions` and `/v1/failures` and therefore do not share Python memory with the control plane.

## Evaluation-driven routing

Each worker pool accumulates runs, successes, mean quality, cost and latency. Fleet computes an inspectable route score rather than letting a language model silently choose its own executor. The reference scheduler first enforces capabilities and concurrency; production extensions can rank eligible pools by this score and use shadow evaluations before promotion.

## Reliability boundary

Node completion is accepted only for an active lease token. Expired leases are requeued up to `max_attempts`; late workers cannot commit results. Completion is rejected when declared token or dollar budgets are exceeded. Artifacts are SHA-256 addressed and event transitions are append-only.

## Development

```bash
ruff check . && ruff format --check . && pytest -q
```

Apache-2.0.
