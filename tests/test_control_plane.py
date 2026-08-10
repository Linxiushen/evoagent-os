from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from evoagent_os.api import create_app
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path: Path):
    with TestClient(create_app(tmp_path / "state")) as value:
        yield value


def test_health_and_seeded_catalog(client: TestClient) -> None:
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["components"]["fleet"] == "ok"

    overview = client.get("/api/v1/overview").json()
    assert overview["counts"]["workspaces"] == 1
    assert overview["counts"]["agents"] == 4
    assert overview["counts"]["skills"] >= 3
    assert overview["counts"]["online_workers"] == 1


def test_optional_gateway_auth_is_fail_closed(tmp_path: Path) -> None:
    with TestClient(
        create_app(tmp_path / "state", gateway_token="secret")  # noqa: S106
    ) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/api/v1/overview").status_code == 401
        response = client.get("/api/v1/overview", headers={"Authorization": "Bearer secret"})
        assert response.status_code == 200


def test_run_is_linked_to_agent_and_workspace(client: TestClient) -> None:
    response = client.post(
        "/api/v1/runs",
        json={
            "workspace_id": "ws_default",
            "agent_id": "agent_coordinator",
            "input": "Prepare a bounded execution plan",
        },
    )
    assert response.status_code == 202
    run = response.json()
    assert run["status"] == "completed"

    detail = client.get(f"/api/v1/runs/{run['run_id']}").json()
    assert detail["workspace_id"] == "ws_default"
    assert detail["agent_id"] == "agent_coordinator"
    assert detail["events"][-1]["type"] == "run.accepted"


def test_high_risk_runtime_tool_requires_approval(client: TestClient, tmp_path: Path) -> None:
    response = client.post(
        "/api/v1/runs",
        json={
            "workspace_id": "ws_default",
            "agent_id": "agent_writer",
            "input": "write: approved.txt=governed output",
        },
    )
    assert response.status_code == 202
    assert response.json()["status"] == "awaiting_approval"
    approval = client.get("/api/v1/approvals").json()[0]
    assert approval["source"] == "runtime"
    assert approval["risk"] == "high"

    decision = client.post(
        f"/api/v1/approvals/{approval['approval_id']}",
        json={"approved": True, "actor": "test-operator"},
    )
    assert decision.status_code == 200
    assert decision.json()["run"]["status"] == "completed"


def test_demo_pauses_resumes_and_publishes_verified_artifacts(client: TestClient) -> None:
    launched = client.post("/api/v1/demo/launch", headers={"Idempotency-Key": "demo-1"})
    assert launched.status_code == 201
    demo = launched.json()
    assert demo["completed_nodes"] == ["research", "analysis", "draft"]

    workflow = client.get(f"/api/v1/workflows/{demo['workflow_id']}").json()
    statuses = {node["node_id"]: node["status"] for node in workflow["nodes"]}
    assert statuses["publish"] == "awaiting_approval"

    approvals = client.get("/api/v1/approvals").json()
    assert [item["approval_id"] for item in approvals] == [demo["approval_id"]]
    decision = client.post(
        f"/api/v1/approvals/{demo['approval_id']}",
        json={"approved": True, "actor": "release-manager"},
    )
    assert decision.status_code == 200
    assert decision.json()["status"] == "completed"

    artifacts = client.get("/api/v1/artifacts", params={"workflow_id": demo["workflow_id"]}).json()
    assert {item["name"] for item in artifacts} >= {
        "market-brief.md",
        "market-brief.html",
        "evidence.csv",
    }
    artifact = next(item for item in artifacts if item["name"] == "market-brief.md")
    content = client.get(artifact["download_url"])
    assert content.status_code == 200
    assert hashlib.sha256(content.content).hexdigest() == artifact["sha256"]


def test_demo_idempotency_prevents_duplicate_workflow(client: TestClient) -> None:
    first = client.post("/api/v1/demo/launch", headers={"Idempotency-Key": "same"}).json()
    second = client.post("/api/v1/demo/launch", headers={"Idempotency-Key": "same"}).json()
    assert second == first
    assert len(client.get("/api/v1/workflows").json()) == 1


def test_invalid_cycle_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/v1/workflows",
        json={
            "name": "Invalid cyclic workflow",
            "nodes": [
                {"id": "a", "objective": "A", "depends_on": ["b"]},
                {"id": "b", "objective": "B", "depends_on": ["a"]},
            ],
        },
    )
    assert response.status_code == 422


def test_remote_worker_lease_protocol(client: TestClient) -> None:
    workflow = client.post(
        "/api/v1/workflows",
        json={
            "name": "Remote execution",
            "nodes": [
                {
                    "id": "execute",
                    "objective": "Produce a bounded result",
                    "capabilities": ["custom"],
                    "tokens": 100,
                    "cost_usd": 0.1,
                }
            ],
        },
    ).json()
    registration = client.post(
        "/api/v1/workers",
        json={"worker_id": "worker_remote", "capabilities": ["custom"]},
    )
    assert registration.status_code == 201
    lease = client.post("/api/v1/workers/worker_remote/claims").json()["lease"]
    assert lease["workflow_id"] == workflow["workflow_id"]

    heartbeat = client.post(
        f"/api/v1/leases/{lease['lease_token']}/heartbeat",
        json={"worker_id": "worker_remote"},
    )
    assert heartbeat.status_code == 200
    completion = client.post(
        f"/api/v1/leases/{lease['lease_token']}/completion",
        json={
            "worker_id": "worker_remote",
            "output": {"ok": True},
            "artifacts": {"result.md": "# Result"},
            "tokens_used": 50,
            "cost_usd": 0.02,
            "quality": 0.9,
        },
    )
    assert completion.status_code == 200
    detail = client.get(f"/api/v1/workflows/{workflow['workflow_id']}").json()
    assert detail["status"] == "completed"


def test_expired_lease_rejects_stale_completion_and_requeues(client: TestClient) -> None:
    workflow = client.post(
        "/api/v1/workflows",
        json={
            "name": "Lease recovery",
            "nodes": [
                {
                    "id": "recover",
                    "objective": "Recover after worker loss",
                    "capabilities": ["recovery-test"],
                }
            ],
        },
    ).json()
    client.post(
        "/api/v1/workers",
        json={"worker_id": "worker_recovery", "capabilities": ["recovery-test"]},
    )
    stale = client.post("/api/v1/workers/worker_recovery/claims").json()["lease"]
    client.app.state.fleet_store.execute(
        "UPDATE nodes SET lease_expires=0 WHERE workflow_id=?",
        (workflow["workflow_id"],),
    )
    recovered = client.post("/api/v1/workers/worker_recovery/claims").json()["lease"]
    assert recovered["lease_token"] != stale["lease_token"]

    stale_result = client.post(
        f"/api/v1/leases/{stale['lease_token']}/completion",
        json={"worker_id": "worker_recovery", "output": {"stale": True}},
    )
    assert stale_result.status_code == 409
    valid_result = client.post(
        f"/api/v1/leases/{recovered['lease_token']}/completion",
        json={"worker_id": "worker_recovery", "output": {"recovered": True}},
    )
    assert valid_result.status_code == 200


def test_demo_worker_does_not_claim_an_unrelated_workflow(client: TestClient) -> None:
    unrelated = client.post(
        "/api/v1/workflows",
        json={
            "name": "Unrelated queued work",
            "nodes": [{"id": "open", "objective": "Wait for an external worker"}],
        },
    ).json()
    demo = client.post("/api/v1/demo/launch").json()

    unrelated_detail = client.get(f"/api/v1/workflows/{unrelated['workflow_id']}").json()
    assert unrelated_detail["nodes"][0]["status"] == "queued"
    assert demo["completed_nodes"] == ["research", "analysis", "draft"]
