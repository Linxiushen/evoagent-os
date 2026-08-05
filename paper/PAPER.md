# Capability-Transparent Supply Chains for Evolving Agent Skills

## Abstract

Agent skill ecosystems combine natural-language instructions and executable code, creating risks that traditional prompt registries and ordinary package indexes each only partially address. EvoAgent Forge models a skill as a capability-bearing, testable and signed artifact. It connects static capability inference, reproducible packaging, immutable distribution and evaluation-gated evolution.

## Method

An author declares capabilities `D`; static analysis infers a conservative set `I`. Packaging is blocked when `I - D` is non-empty at high severity or when credential/dynamic-code findings occur. The artifact includes canonical metadata and deterministic file ordering. Detached Ed25519 signatures bind its digest to a key; content addressing prevents registry ambiguity.

Evolution creates a new artifact only when a candidate passes executable regression cases and introduces no new blocking finding. Feedback is evidence for tests, not authority to alter installed code.

## Limitations and future work

Static Python analysis is incomplete and the local evaluator is not process-isolated. Future work includes Sigstore identity, transparency logs, SBOM/provenance attestations, dependency scanning, WASI/container evaluation and compatibility resolution across registries.

