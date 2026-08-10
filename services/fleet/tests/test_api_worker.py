from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from evoagent_fleet.api import create_app
from evoagent_fleet.models import Completion, NodeSpec, WorkerRegistration, WorkflowSpec
from evoagent_fleet.orchestrator import Orchestrator
from evoagent_fleet.store import Store
from evoagent_fleet.worker import Worker


def test_control_plane_api(tmp_path: Path) -> None:
    app = create_app(tmp_path / "fleet.sqlite", tmp_path / "artifacts")
    with TestClient(app) as client:
        response = client.post(
            "/v1/workflows", json={"name": "api", "nodes": [{"id": "a", "objective": "do it"}]}
        )
        workflow_id = response.json()["workflow_id"]
        client.post("/v1/workers", json={"worker_id": "w", "capabilities": []})
        lease = client.post("/v1/claims", json={"worker_id": "w"}).json()["lease"]
        complete = client.post(
            "/v1/completions",
            json={
                "worker_id": "w",
                "lease_token": lease["lease_token"],
                "result": {"output": {"ok": True}},
            },
        )
        assert complete.status_code == 200
        assert client.get(f"/v1/workflows/{workflow_id}").json()["status"] == "completed"


@pytest.mark.asyncio
async def test_embedded_worker_sdk(tmp_path: Path) -> None:
    store = Store(tmp_path / "fleet.sqlite")
    fleet = Orchestrator(store, tmp_path / "artifacts")
    workflow_id = fleet.submit(WorkflowSpec(name="sdk", nodes=[NodeSpec(id="a", objective="work")]))

    async def handler(lease):
        return Completion(output={"objective": lease["objective"]})

    worker = Worker(fleet, WorkerRegistration(worker_id="sdk-worker"), handler)
    assert await worker.run_once()
    assert fleet.view(workflow_id)["status"] == "completed"
    store.close()
