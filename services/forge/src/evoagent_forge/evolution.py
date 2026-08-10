from __future__ import annotations

import importlib.util
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from .models import SkillManifest


@dataclass
class Evaluation:
    suite: str
    passed: int
    total: int
    score: float
    failures: list[dict[str, Any]]


def evaluate(root: Path | str, manifest: SkillManifest) -> Evaluation:
    root = Path(root).resolve()
    cases_path = root / "tests" / "cases.yaml"
    if not cases_path.is_file() or not manifest.entrypoint:
        raise ValueError("An entrypoint and tests/cases.yaml are required")
    spec = importlib.util.spec_from_file_location("forge_candidate", root / manifest.entrypoint)
    if spec is None or spec.loader is None:
        raise ValueError("Cannot load entrypoint")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not callable(getattr(module, "run", None)):
        raise TypeError("Entrypoint must expose run(payload: dict) -> dict")
    cases = (yaml.safe_load(cases_path.read_text(encoding="utf-8")) or {}).get("cases", [])
    failures = []
    for case in cases:
        try:
            result = module.run(case.get("input") or {})
            expected = case.get("expect") or {}
            mismatches = {key: value for key, value in expected.items() if result.get(key) != value}
            if mismatches:
                failures.append(
                    {"case": case.get("name"), "expected": mismatches, "actual": result}
                )
        except Exception as exc:  # noqa: BLE001 - candidate failures are evaluation data.
            failures.append({"case": case.get("name"), "error": str(exc)})
    total = len(cases)
    passed = total - len(failures)
    return Evaluation("tests/cases.yaml", passed, total, passed / max(1, total), failures)


def write_evolution_proposal(
    root: Path | str, baseline: Evaluation, feedback: list[str], output: Path
) -> Path:
    proposal = {
        "kind": "skill-evolution-proposal/v1",
        "baseline": asdict(baseline),
        "evidence": feedback,
        "acceptance_gate": {
            "minimum_score": max(baseline.score, 0.8),
            "no_new_high_findings": True,
        },
        "instructions": [
            "Change a copy of the skill, never the published source artifact.",
            "Add a regression case for each accepted feedback item.",
            "Publish only when evaluation and security gates both pass.",
        ],
    }
    output.write_text(json.dumps(proposal, indent=2), encoding="utf-8")
    return output
