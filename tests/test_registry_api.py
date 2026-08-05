from pathlib import Path

from fastapi.testclient import TestClient

from evoagent_forge.api import create_app
from evoagent_forge.package import build
from evoagent_forge.registry import Registry
from evoagent_forge.skill import load_skill


def test_registry_publish_search_and_immutability(skill: Path, tmp_path: Path) -> None:
    manifest, _ = load_skill(skill)
    artifact, _ = build(skill, manifest, tmp_path / "demo.evoskill")
    registry = Registry(tmp_path / "registry")
    release = registry.publish(manifest, artifact)
    assert release["name"] == "demo-skill"
    assert registry.search("deterministic")[0]["version"] == "1.2.3"
    try:
        registry.publish(manifest, artifact)
    except ValueError as exc:
        assert "already published" in str(exc)
    else:
        raise AssertionError("immutable name/version must not be overwritten")
    registry.close()


def test_registry_http(skill: Path, tmp_path: Path) -> None:
    manifest, _ = load_skill(skill)
    artifact, _ = build(skill, manifest, tmp_path / "demo.evoskill")
    root = tmp_path / "registry"
    registry = Registry(root)
    registry.publish(manifest, artifact)
    registry.close()
    with TestClient(create_app(root)) as client:
        assert client.get("/health").json()["status"] == "ok"
        assert client.get("/v1/releases/demo-skill/1.2.3").status_code == 200
