from datetime import UTC

import pytest
from pydantic import ValidationError

from evoagent_contracts import (
    AgentCreate,
    AgentStatus,
    AgentSummary,
    Overview,
    UnifiedEvent,
    WorkflowCreate,
    WorkflowNode,
    Workspace,
)


def test_agent_create_is_strict_and_json_ready() -> None:
    agent = AgentCreate(
        workspace_id="ws_demo",
        name="researcher",
        role="Research analyst",
        model="gpt-5",
        system_prompt="Return cited evidence.",
        tools=["web.search"],
    )

    assert agent.model_dump(mode="json")["workspace_id"] == "ws_demo"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AgentCreate(
            workspace_id="ws_demo",
            name="researcher",
            system_prompt="Return cited evidence.",
            unknown_control=True,
        )


def test_workflow_rejects_unknown_dependencies_and_cycles() -> None:
    with pytest.raises(ValidationError, match="unknown dependencies"):
        WorkflowCreate(
            workspace_id="ws_demo",
            name="invalid",
            nodes=[WorkflowNode(id="publish", objective="Publish", depends_on=["review"])],
        )

    with pytest.raises(ValidationError, match="dependency cycle"):
        WorkflowCreate(
            workspace_id="ws_demo",
            name="cycle",
            nodes=[
                WorkflowNode(id="a", objective="A", depends_on=["b"]),
                WorkflowNode(id="b", objective="B", depends_on=["a"]),
            ],
        )


def test_unified_event_exposes_cross_service_context() -> None:
    event = UnifiedEvent(
        source="runtime",
        type="run.completed",
        correlation="corr_1",
        causation="evt_parent",
        tenant="tenant_1",
        workspace="ws_1",
        run="run_1",
        workflow="wf_1",
        node="summarize",
        payload={"quality": 0.98},
    )

    assert event.id.startswith("evt_")
    assert event.timestamp.tzinfo == UTC
    assert event.model_dump(mode="json")["payload"] == {"quality": 0.98}


def test_overview_accepts_additive_server_fields() -> None:
    overview = Overview.model_validate(
        {
            "counts": {"runs": 4, "future_counter": 2},
            "metrics": {"success_rate": 0.95},
            "release_channel": "edge",
        }
    )

    assert overview.counts.runs == 4
    assert overview.counts.model_extra == {"future_counter": 2}
    assert overview.model_extra == {"release_channel": "edge"}


def test_read_contracts_normalize_control_plane_storage_names() -> None:
    workspace = Workspace.model_validate(
        {"workspace_id": "ws_1", "tenant_id": "tenant_1", "name": "Operations"}
    )
    agent = AgentSummary.model_validate(
        {
            "agent_id": "agent_1",
            "workspace_id": "ws_1",
            "name": "Coordinator",
            "status": "ready",
        }
    )
    overview = Overview.model_validate(
        {
            "recent_runs": [],
            "workflows": [
                {
                    "workflow_id": "wf_1",
                    "name": "Research",
                    "status": "running",
                    "metadata": {"workspace_id": "ws_1"},
                }
            ],
            "approvals": [
                {
                    "approval_id": "apr_1",
                    "status": "pending",
                    "subject": "Publish report",
                    "arguments": {"format": "md"},
                    "created_at": "2026-08-10T03:00:00Z",
                }
            ],
            "events": [
                {
                    "id": "evt_1",
                    "source": "fleet",
                    "type": "node.ready",
                    "workspace_id": "ws_1",
                    "workflow_id": "wf_1",
                    "node_id": "publish",
                    "payload": {},
                    "created_at": "2026-08-10T03:00:00Z",
                }
            ],
        }
    )

    assert workspace.id == "ws_1"
    assert agent.id == "agent_1"
    assert agent.status == AgentStatus.READY
    assert overview.workflows[0].workspace_id == "ws_1"
    assert overview.approvals[0].action == "Publish report"
    assert overview.events[0].workflow == "wf_1"
    assert overview.events[0].timestamp.tzinfo is not None
