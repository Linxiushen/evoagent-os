# ADR 0001: Independent services in one monorepo

- Status: Accepted
- Date: 2026-08-10

## Context

Runtime execution, distributed workflow scheduling, Skill supply-chain operations, trace regression and realtime media have different security boundaries, dependencies and scaling profiles. Combining them into one process would make optional GPU/model stacks part of every deployment and blur ownership of persistent state. Keeping them in unrelated repositories, however, makes end-to-end contracts and release evidence difficult to review.

## Decision

Keep each component independently installable and runnable under `services/`, with a thin operator-facing control plane under `apps/`. The monorepo owns integration documentation, examples, CI and cross-service contracts. A component remains authoritative for its own state and API during v0.1.

## Consequences

- Components can be tested and deployed independently.
- Realtime GPU dependencies do not contaminate control-plane dependency resolution.
- Security and scaling controls can be applied per boundary.
- Cross-service calls can partially fail and are not distributed transactions.
- Versioning and correlation conventions must be made explicit before a stable release.

## Alternatives considered

- **Single modular process:** simpler local startup, but couples failure domains and dependency stacks.
- **Separate repositories only:** clear ownership, but weakens atomic integration changes and discoverability.
- **Submodules:** preserves upstream repositories but creates a less ergonomic contributor and CI workflow.
