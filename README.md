# EvoAgent OS

EvoAgent OS is a governed operating system for durable, auditable, and self-evolving agent teams.

The repository unifies durable sessions, multi-agent workflow orchestration, signed skill distribution,
trace-contract regression testing, and consent-first realtime agents behind one control plane.

> Status: active v0.1 development. The first release targets a complete offline demonstration path and
> production-oriented contracts; it does not claim feature parity with every enterprise automation suite.

## Capabilities

- Durable agent sessions with memory, tools, approvals, schedules, and event replay
- DAG orchestration with leases, retries, budgets, adaptive routing, and content-addressed artifacts
- Reproducible skill packages with static scanning, Ed25519 signatures, and evaluation gates
- Executable trace contracts for lifecycle, tool, approval, and adapter regressions
- Consent-first realtime voice and avatar orchestration
- Unified operations console and deterministic offline demo

## Repository layout

```text
apps/control-plane/     unified API and operations console
apps/worker/            reference fleet worker
packages/contracts/     shared API and event contracts
services/runtime/       durable single-agent runtime
services/fleet/         multi-agent orchestration
services/forge/         trusted skill supply chain
services/observability/ trace contracts and regression gates
services/realtime/      voice and avatar runtime
examples/               end-to-end scenarios
docs/                   architecture, operations, and security
```

The detailed quickstart will be added with the integrated control plane.

## License

Apache License 2.0. Components imported from the original EvoAgent projects retain their history and
license notices. HarnessLab remains available under its original MIT license inside its subtree.
