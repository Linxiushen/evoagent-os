# Governed Evolution in a Local-First Agent Runtime

## Abstract

Long-lived agents require two control loops: an execution loop that performs work and an evolution loop that changes future behavior. Combining them without governance creates silent drift and unsafe side effects. EvoAgent Runtime is a reference architecture that separates these loops through versioned prompts, comparative scenario evaluation, safety invariants and explicit promotion, while providing persistent sessions, typed tools, approvals and an audit ledger.

## Design

For candidate policy `c` and baseline `b`, the evaluator uses the same scenario set. A candidate passes when `score(c) >= score(b) + delta` and all safety predicates hold. Promotion creates a new version with a parent pointer; it never edits the live policy in place. Execution snapshots a version per run, so concurrent promotion cannot change an in-flight trace.

The reference scorer combines scenario-required behavior and structural prompt signals. It is intentionally replaceable: real deployments should use held-out tests, deterministic verifiers, human pairwise labels or process reward models. The contribution is the lifecycle and evidence model, not a universal reward function.

## Relation to prior work

Reflexion and Voyager show the value of feedback and reusable skills; DGM, ADAS and AFlow explore search over agent structures; GEPA uses reflective prompt evolution; OpenClaw demonstrates the operational value of a durable local Gateway. EvoAgent Runtime focuses on the missing deployment boundary between an optimization result and live agent behavior.

## Limitations

The implementation is single-node, prompt evolution is conservative, model calls are not reproducible across providers, and tool execution is process-local. These choices keep the control flow inspectable. Future work includes signed policy artifacts, shadow traffic, sequential tests, container executors and distributed run claims.

