# Clean-room Scope Comparison: EvoAgent OS and Magic

Snapshot date: 2026-08-10  
Magic reference commit: [`b857dc62`](https://github.com/dtyq/magic/tree/b857dc62bbab320edd5c93542f5efc2119804b65)

## Method and limits

EvoAgent OS does not import, copy or rebrand Magic code. This comparison was written from Magic's public project description, README, license and visible repository organization at the pinned commit, then checked against EvoAgent OS source and tests. Magic statements are labeled **documented** rather than independently benchmarked.

“Not established in reviewed evidence” never means a capability is absent. It means this review did not find enough stable public evidence to make a precise comparison. The two projects have different product boundaries, so this is not a winner/loser scorecard.

Primary Magic sources:

- [Magic README at the pinned commit](https://github.com/dtyq/magic/blob/b857dc62bbab320edd5c93542f5efc2119804b65/README.md)
- [Magic license at the pinned commit](https://github.com/dtyq/magic/blob/b857dc62bbab320edd5c93542f5efc2119804b65/LICENSE)
- [Magic repository tree at the pinned commit](https://github.com/dtyq/magic/tree/b857dc62bbab320edd5c93542f5efc2119804b65)

## Neutral comparison

| Dimension | Magic public scope | EvoAgent OS v0.1 scope |
| --- | --- | --- |
| Product boundary | Describes an enterprise AI Agent platform spanning a generalist Agent, workflow engine, IM and online collaborative office system | Development control plane focused on durable execution, workflow governance, Skill provenance, regression evidence and optional realtime interaction |
| Organization and collaboration | README documents organizational data consolidation, shared projects, enterprise channels and team-wide collaboration | No enterprise directory, shared office suite or mature collaboration product is claimed |
| Knowledge and business systems | README documents enterprise knowledge consolidation and connections to ERP/CRM/databases | Runtime provides local memory/workspace tools; an enterprise knowledge platform is outside v0.1 scope |
| Deliverable rendering | README documents PPT, dashboard, report, Excel, meeting-summary and canvas deliverables | Fleet stores generic content-addressed artifacts; format-specific office rendering is not a v0.1 claim |
| Agent/runtime path | README documents personal assistants and expert Agents operating continuously | Runtime implements persistent sessions, schedules, tools, approval/resume, event records and evaluation-gated prompt candidates |
| Workflow path | Project description and README document a workflow engine and human approval | Fleet exposes validated DAGs with dependencies, capabilities, expiring leases, heartbeats, retries, node budgets and approval nodes |
| Cost control | README documents daily budgets per department, user and Agent | Fleet checks worker-declared token/dollar usage against per-node budgets; trusted metering and organizational budgets are not yet implemented |
| Sandbox and isolation | README documents proprietary sandbox containers, VPC/private endpoints, sidecar network control and tenant isolation | Runtime has workspace/HTTP guards, but EvoAgent OS explicitly does not claim a hostile-code sandbox or tenant isolation |
| Skill ecosystem | README documents compatibility with Anthropic Skills and OpenClaw Skills plus plugin review | Forge defines its own deterministic `.evoskill` package, static scan, Ed25519 signature and registry/evaluation path; broad external-skill compatibility is not claimed |
| MCP | Magic's public repository contains MCP application/domain/infrastructure modules; README describes an open ecosystem | HarnessLab offers optional MCP tooling; a repository-wide MCP marketplace/server product is not claimed |
| Regression evidence | Exact equivalent not established from the public sources reviewed | HarnessLab defines `harnesslab.trace/v1`, invariants, stable fingerprints, structural comparison and CI failure semantics |
| Realtime voice/avatar | Not evaluated in this comparison | EchoWeave provides a consent-first versioned realtime pipeline and synthetic offline demo; named-model production performance is not claimed |
| Deployment maturity | README documents self-hosted Docker deployment for macOS/Linux, cloud services and an enterprise offering | Reference Compose is a localhost, single-host development topology; production identity/distributed state/SLOs remain roadmap items |
| License | Magic Open Source License: modified Apache 2.0 with multi-tenant SaaS and branding restrictions described in its LICENSE | Repository work is Apache-2.0; HarnessLab subtree remains MIT; component/third-party notices remain in place |

## What the comparison says

Magic's public positioning emphasizes the breadth of an enterprise work platform: organization, collaboration, knowledge, office deliverables, sandboxing and commercial deployment. EvoAgent OS v0.1 is narrower and engineering-oriented. Its differentiators are explicit durable workflow/event state, worker lease and budget semantics, policy approvals, signed Skill artifacts, deterministic offline tests and executable trace regression.

Those strengths are complementary categories, not proof of overall superiority. A responsible evaluation should start from the intended deployment:

- Choose a broad workplace platform evaluation when identity, IM, shared projects, enterprise knowledge and finished office artifacts are primary requirements.
- Evaluate EvoAgent OS when the question is whether execution and behavioral-control contracts can be inspected, tested and extended in a small local reference system.
- For either project, validate security, scale, cost and model quality on the actual release and infrastructure rather than relying on README statements.

## Claim hygiene

Acceptable wording:

> “EvoAgent OS v0.1 explores a narrower control-plane architecture with explicit leases, budgets, approvals, signed Skill artifacts and executable trace regression.”

Avoid:

- “Feature complete replacement for Magic”
- “Enterprise-ready” without deployment evidence
- “Safer” or “faster” without an agreed threat model or benchmark
- Any staffing, person-year or productivity-equivalence claim
