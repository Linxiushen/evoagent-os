import time
from pathlib import Path

import pytest

from evoagent_fleet.models import Budget, Completion, NodeSpec, WorkerRegistration, WorkflowSpec
from evoagent_fleet.orchestrator import Orchestrator
from evoagent_fleet.store import Store


@pytest.fixture
def fleet(tmp_path: Path):
    store = Store(tmp_path / "fleet.sqlite")
    value = Orchestrator(store, tmp_path / "artifacts", lease_seconds=1)
    yield value
    store.close()


def test_dag_validation_rejects_cycles() -> None:
    with pytest.raises(ValueError, match="cycle"):
        WorkflowSpec(
            name="bad",
            nodes=[
                NodeSpec(id="a", objective="a", depends_on=["b"]),
                NodeSpec(id="b", objective="b", depends_on=["a"]),
            ],
        )


def test_durable_dag_capabilities_approval_and_artifacts(fleet: Orchestrator) -> None:
    workflow_id = fleet.submit(
        WorkflowSpec(
            name="release",
            nodes=[
                NodeSpec(id="research", objective="collect", capabilities=["research"]),
                NodeSpec(
                    id="publish",
                    objective="publish",
                    capabilities=["publish"],
                    depends_on=["research"],
                    approval_required=True,
                ),
            ],
        )
    )
    fleet.register(WorkerRegistration(worker_id="wrong", capabilities=["publish"]))
    assert fleet.claim("wrong") is None
    fleet.register(WorkerRegistration(worker_id="r1", capabilities=["research"], pool="reasoner"))
    lease = fleet.claim("r1")
    assert lease and lease["node_id"] == "research"
    result = fleet.complete(
        "r1",
        lease["lease_token"],
        Completion(output={"facts": 3}, artifacts={"evidence.md": "three facts"}, quality=0.9),
    )
    assert result["artifacts"][0]["sha256"]
    nodes = {node["node_id"]: node for node in fleet.view(workflow_id)["nodes"]}
    assert nodes["publish"]["status"] == "awaiting_approval"
    fleet.approve(workflow_id, "publish", True, "alice")
    fleet.register(WorkerRegistration(worker_id="p1", capabilities=["publish"]))
    publish = fleet.claim("p1")
    fleet.complete("p1", publish["lease_token"], Completion(output={"url": "local"}))
    assert fleet.view(workflow_id)["status"] == "completed"


def test_budget_and_lease_enforcement(fleet: Orchestrator) -> None:
    workflow_id = fleet.submit(
        WorkflowSpec(
            name="budgeted",
            nodes=[
                NodeSpec(
                    id="n", objective="work", budget=Budget(tokens=10, cost_usd=0.1, seconds=5)
                )
            ],
        )
    )
    fleet.register(WorkerRegistration(worker_id="w", capabilities=[]))
    lease = fleet.claim("w")
    with pytest.raises(ValueError, match="budget"):
        fleet.complete("w", lease["lease_token"], Completion(tokens_used=11))
    fleet.fail("w", lease["lease_token"], "temporary")
    assert fleet.view(workflow_id)["nodes"][0]["status"] == "queued"


def test_expired_lease_is_requeued(fleet: Orchestrator) -> None:
    workflow_id = fleet.submit(
        WorkflowSpec(name="lease", nodes=[NodeSpec(id="n", objective="work")])
    )
    fleet.register(WorkerRegistration(worker_id="w", capabilities=[]))
    lease = fleet.claim("w")
    fleet.store.execute(
        "UPDATE nodes SET lease_expires=? WHERE workflow_id=? AND node_id='n'",
        (time.time() - 1, workflow_id),
    )
    assert fleet.sweep_expired() == 1
    assert fleet.view(workflow_id)["nodes"][0]["status"] == "queued"
    with pytest.raises(ValueError, match="no longer active"):
        fleet.heartbeat("w", lease["lease_token"])
