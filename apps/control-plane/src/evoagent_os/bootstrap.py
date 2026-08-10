from __future__ import annotations

import sys
from pathlib import Path


def activate_workspace_packages() -> Path:
    """Make monorepo packages importable without requiring an editable install for each one."""
    root = Path(__file__).resolve().parents[4]
    sources = (
        root / "packages" / "contracts" / "src",
        root / "services" / "runtime" / "src",
        root / "services" / "fleet" / "src",
        root / "services" / "forge" / "src",
        root / "services" / "observability" / "src",
        root / "services" / "realtime" / "src",
    )
    for source in reversed(sources):
        if source.is_dir() and str(source) not in sys.path:
            sys.path.insert(0, str(source))
    return root
