from __future__ import annotations

import json as jsonlib
from collections.abc import Mapping
from types import TracebackType
from typing import Any, Self, TypeVar

import httpx
from evoagent_contracts import (
    AgentCreate,
    AgentSummary,
    ApprovalDecision,
    ApprovalSummary,
    ArtifactSummary,
    Overview,
    RunCreate,
    RunSummary,
    SkillSummary,
    UnifiedEvent,
    WorkflowCreate,
    WorkflowSummary,
    Workspace,
    WorkspaceCreate,
)
from pydantic import BaseModel, ValidationError

from .errors import ApiError, ProtocolError

ModelT = TypeVar("ModelT", bound=BaseModel)
Payload = BaseModel | Mapping[str, Any]


def _api_base_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if not normalized.endswith("/api/v1"):
        normalized += "/api/v1"
    return normalized + "/"


def _payload(value: Payload) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=True)
    return dict(value)


class Client:
    """Synchronous, typed EvoAgent OS control-plane client."""

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        tenant_id: str | None = None,
        workspace_id: str | None = None,
        timeout: float | httpx.Timeout = 30.0,
        headers: Mapping[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        request_headers = {
            "Accept": "application/json",
            "User-Agent": "evoagent-python/0.1.0",
        }
        if api_key:
            request_headers["Authorization"] = f"Bearer {api_key}"
        if tenant_id:
            request_headers["X-Tenant-ID"] = tenant_id
        if workspace_id:
            request_headers["X-Workspace-ID"] = workspace_id
        if headers:
            request_headers.update(headers)

        self._http = httpx.Client(
            base_url=_api_base_url(base_url),
            headers=request_headers,
            timeout=timeout,
            transport=transport,
        )

    @property
    def base_url(self) -> httpx.URL:
        return self._http.base_url

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        filtered_params = None
        if params is not None:
            filtered_params = {key: value for key, value in params.items() if value is not None}

        response = self._http.request(
            method,
            path.lstrip("/"),
            params=filtered_params,
            json=json,
            headers=headers,
        )
        if response.is_error:
            body: Any
            try:
                body = response.json()
            except ValueError:
                body = response.text
            detail = (
                body.get("detail", body.get("message", body)) if isinstance(body, dict) else body
            )
            raise ApiError(
                status_code=response.status_code,
                method=method.upper(),
                url=str(response.request.url),
                detail=str(detail),
                response_body=body,
            )

        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise ProtocolError(
                f"{method.upper()} {response.request.url} returned non-JSON success content"
            ) from exc

    @staticmethod
    def _model(model: type[ModelT], value: Any) -> ModelT:
        try:
            return model.model_validate(value)
        except ValidationError as exc:
            raise ProtocolError(f"response does not match {model.__name__}: {exc}") from exc

    @staticmethod
    def _list(model: type[ModelT], value: Any) -> list[ModelT]:
        if isinstance(value, dict) and isinstance(value.get("items"), list):
            value = value["items"]
        if not isinstance(value, list):
            raise ProtocolError(f"expected a list of {model.__name__} resources")
        return [Client._model(model, item) for item in value]

    def overview(self) -> Overview:
        return self._model(Overview, self._request("GET", "overview"))

    get_overview = overview

    def workspaces(self) -> list[Workspace]:
        return self._list(Workspace, self._request("GET", "workspaces"))

    list_workspaces = workspaces

    def create_workspace(self, workspace: WorkspaceCreate | Mapping[str, Any]) -> Workspace:
        request = (
            workspace
            if isinstance(workspace, WorkspaceCreate)
            else WorkspaceCreate.model_validate(workspace)
        )
        return self._model(
            Workspace,
            self._request("POST", "workspaces", json=_payload(request)),
        )

    def agents(self, *, workspace_id: str | None = None) -> list[AgentSummary]:
        return self._list(
            AgentSummary,
            self._request("GET", "agents", params={"workspace_id": workspace_id}),
        )

    list_agents = agents

    def create_agent(self, agent: AgentCreate | Mapping[str, Any]) -> AgentSummary:
        request = agent if isinstance(agent, AgentCreate) else AgentCreate.model_validate(agent)
        return self._model(AgentSummary, self._request("POST", "agents", json=_payload(request)))

    def runs(
        self,
        *,
        workspace_id: str | None = None,
        agent_id: str | None = None,
        status: str | None = None,
        limit: int | None = None,
    ) -> list[RunSummary]:
        return self._list(
            RunSummary,
            self._request(
                "GET",
                "runs",
                params={
                    "workspace_id": workspace_id,
                    "agent_id": agent_id,
                    "status": status,
                    "limit": limit,
                },
            ),
        )

    list_runs = runs

    def create_run(self, run: RunCreate | Mapping[str, Any]) -> RunSummary:
        request = run if isinstance(run, RunCreate) else RunCreate.model_validate(run)
        payload = _payload(request)
        idempotency_key = payload.pop("idempotency_key", None)
        if isinstance(payload["input"], dict):
            payload["input"] = jsonlib.dumps(
                payload["input"],
                ensure_ascii=False,
                separators=(",", ":"),
            )
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        return self._model(
            RunSummary,
            self._request("POST", "runs", json=payload, headers=headers),
        )

    def get_run(self, run_id: str) -> RunSummary:
        return self._model(RunSummary, self._request("GET", f"runs/{run_id}"))

    def workflows(
        self,
        *,
        workspace_id: str | None = None,
        status: str | None = None,
        limit: int | None = None,
    ) -> list[WorkflowSummary]:
        return self._list(
            WorkflowSummary,
            self._request(
                "GET",
                "workflows",
                params={"workspace_id": workspace_id, "status": status, "limit": limit},
            ),
        )

    list_workflows = workflows

    def get_workflow(self, workflow_id: str) -> WorkflowSummary:
        return self._model(
            WorkflowSummary,
            self._request("GET", f"workflows/{workflow_id}"),
        )

    def create_workflow(self, workflow: WorkflowCreate | Mapping[str, Any]) -> WorkflowSummary:
        request = (
            workflow
            if isinstance(workflow, WorkflowCreate)
            else WorkflowCreate.model_validate(workflow)
        )
        payload = _payload(request)
        idempotency_key = payload.pop("idempotency_key", None)
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        value = self._request("POST", "workflows", json=payload, headers=headers)
        if isinstance(value, dict):
            value.setdefault("workspace_id", request.workspace_id)
            value.setdefault("name", request.name)
            value.setdefault("description", request.description)
            value.setdefault("nodes", [node.model_dump(mode="json") for node in request.nodes])
            value.setdefault("input", request.input)
            value.setdefault("metadata", request.metadata)
        return self._model(WorkflowSummary, value)

    def approvals(
        self,
        *,
        workspace_id: str | None = None,
        status: str | None = None,
        limit: int | None = None,
    ) -> list[ApprovalSummary]:
        return self._list(
            ApprovalSummary,
            self._request(
                "GET",
                "approvals",
                params={"workspace_id": workspace_id, "status": status, "limit": limit},
            ),
        )

    list_approvals = approvals

    def decide_approval(
        self,
        approval_id: str,
        decision: ApprovalDecision | Mapping[str, Any] | bool,
        *,
        actor: str = "operator",
        reason: str | None = None,
    ) -> ApprovalSummary:
        if isinstance(decision, bool):
            request = ApprovalDecision(approved=decision, actor=actor, reason=reason)
        elif isinstance(decision, ApprovalDecision):
            request = decision
        else:
            request = ApprovalDecision.model_validate(decision)

        value = self._request("POST", f"approvals/{approval_id}", json=_payload(request))
        if isinstance(value, dict):
            value.setdefault("id", approval_id)
            if value.get("status") not in {"pending", "approved", "rejected", "denied"}:
                value["execution_status"] = value.get("status")
            value["status"] = "approved" if request.approved else "rejected"
            value.setdefault("action", "approval.decision")
            value["decision"] = request.model_dump(mode="json", exclude_none=True)
        return self._model(ApprovalSummary, value)

    def events(
        self,
        *,
        after: int | None = None,
        source: str | None = None,
        type: str | None = None,
        run: str | None = None,
        workflow: str | None = None,
        limit: int | None = None,
    ) -> list[UnifiedEvent]:
        return self._list(
            UnifiedEvent,
            self._request(
                "GET",
                "events",
                params={
                    "after": after,
                    "source": source,
                    "type": type,
                    "run": run,
                    "workflow": workflow,
                    "limit": limit,
                },
            ),
        )

    list_events = events

    def skills(self, *, query: str | None = None) -> list[SkillSummary]:
        return self._list(
            SkillSummary,
            self._request("GET", "skills", params={"q": query}),
        )

    list_skills = skills

    def artifacts(self, *, workflow_id: str | None = None) -> list[ArtifactSummary]:
        return self._list(
            ArtifactSummary,
            self._request("GET", "artifacts", params={"workflow_id": workflow_id}),
        )

    list_artifacts = artifacts

    def demo(
        self,
        payload: Mapping[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        value = self._request(
            "POST",
            "demo/launch",
            json=dict(payload or {}),
            headers=headers,
        )
        if not isinstance(value, dict):
            raise ProtocolError("expected the demo launch response to be an object")
        return value

    launch_demo = demo
