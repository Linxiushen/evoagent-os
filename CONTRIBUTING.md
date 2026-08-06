# Contributing

Contributions are welcome, especially public adapter fixtures, Trace Contract invariants, policy
providers, and exporters.

## Development setup

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev,mcp]"  # Windows
pytest -q
ruff check .
harnesslab check
```

Use `.venv/bin/...` on macOS and Linux.

## Adapter pull requests

Adapter changes must include:

1. A link to the public protocol or API contract.
2. A credential-free fixture that is stable in CI.
3. At least one success case and one fail-closed case.
4. A recorded Trace Contract baseline when lifecycle behavior is deterministic.
5. No claim of official support unless the upstream owner confirms it.

## Trace invariant pull requests

Explain the production failure the invariant prevents. Include a passing trace and a minimal
regressed trace that proves the comparison catches it. Keep provider-specific rules separate from
the generic `harnesslab.trace/v1` contract.

## Pull request scope

Keep changes reviewable and avoid unrelated formatting or dependency churn. Run the full test,
lint, conformance, and snapshot/verify flows before submitting.
