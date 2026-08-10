# Roadmap

This roadmap describes outcome gates, not staffing or delivery promises. Priorities may change after security review and user feedback.

## v0.1: integrated development preview

Current repository scope:

- [x] Durable local agent sessions, memory, tools, approvals, schedules and event records
- [x] DAG validation, worker capabilities, expiring leases, retries, node budgets and artifacts
- [x] Deterministic Skill packaging, scanning, Ed25519 signing and registry metadata
- [x] Executable Trace Contract projection, diff and conformance checks
- [x] Consent-first realtime gateway with an offline/synthetic demo path
- [x] Local service consoles, Docker Compose example and Python 3.11/3.12 CI matrix
- [x] Versioned Python contracts plus typed Python and TypeScript clients
- [ ] Stable cross-service API/event versioning and generated client SDK
- [ ] Production-grade identity, tenant authorization and workload identity
- [ ] Multi-node durability and supported schema migration policy

Exit criteria for a tagged `v0.1.0`:

1. Clean checkout passes the documented CI matrix on Python 3.11 and 3.12.
2. Docker Compose starts the offline core path with no model key.
3. The market-research contract demo reaches a completed workflow and emits hashed artifacts.
4. Security/threat-model claims match executable controls and documented residual risks.
5. All imported component licenses/notices are retained.

## v0.2: contract convergence

- Versioned shared run, workflow, approval, artifact and event envelopes
- Correlation propagation across control plane, Runtime, Fleet and HarnessLab
- Reference worker adapter that executes Runtime tasks under Fleet leases
- Forge trust-policy file with pinned signer fingerprints and capability policy
- Trace export hooks from Runtime/Fleet into HarnessLab
- Contract compatibility tests and migration fixtures
- Accessible operator flows for pending approvals and failure recovery

## v0.3: deployment foundations

- Authenticated edge integration and role-based policy hooks
- Short-lived workload identity for workers
- PostgreSQL-backed lease/claim reference implementation
- Object-store artifact backend with retention and integrity verification
- OpenTelemetry traces/metrics with documented redaction
- Release SBOM, provenance attestations and signed container images
- Backup/restore automation and schema migration rehearsals
- Rate limits, quotas and trusted provider-side cost accounting

## v1.0 readiness gates

`v1.0` should mean a stable supported contract, not simply more features. Proposed gates:

- Published compatibility and deprecation policy
- Independent security review with resolved critical/high findings
- Reproducible release artifacts and documented supply-chain provenance
- Multi-node failure and recovery tests
- Measured SLOs on named reference infrastructure
- Tenant isolation tests for every stateful API
- Upgrade/rollback tests across two consecutive releases
- At least one production pilot with documented operating feedback

## Research tracks

These are experiments until accompanied by code, tests and evidence:

- Evaluation-driven worker routing with shadow traffic and rollback
- Policy-as-code for tool, Skill and data-access decisions
- A2A/MCP interoperability adapters against published protocol fixtures
- Privacy-preserving memory retention and selective deletion
- Deterministic replay across non-deterministic model providers
- Realtime quality/capacity characterization across explicit GPU profiles

Feature requests should describe the operator problem, trust boundary, failure behavior and acceptance evidence rather than only naming a framework or model.
