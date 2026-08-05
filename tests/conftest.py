from pathlib import Path

import pytest


@pytest.fixture
def skill(tmp_path: Path) -> Path:
    root = tmp_path / "demo-skill"
    (root / "tests").mkdir(parents=True)
    (root / "SKILL.md").write_text(
        "---\nname: demo-skill\nversion: 1.2.3\ndescription: Safe deterministic demo\n"
        "license: Apache-2.0\nentrypoint: main.py\ncapabilities: []\n---\n\n# Demo\n",
        encoding="utf-8",
    )
    (root / "main.py").write_text(
        "def run(payload: dict) -> dict:\n    return {'ok': True, **payload}\n", encoding="utf-8"
    )
    (root / "tests" / "cases.yaml").write_text(
        "cases:\n  - name: smoke\n    input: {value: 2}\n    expect: {ok: true, value: 2}\n",
        encoding="utf-8",
    )
    return root
