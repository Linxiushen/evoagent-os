# ADR 0002: SQLite for the reference runtime

- Status: Accepted for v0.1
- Date: 2026-08-10

## Context

The development preview needs a deterministic offline path, durable state across restarts and minimal infrastructure. Runtime, Fleet and Forge already implement local stores around SQLite. Requiring a database cluster would make the reference demo harder to reproduce and would not by itself validate distributed workflow semantics.

## Decision

Use SQLite WAL and local artifact/workspace directories for the single-host reference topology. Keep service APIs and lease invariants independent from SQLite-specific SQL so a shared transactional backend can be introduced later.

## Consequences

- A clean checkout can run without external infrastructure.
- State is inspectable and backup is straightforward when services are quiesced.
- Horizontal writers, cross-region failover and distributed consensus are not supported.
- Live database backups must include WAL-consistent state.
- Multi-replica production work is gated on a transactional shared-store design and migration tests.

## Alternatives considered

- **PostgreSQL from v0.1:** better multi-writer foundation, but increases onboarding and operational scope before contracts stabilize.
- **In-memory state:** simplest tests, but cannot demonstrate restart durability or approval recovery.
- **External workflow engine:** mature durability, but would hide the explicit lease/budget contract this preview is designed to exercise.
