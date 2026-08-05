from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from .models import Capability, SkillManifest

NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
VERSION = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")


def load_skill(root: Path | str) -> tuple[SkillManifest, str]:
    root = Path(root).resolve()
    path = root / "SKILL.md"
    if not path.is_file():
        raise ValueError("SKILL.md is required")
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError("SKILL.md must start with YAML frontmatter")
    try:
        raw, body = text[4:].split("\n---\n", 1)
    except ValueError as exc:
        raise ValueError("SKILL.md frontmatter is not closed") from exc
    data = yaml.safe_load(raw) or {}
    if not isinstance(data, dict):
        raise TypeError("SKILL.md frontmatter must be an object")
    return _manifest(data, root), body.strip()


def _manifest(data: dict[str, Any], root: Path) -> SkillManifest:
    name = str(data.get("name", ""))
    version = str(data.get("version", ""))
    description = str(data.get("description", "")).strip()
    if not NAME.fullmatch(name):
        raise ValueError("name must be 2-63 lowercase letters, digits or hyphens")
    if not VERSION.fullmatch(version):
        raise ValueError("version must be semantic x.y.z")
    if not description:
        raise ValueError("description is required")
    capabilities = []
    for item in data.get("capabilities") or []:
        if isinstance(item, str):
            capabilities.append(Capability(item, "declared by author"))
        elif isinstance(item, dict):
            capabilities.append(
                Capability(
                    name=str(item.get("name", "")),
                    reason=str(item.get("reason", "declared by author")),
                    required=bool(item.get("required", True)),
                )
            )
    entrypoint = data.get("entrypoint")
    if entrypoint and not (root / str(entrypoint)).is_file():
        raise ValueError(f"entrypoint does not exist: {entrypoint}")
    known = {
        "name",
        "version",
        "description",
        "license",
        "entrypoint",
        "capabilities",
        "compatibility",
    }
    return SkillManifest(
        name=name,
        version=version,
        description=description,
        license=str(data.get("license", "NOASSERTION")),
        entrypoint=str(entrypoint) if entrypoint else None,
        capabilities=capabilities,
        compatibility={str(k): str(v) for k, v in (data.get("compatibility") or {}).items()},
        metadata={key: value for key, value in data.items() if key not in known},
    )


def scaffold(root: Path, name: str) -> Path:
    if root.exists():
        raise ValueError(f"Target already exists: {root}")
    root.mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\nversion: 0.1.0\ndescription: Describe when this skill should run\n"
        "license: Apache-2.0\nentrypoint: main.py\ncapabilities:\n  - name: filesystem.read\n"
        "    reason: Read operator-selected input files\n---\n\n# Instructions\n\nDescribe a bounded workflow.\n",
        encoding="utf-8",
    )
    (root / "main.py").write_text(
        "def run(payload: dict) -> dict:\n    return {'ok': True, 'input': payload}\n",
        encoding="utf-8",
    )
    (root / "tests" / "cases.yaml").write_text(
        "cases:\n  - name: smoke\n    input: {}\n    expect:\n      ok: true\n",
        encoding="utf-8",
    )
    return root
