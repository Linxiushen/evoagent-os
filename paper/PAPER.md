# Durable and Adaptive Control Planes for Multi-Agent Work

## Abstract

Multi-agent frameworks often model collaboration as messages among in-memory roles. Long-running work instead requires durable ownership, dependency semantics, bounded resources and objective executor selection. EvoAgent Fleet defines an orchestration model based on validated DAGs, expiring leases, capability constraints, budgets, human approval, content-addressed artifacts and evaluation-driven route metrics.

## Model

A workflow is a DAG `G=(V,E)`. Node `v` is claimable only when every predecessor has completed, its approval predicate holds and worker capabilities cover its requirements. A lease token grants temporary commit authority. On expiry the token becomes invalid and the node is retried up to a fixed bound.

Executor pools accumulate success and quality signals. A transparent score combines smoothed success rate and mean quality; cost and latency remain first-class observability dimensions. This separates learning which executor works from allowing an LLM to rewrite orchestration policy.

## Limitations

The reference control plane is single-node, artifacts are local files, route scoring is global rather than task-conditional, and authentication belongs at the deployment edge. Future work includes PostgreSQL claims, OpenTelemetry, A2A adapters, contextual bandits with offline evaluation, credential brokerage and hierarchical manager policies.

