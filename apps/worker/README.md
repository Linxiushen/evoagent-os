# EvoAgent Worker

The reference worker demonstrates the remote lease protocol without executing arbitrary code. It
registers declared capabilities, claims one bounded node at a time, and submits a deterministic
result plus a Markdown artifact. Production workers can replace the executor while keeping the same
lease, heartbeat, budget, and stale-completion semantics.

```bash
pip install -e apps/worker
evoagent-worker --url http://127.0.0.1:8765 --capability research --capability writing
```

Set `EVOAGENT_OS_TOKEN` or pass `--token` when the control plane requires authentication.
