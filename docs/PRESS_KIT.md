# EvoAgent OS v0.1 Press Kit

Publication status: draft material for a development preview  
Fact snapshot: 2026-08-10

Use this copy only with the `v0.1 development preview` qualifier. Do not describe the project as feature complete, enterprise certified, production proven or equivalent to a stated amount of engineering labor.

## Naming

- Project: **EvoAgent OS**
- Repository: `github.com/Linxiushen/evoagent-os`
- Category: open-source agent control plane / engineering preview
- License: Apache-2.0 repository work; HarnessLab subtree remains MIT

## One-line description

EvoAgent OS is a local-first control plane for durable agent runs, leased multi-agent workflows, signed Skills, executable trace regression and consent-first realtime interaction.

## 60-word description

EvoAgent OS v0.1 is an open-source development preview for engineers building governed agent systems. It combines persistent sessions and approvals, DAG execution with expiring worker leases and budgets, deterministic signed Skill packages, executable Trace Contract regression tests, and an optional consent-first realtime gateway. Its default demos run offline; production identity, distributed state and measured SLOs remain roadmap work.

## 中文简介

EvoAgent OS v0.1 是面向可治理 Agent 系统的开源开发预览版。它将持久会话与审批、带租约和预算的多 Agent DAG、确定性签名 Skill 包、可执行 Trace Contract 回归测试，以及以授权为前提的实时交互网关整合到一个仓库。默认演示可离线运行；生产级身份、分布式状态和经测量的 SLO 仍属于路线图范围。

## Verified launch points

- Runtime persists sessions, runs, approvals, schedules, memory and event records in the local reference store.
- Fleet validates DAGs and commits work through capability matching, expiring leases, retries, declared budgets and approval nodes.
- Forge scans, deterministically packages and optionally signs `.evoskill` artifacts with Ed25519.
- HarnessLab turns agent lifecycle behavior into portable trace artifacts and CI regression decisions.
- EchoWeave provides an offline synthetic demo plus consent/disclosure controls for realtime voice/avatar paths.
- Versioned contracts are exposed through tested Python and TypeScript SDKs.
- Python components are covered by a Python 3.11/3.12 Ruff and pytest CI matrix.

Every statement above is qualified in [`FEATURE_MATRIX.md`](FEATURE_MATRIX.md).

## What v0.1 is not

- Not a replacement for a full enterprise collaboration or office suite
- Not a certified hostile-code sandbox
- Not a multi-tenant authorization system
- Not a multi-region workflow service
- Not a model-quality or productivity benchmark
- Not authorization to clone or impersonate a real person

## Release headline options

1. **EvoAgent OS v0.1 makes agent control-flow evidence executable**
2. **A local reference stack for durable, governed agent workflows**
3. **From agent demo to inspectable leases, approvals, Skills and traces**

## Short social copy

### English

> EvoAgent OS v0.1 is now available as a development preview: durable local agent sessions, leased DAG workflows, approval gates, signed Skill artifacts, Trace Contract regression tests, and an optional consent-first realtime path. The offline demo needs no model key. Scope and evidence: https://github.com/Linxiushen/evoagent-os

### 中文

> EvoAgent OS v0.1 开发预览版：持久化 Agent 会话、带租约的 DAG、审批门禁、签名 Skill 工件、Trace Contract 回归测试，以及可选的授权优先实时链路。默认离线演示无需模型密钥。功能范围和证据：https://github.com/Linxiushen/evoagent-os

## Suggested maintainer quote

Edit and approve before use; this is a draft, not a recorded quotation:

> “The goal of v0.1 is not to hide complexity behind another chat screen. It is to make the decisions around agent execution—leases, approvals, artifacts and regressions—visible enough to test.”

## Interview talking points

- **Architecture:** independent services preserve failure and dependency boundaries while a monorepo enables contract tests and one review surface.
- **Reliability:** a Fleet lease is the commit capability; expired work cannot submit a late result, while external side effects still require idempotency.
- **Safety:** model output is untrusted; approvals and capability policies are persisted outside model prose.
- **Supply chain:** deterministic bytes, scanning, signatures and evaluation provide separate evidence signals.
- **Testing:** Trace Contract compares control flow rather than requiring model wording to be identical.
- **Honesty:** v0.1 uses SQLite and localhost deployment; production gaps are documented as gates.

## FAQ

**Does it require a paid model API?**  
No for the offline reference demos and contract tests. External model/provider modes require their own credentials and terms.

**Is it production ready?**  
No production-readiness claim is made for v0.1. See the roadmap and operations gaps.

**Does a signed Skill mean it is safe?**  
No. A signature proves exact bytes were signed by a key. Operators still need a trusted fingerprint, capability review and isolated execution.

**Does the realtime component support cloning anyone?**  
No. Real-person use requires verified, scoped and revocable authorization plus persistent synthetic-media disclosure. The public demo is fictional.

**How does it relate to broad Agent platforms?**  
EvoAgent OS focuses on inspectable execution/governance contracts. It does not claim the organizational, collaboration and office-suite breadth of a full enterprise work platform.

## Visual asset checklist

No visual asset should imply an unimplemented capability. Recommended captures:

1. Fleet workflow with `publish` visibly paused at `awaiting_approval`.
2. The same workflow completed with artifact SHA-256 evidence.
3. HarnessLab structural diff showing an approval/tool-path regression.
4. Runtime pending-approval and event views using fictional data.
5. EchoWeave's fictional offline persona with visible AI disclosure.

Redact tokens, user content, internal hostnames and real-person media before publication.

Repository-ready v0.1 captures:

- [`assets/evoagent-console.png`](assets/evoagent-console.png): operations overview after the deterministic demo launch
- [`assets/evoagent-workflow.png`](assets/evoagent-workflow.png): four-node workflow paused at the publish approval gate
- [`assets/evoagent-approval.png`](assets/evoagent-approval.png): operator decision queue for a high-risk publish gate
- [`assets/evoagent-mobile.png`](assets/evoagent-mobile.png): responsive operations view at a 390 x 844 viewport
- [`assets/evoagent-social-card.png`](assets/evoagent-social-card.png): 1280 x 640 GitHub social preview built from the real workflow console

All captures use synthetic demo data. Keep the `v0.1 development preview` qualifier in adjacent release copy.
