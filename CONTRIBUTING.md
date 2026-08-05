# Contributing

Contributions are welcome, especially adapter fixtures and protocol conformance cases.

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev,mcp]"  # Windows
pytest -q
ruff check .
```

Adapter pull requests should include:

1. A link to the public protocol or API contract.
2. A fixture that works without live credentials.
3. At least one success case and one fail-closed case.
4. No claims of official support unless the upstream owner confirms them.

