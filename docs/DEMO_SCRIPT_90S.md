# 90-second demo script

Audience: engineering, product or hiring review  
Scenario: deterministic market-research workflow contract  
Claim boundary: no live research and no model-quality claim

## Before recording

1. Start the default Compose profile with fresh state and private random tokens.
2. Open `http://127.0.0.1:8800` and authenticate with the local control-plane token.
3. Export the same token as `EVOAGENT_OS_TOKEN` in a terminal at the repository root.
4. Use only the synthetic fixture; do not display the token, private prompts or real-person media.

## Script

**0-12 seconds: Frame the problem**

Show the control-plane overview.

> "Agent demos usually hide the difficult part: durable state, retries, budgets and human control. EvoAgent OS v0.1 makes those transitions inspectable. This is a development preview, and this demo uses deterministic fixtures rather than pretending to do live research."

**12-30 seconds: Submit durable work**

Run one of:

```powershell
.\examples\market-research\launch.ps1
```

```bash
sh examples/market-research/launch.sh
```

> "One idempotent request creates a persistent Agent run and a typed DAG: source mapping, analysis, drafting, then publishing. Every node carries capabilities, dependencies, budgets and retry policy."

Point to the returned `run_id`, `workflow_id` and `approval_id`.

**30-50 seconds: Show the governance boundary**

Refresh the approvals view and open the `publish` decision.

> "The deterministic worker can produce reviewed draft evidence, but publishing is not claimable until an operator records a decision. Approval is durable state outside the model prompt."

Review the fixture disclosure and click **Approve**.

**50-70 seconds: Complete under a lease**

Open the workflow and final artifacts.

> "The publisher commits through an expiring lease. Late results are rejected, declared usage is checked against the node budget, and every deliverable has a SHA-256 content identity."

Show the completed nodes and one artifact digest.

**70-90 seconds: Establish the wider system and limits**

Show the repository's service navigation or [`FEATURE_MATRIX.md`](FEATURE_MATRIX.md).

> "The repository also includes persistent agent sessions and resumable tool approvals, signed Skill packages, executable Trace Contract regression tests, and an optional consent-first realtime gateway. v0.1 is a single-host engineering preview; production identity, distributed state and measured SLOs are roadmap gates, not claims."

## Backup path

If the integrated UI is unavailable, run the standalone Fleet exercise:

```bash
python examples/market-research/run_demo.py
```

It pauses for the literal input `APPROVE`. For a non-interactive recording, `--approve-publish` is available; state verbally that the flag is the scripted operator decision, not automatic policy approval.
