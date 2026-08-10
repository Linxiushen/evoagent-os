# Market-research workflow contract demo

This example demonstrates the integrated control plane and Fleet's lower-level orchestration controls. It deliberately does **not** browse the web, call a model or present fixture text as market evidence.

## Integrated control-plane demo

Start the default Compose profile, export the token from your private `deploy/.env`, then launch the scenario:

```powershell
$env:EVOAGENT_OS_TOKEN = "<your-local-token>"
.\examples\market-research\launch.ps1
```

```bash
export EVOAGENT_OS_TOKEN="<your-local-token>"
sh examples/market-research/launch.sh
```

The response includes `run_id`, `workflow_id`, `approval_id` and `next_action`. Open `http://127.0.0.1:8800`, inspect the evidence and approve `publish`; the reference worker then emits the final content-addressed fixtures. [`control-plane.http`](control-plane.http) contains the equivalent raw HTTP requests.

The `Idempotency-Key` intentionally returns the same launch response on repeated requests against the same state. Change it only when you intend to create another workflow.

## Standalone Fleet: submit only

Start Fleet, then submit the DAG:

```powershell
.\examples\market-research\submit.ps1
```

```bash
sh examples/market-research/submit.sh
```

The workflow remains queued until workers with `market.research`, `market.review` and `artifact.publish` capabilities register.

## Standalone Fleet: deterministic end-to-end exercise

Use a fresh Fleet state directory, start the service, then run:

```bash
python examples/market-research/run_demo.py
```

The script uses Python's standard library. It creates fixture workers, completes the first two nodes and pauses visibly before `publish`. Type `APPROVE` to persist the approval and finish the workflow. For non-interactive CI or a scripted recording, pass `--approve-publish`.

What this proves:

- The DAG is accepted and dependencies unblock in order.
- Workers claim nodes by declared capability.
- Completion requires the active lease token.
- Artifacts are stored with SHA-256 identity.
- The publish node cannot be claimed before approval.
- The workflow reaches `completed` only after every node completes.

What this does not prove:

- Research quality, browsing, citation verification or model accuracy
- Authentication, tenant isolation or production scalability
- Exactly-once external side effects

Replace fixture workers with reviewed production workers only after defining source policy, network allowlists, trusted cost metering, idempotency and artifact retention.
