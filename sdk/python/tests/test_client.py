from __future__ import annotations

import json

import httpx
import pytest
from evoagent_contracts import (
    AgentCreate,
    ApprovalStatus,
    RunCreate,
    WorkflowCreate,
    WorkflowNode,
)

from evoagent_sdk import ApiError, Client, ProtocolError


def json_response(request: httpx.Request, data: object, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, request=request, json=data)


def test_client_uses_v1_routes_auth_and_typed_resources() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path == "/api/v1/overview":
            return json_response(request, {"counts": {"runs": 3}, "metrics": {"tokens": 1200}})
        if path == "/api/v1/workspaces":
            return json_response(request, [{"id": "ws_1", "name": "Demo"}])
        if path == "/api/v1/agents" and request.method == "POST":
            body = json.loads(request.content)
            return json_response(request, {"id": "agt_1", **body})
        if path == "/api/v1/runs" and request.method == "POST":
            body = json.loads(request.content)
            return json_response(request, {"id": "run_1", "status": "running", **body})
        raise AssertionError(f"unexpected request: {request.method} {path}")

    with Client(
        "https://control.example",
        api_key="secret",
        tenant_id="tenant_1",
        transport=httpx.MockTransport(handler),
    ) as client:
        assert client.overview().counts.runs == 3
        assert client.workspaces()[0].id == "ws_1"
        agent = client.create_agent(
            AgentCreate(
                workspace_id="ws_1",
                name="researcher",
                system_prompt="Cite evidence.",
                capabilities=["research"],
            )
        )
        run = client.create_run(
            RunCreate(
                workspace_id="ws_1",
                agent_id=agent.id,
                input={"topic": "agents"},
                idempotency_key="run-once",
            )
        )

    assert run.status == "running"
    assert seen[0].headers["authorization"] == "Bearer secret"
    assert seen[0].headers["x-tenant-id"] == "tenant_1"
    assert seen[-1].url.path == "/api/v1/runs"
    assert seen[-1].headers["idempotency-key"] == "run-once"
    assert json.loads(seen[-1].content)["input"] == '{"topic":"agents"}'


def test_create_workflow_enriches_lightweight_control_plane_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/workflows"
        assert request.headers["idempotency-key"] == "workflow-once"
        body = json.loads(request.content)
        assert body["nodes"][0]["tokens"] == 8000
        assert "idempotency_key" not in body
        return json_response(request, {"workflow_id": "wf_1", "status": "running"}, 201)

    request = WorkflowCreate(
        workspace_id="ws_1",
        name="Research pipeline",
        nodes=[WorkflowNode(id="research", objective="Collect evidence", tokens=8000)],
        idempotency_key="workflow-once",
    )
    with Client("https://control.example", transport=httpx.MockTransport(handler)) as client:
        workflow = client.create_workflow(request)

    assert workflow.id == "wf_1"
    assert workflow.workspace_id == "ws_1"
    assert workflow.name == "Research pipeline"


def test_decision_and_demo_use_canonical_routes() -> None:
    requests: list[tuple[str, str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append((request.method, request.url.path, body))
        if request.url.path == "/api/v1/approvals/apr_1":
            return json_response(
                request,
                {
                    "workspace_id": "ws_1",
                    "status": "approved",
                    "decision": body,
                },
            )
        if request.url.path == "/api/v1/demo/launch":
            return json_response(request, {"workflow_id": "wf_demo", "status": "running"})
        raise AssertionError(request.url.path)

    with Client("https://control.example/api/v1", transport=httpx.MockTransport(handler)) as client:
        approval = client.decide_approval("apr_1", True, actor="lin", reason="reviewed")
        demo = client.demo({"workspace_id": "ws_1"})

    assert approval.id == "apr_1"
    assert approval.status == ApprovalStatus.APPROVED
    assert demo["workflow_id"] == "wf_demo"
    assert requests == [
        (
            "POST",
            "/api/v1/approvals/apr_1",
            {"approved": True, "actor": "lin", "reason": "reviewed"},
        ),
        ("POST", "/api/v1/demo/launch", {"workspace_id": "ws_1"}),
    ]


def test_list_filters_none_and_accepts_items_envelope() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert dict(request.url.params) == {"workspace_id": "ws_1", "limit": "10"}
        return json_response(
            request,
            {
                "items": [
                    {
                        "id": "run_1",
                        "workspace_id": "ws_1",
                        "agent_id": "agt_1",
                        "status": "completed",
                    }
                ]
            },
        )

    with Client("https://control.example", transport=httpx.MockTransport(handler)) as client:
        runs = client.runs(workspace_id="ws_1", limit=10)

    assert [run.id for run in runs] == ["run_1"]


def test_api_error_preserves_status_and_detail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(request, {"detail": "budget exceeded"}, status_code=409)

    with (
        Client("https://control.example", transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ApiError, match="budget exceeded") as caught,
    ):
        client.runs()

    assert caught.value.status_code == 409
    assert caught.value.response_body == {"detail": "budget exceeded"}


def test_protocol_error_on_non_list_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(request, {"runs": []})

    with (
        Client("https://control.example", transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ProtocolError, match="expected a list"),
    ):
        client.runs()
