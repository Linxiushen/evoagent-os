# ADR 0003: Explicit governance before autonomy

- Status: Accepted
- Date: 2026-08-10

## Context

Model output is untrusted and non-deterministic. Agent systems also execute external side effects, distribute work to workers, install executable Skills and evolve prompts. If those decisions are implicit inside model prose, operators cannot reliably bound cost, reconstruct execution or stop unsafe change.

## Decision

Represent consequential control decisions as typed, persisted transitions:

- Risky Runtime tools and selected Fleet nodes pause for an explicit approval decision.
- Fleet work is committed only under an active expiring lease and declared node budget.
- Skill releases use deterministic bytes, scan/evaluation gates and optional Ed25519 signatures.
- Prompt changes are candidates that require comparative evaluation and explicit promotion.
- Agent behavior is projected into executable Trace Contracts for regression review.

## Consequences

- Operators can inspect and test policy paths separately from model prose.
- Offline fixtures can verify governance behavior without a model key.
- More states and recovery paths must be designed and operated.
- Approval can become a throughput bottleneck without clear risk classification and staffing.
- Signatures, scanners and traces remain evidence, not proofs of safety.

## Alternatives considered

- **Model decides its own policy:** flexible but circular; the untrusted actor controls the boundary.
- **Approve every action:** safer for some contexts but unusable at scale and encourages rubber-stamping.
- **Post-hoc logs only:** useful for diagnosis but cannot prevent an irreversible action.
