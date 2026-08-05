from pathlib import Path

from evoagent_forge.evolution import evaluate
from evoagent_forge.package import build
from evoagent_forge.scanner import blocking, scan
from evoagent_forge.signing import generate, sign, verify
from evoagent_forge.skill import load_skill


def test_validate_scan_evaluate(skill: Path) -> None:
    manifest, body = load_skill(skill)
    assert manifest.name == "demo-skill"
    assert "Demo" in body
    assert not blocking(scan(skill, manifest))
    result = evaluate(skill, manifest)
    assert result.score == 1.0


def test_deterministic_package_and_signature(skill: Path, tmp_path: Path) -> None:
    manifest, _ = load_skill(skill)
    first, digest_a = build(skill, manifest, tmp_path / "a.evoskill")
    _, digest_b = build(skill, manifest, tmp_path / "b.evoskill")
    assert digest_a == digest_b

    private, public = tmp_path / "private.pem", tmp_path / "public.pem"
    fingerprint = generate(private, public)
    signature = tmp_path / "a.sig.json"
    sign(first, private, signature)
    assert verify(first, signature, fingerprint)
    first.write_bytes(first.read_bytes() + b"tampered")
    assert not verify(first, signature)


def test_scanner_blocks_secret_and_undeclared_network(skill: Path) -> None:
    (skill / "main.py").write_text(
        "import httpx\nAPI_KEY='abcdefghijk'\ndef run(payload): return {}\n", encoding="utf-8"
    )
    manifest, _ = load_skill(skill)
    findings = scan(skill, manifest)
    assert blocking(findings)
    assert {item.code for item in findings} >= {"S001", "C001"}
