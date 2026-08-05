from __future__ import annotations

import ast
import re
from pathlib import Path

from .models import Finding, SkillManifest

IMPORT_CAPABILITIES = {
    "requests": "network.http",
    "httpx": "network.http",
    "socket": "network.raw",
    "subprocess": "process.exec",
    "sqlite3": "database.local",
}
DANGEROUS_CALLS = {"eval": "dynamic eval", "exec": "dynamic exec", "compile": "dynamic compile"}
SECRET = re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[=:]\s*['\"][^'\"]{8,}")


def scan(root: Path | str, manifest: SkillManifest) -> list[Finding]:
    root = Path(root).resolve()
    findings: list[Finding] = []
    inferred: set[str] = set()
    for path in sorted(root.rglob("*")):
        if not path.is_file() or any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        relative = path.relative_to(root).as_posix()
        if path.stat().st_size > 5_000_000:
            findings.append(Finding("high", "F003", "File exceeds 5 MB package limit", relative))
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in SECRET.finditer(text):
            findings.append(
                Finding(
                    "critical",
                    "S001",
                    "Possible embedded credential",
                    relative,
                    text[: match.start()].count("\n") + 1,
                )
            )
        if path.suffix == ".py":
            _scan_python(text, relative, findings, inferred)
    declared = {item.name for item in manifest.capabilities}
    for capability in sorted(inferred - declared):
        findings.append(
            Finding("high", "C001", f"Undeclared inferred capability: {capability}", "SKILL.md")
        )
    for capability in sorted(declared - inferred):
        findings.append(
            Finding(
                "info",
                "C002",
                f"Declared capability was not statically observed: {capability}",
                "SKILL.md",
            )
        )
    return findings


def _scan_python(text: str, path: str, findings: list[Finding], inferred: set[str]) -> None:
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        findings.append(
            Finding("high", "P001", f"Python syntax error: {exc.msg}", path, exc.lineno or 1)
        )
        return
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = (
                [item.name for item in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            for name in names:
                root_name = name.split(".")[0]
                if root_name in IMPORT_CAPABILITIES:
                    inferred.add(IMPORT_CAPABILITIES[root_name])
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else ""
            if name in DANGEROUS_CALLS:
                findings.append(
                    Finding("critical", "P002", DANGEROUS_CALLS[name], path, node.lineno)
                )
            if isinstance(node.func, ast.Attribute) and node.func.attr in {"system", "popen"}:
                inferred.add("process.exec")
                findings.append(
                    Finding("high", "P003", "Shell process execution", path, node.lineno)
                )


def blocking(findings: list[Finding]) -> bool:
    return any(item.severity in {"critical", "high"} for item in findings)
