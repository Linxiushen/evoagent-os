from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .bootstrap import activate_workspace_packages

activate_workspace_packages()

from evoagent_fleet.models import (  # noqa: E402
    Budget,
    Completion,
    NodeSpec,
    WorkerRegistration,
    WorkflowSpec,
)
from evoagent_fleet.orchestrator import Orchestrator  # noqa: E402
from evoagent_fleet.store import Store as FleetStore  # noqa: E402
from evoagent_forge.registry import Registry  # noqa: E402
from evoagent_runtime.evolution import EvolutionEngine  # noqa: E402
from evoagent_runtime.models import Envelope, EvolutionScenario  # noqa: E402
from evoagent_runtime.providers import OfflineProvider, OpenAICompatibleProvider  # noqa: E402
from evoagent_runtime.runtime import AgentRuntime  # noqa: E402
from evoagent_runtime.scheduler import Scheduler  # noqa: E402
from evoagent_runtime.store import Store as RuntimeStore  # noqa: E402

from .demo import DemoCoordinator  # noqa: E402
from .models import (  # noqa: E402
    AgentCreate,
    ApprovalDecision,
    FeedbackCreate,
    JobCreate,
    LeaseCompletion,
    LeaseFailure,
    LeaseHeartbeat,
    RunCreate,
    WorkerCreate,
    WorkflowCreate,
    WorkspaceCreate,
)
from .store import ControlStore  # noqa: E402

BUNDLED_SKILLS = [
    {
        "name": "durable-research",
        "version": "0.1.0",
        "description": "Evidence-led research with bounded retrieval and citation provenance",
        "capabilities": ["research", "citations"],
        "publisher": "evoagent-os",
        "trust": "bundled",
        "signature": "repository-provenance",
    },
    {
        "name": "artifact-publisher",
        "version": "0.1.0",
        "description": "Approval-gated publication of content-addressed deliverables",
        "capabilities": ["artifacts", "filesystem.write"],
        "publisher": "evoagent-os",
        "trust": "bundled",
        "signature": "repository-provenance",
    },
    {
        "name": "trace-regression",
        "version": "0.1.0",
        "description": "Executable lifecycle, tool, and policy trace regression gates",
        "capabilities": ["evaluation", "trace-contract"],
        "publisher": "harnesslab",
        "trust": "bundled",
        "signature": "repository-provenance",
    },
]


def _parse_json(value: Any, fallback: Any) -> Any:
    if not value:
        return fallback
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def create_app(
    state_dir: Path | str | None = None,
    gateway_token: str | None = None,
    provider_mode: str | None = None,
) -> FastAPI:
    root = Path(state_dir or os.getenv("EVOAGENT_OS_STATE_DIR", ".evoagent-os")).resolve()
    root.mkdir(parents=True, exist_ok=True)

    control = ControlStore(root / "control.sqlite")
    runtime_store = RuntimeStore(root / "runtime.sqlite")
    mode = provider_mode or os.getenv("EVOAGENT_PROVIDER", "offline")
    provider = OpenAICompatibleProvider() if mode == "openai" else OfflineProvider()
    runtime = AgentRuntime(runtime_store, root / "workspace", provider)
    scheduler = Scheduler(runtime_store, runtime)
    fleet_store = FleetStore(root / "fleet.sqlite")
    orchestrator = Orchestrator(fleet_store, root / "artifacts")
    forge = Registry(root / "registry")
    evolution = EvolutionEngine(runtime_store, provider)
    demo = DemoCoordinator(orchestrator)
    token = gateway_token if gateway_token is not None else os.getenv("EVOAGENT_OS_TOKEN", "")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await scheduler.start()
        yield
        await scheduler.stop()
        pending = list(runtime._tasks)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        forge.close()
        fleet_store.close()
        runtime_store.close()
        control.close()

    app = FastAPI(
        title="EvoAgent OS",
        version="0.1.0",
        description="Governed control plane for durable, auditable, self-evolving agent teams",
        lifespan=lifespan,
        openapi_tags=[
            {"name": "operations", "description": "Fleet-wide status and events"},
            {"name": "catalog", "description": "Workspaces, agents, and signed skills"},
            {"name": "execution", "description": "Agent runs and durable workflows"},
            {"name": "governance", "description": "Approvals, evaluation, and evolution"},
        ],
    )
    app.state.control = control
    app.state.runtime_store = runtime_store
    app.state.runtime = runtime
    app.state.scheduler = scheduler
    app.state.fleet_store = fleet_store
    app.state.orchestrator = orchestrator
    app.state.forge = forge
    app.state.evolution = evolution
    app.state.demo = demo
    app.state.root = root

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or f"req_{uuid4().hex[:20]}"
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), geolocation=(), microphone=(self)"
        return response

    def authorize(authorization: Annotated[str | None, Header()] = None) -> None:
        if token and authorization != f"Bearer {token}":
            raise HTTPException(status_code=401, detail="Invalid control-plane token")

    router = APIRouter(prefix="/api/v1", dependencies=[Depends(authorize)])

    @app.get("/health", tags=["operations"])
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": "0.1.0",
            "mode": mode,
            "components": {
                "control_plane": "ok",
                "runtime": "ok",
                "fleet": "ok",
                "forge": "ok",
                "observability": "ok",
                "realtime": "available",
            },
        }

    def skills() -> list[dict[str, Any]]:
        published = forge.search("", 100)
        normalized = [
            {
                **release["manifest"],
                "digest": release["digest"],
                "published_at": release["published_at"],
                "trust": "signed" if release.get("signature") else "unsigned",
                "signature": release.get("signature"),
            }
            for release in published
        ]
        return [*BUNDLED_SKILLS, *normalized]

    def runs(limit: int = 100) -> list[dict[str, Any]]:
        links = control.run_links()
        result = []
        for row in runtime_store.list_runs(min(max(limit, 1), 200)):
            link = links.get(row["run_id"], {})
            row["usage"] = _parse_json(row.pop("usage_json", None), {})
            row.update(
                {
                    "id": row["run_id"],
                    "workspace_id": link.get("workspace_id"),
                    "agent_id": link.get("agent_id"),
                    "objective": link.get("objective", row["input_text"]),
                }
            )
            result.append(row)
        return result

    def workflow_list(limit: int = 100) -> list[dict[str, Any]]:
        values = orchestrator.list_workflows(min(max(limit, 1), 100))
        for value in values:
            value["id"] = value["workflow_id"]
            value["metadata"] = _parse_json(value.pop("metadata_json", None), {})
            nodes = fleet_store.query(
                """SELECT status,COUNT(*) AS count,SUM(tokens_used) AS tokens,SUM(cost_usd) AS cost,
                   SUM(duration_seconds) AS duration FROM nodes
                   WHERE workflow_id=? GROUP BY status""",
                (value["workflow_id"],),
            )
            value["node_counts"] = {item["status"]: item["count"] for item in nodes}
            value["node_total"] = sum(item["count"] for item in nodes)
            value["tokens_used"] = sum(item["tokens"] or 0 for item in nodes)
            value["cost_usd"] = round(sum(item["cost"] or 0 for item in nodes), 4)
            value["duration_seconds"] = round(sum(item["duration"] or 0 for item in nodes), 3)
        return values

    def approval_list() -> list[dict[str, Any]]:
        values = []
        for item in runtime_store.pending_approvals():
            values.append(
                {
                    "approval_id": item["approval_id"],
                    "id": item["approval_id"],
                    "source": "runtime",
                    "status": item["status"],
                    "subject": f"Run tool: {item['tool_name']}",
                    "reason": item["reason"],
                    "risk": "high"
                    if item["tool_name"] in {"workspace.write", "http.get"}
                    else "medium",
                    "run_id": item["run_id"],
                    "workflow_id": None,
                    "node_id": None,
                    "arguments": _parse_json(item.get("args_json"), {}),
                    "created_at": item["created_at"],
                }
            )
        nodes = fleet_store.query(
            """SELECT n.*,w.name AS workflow_name FROM nodes n JOIN workflows w USING(workflow_id)
               WHERE n.status='awaiting_approval' ORDER BY n.created_at"""
        )
        for node in nodes:
            approval_id = f"fleet:{node['workflow_id']}:{node['node_id']}"
            values.append(
                {
                    "approval_id": approval_id,
                    "id": approval_id,
                    "source": "fleet",
                    "status": "pending",
                    "subject": f"Publish node: {node['objective']}",
                    "reason": "Workflow policy requires operator approval before release",
                    "risk": "high",
                    "run_id": None,
                    "workflow_id": node["workflow_id"],
                    "workflow_name": node["workflow_name"],
                    "node_id": node["node_id"],
                    "arguments": _parse_json(node["input_json"], {}),
                    "created_at": node["created_at"],
                }
            )
        return values

    def unified_events(limit: int = 200) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        for item in control.events(limit):
            values.append(
                {
                    "id": item["event_id"],
                    "sequence": item["seq"],
                    "source": "control",
                    "type": item["kind"],
                    "workspace_id": item["workspace_id"],
                    "run_id": item["run_id"],
                    "workflow_id": item["workflow_id"],
                    "node_id": None,
                    "payload": item["payload"],
                    "created_at": item["created_at"],
                }
            )
        for item in runtime_store.events(0, limit):
            values.append(
                {
                    "id": f"runtime:{item['seq']}",
                    "sequence": item["seq"],
                    "source": "runtime",
                    "type": item["kind"],
                    "workspace_id": control.run_links()
                    .get(item.get("run_id"), {})
                    .get("workspace_id"),
                    "run_id": item.get("run_id"),
                    "workflow_id": None,
                    "node_id": None,
                    "payload": item["payload"],
                    "created_at": item["created_at"],
                }
            )
        for item in fleet_store.events(0, limit):
            values.append(
                {
                    "id": f"fleet:{item['seq']}",
                    "sequence": item["seq"],
                    "source": "fleet",
                    "type": item["kind"],
                    "workspace_id": None,
                    "run_id": None,
                    "workflow_id": item.get("workflow_id"),
                    "node_id": item.get("node_id"),
                    "payload": item["payload"],
                    "created_at": item["created_at"],
                }
            )
        return sorted(values, key=lambda item: item["created_at"], reverse=True)[:limit]

    def metrics() -> dict[str, Any]:
        run_values = runs(200)
        workflow_nodes = fleet_store.query(
            "SELECT status,tokens_used,cost_usd,duration_seconds FROM nodes"
        )
        tokens = sum(
            int(run.get("usage", {}).get("input_tokens", 0))
            + int(run.get("usage", {}).get("output_tokens", 0))
            for run in run_values
        )
        tokens += sum(int(node["tokens_used"] or 0) for node in workflow_nodes)
        cost = sum(float(node["cost_usd"] or 0) for node in workflow_nodes)
        terminal = [run for run in run_values if run["status"] in {"completed", "failed"}]
        successful = [run for run in terminal if run["status"] == "completed"]
        completed_nodes = [node for node in workflow_nodes if node["status"] == "completed"]
        failed_nodes = [node for node in workflow_nodes if node["status"] == "failed"]
        denominator = len(terminal) + len(completed_nodes) + len(failed_nodes)
        success_count = len(successful) + len(completed_nodes)
        route_values = orchestrator.route_metrics()
        quality_runs = sum(int(item["runs"]) for item in route_values)
        quality = (
            sum(float(item["quality_sum"]) for item in route_values) / quality_runs
            if quality_runs
            else 1.0
        )
        durations = [float(node["duration_seconds"]) for node in completed_nodes]
        return {
            "tokens": tokens,
            "cost_usd": round(cost, 4),
            "success_rate": round(success_count / denominator, 4) if denominator else 1.0,
            "quality": round(quality, 4),
            "latency_ms": round(1000 * sum(durations) / len(durations), 1) if durations else 0,
        }

    @router.get("/overview", tags=["operations"])
    async def overview() -> dict[str, Any]:
        run_values = runs(20)
        workflows = workflow_list(20)
        approvals = approval_list()
        skill_values = skills()
        workers = fleet_store.query("SELECT * FROM workers WHERE status='online'")
        return {
            "counts": {
                "workspaces": len(control.list_workspaces()),
                "agents": len(control.list_agents()),
                "runs": len(runtime_store.query("SELECT 1 FROM runs")),
                "active_workflows": sum(item["status"] == "running" for item in workflows),
                "pending_approvals": len(approvals),
                "skills": len(skill_values),
                "online_workers": len(workers),
            },
            "metrics": metrics(),
            "recent_runs": run_values[:8],
            "workflows": workflows[:8],
            "approvals": approvals[:8],
            "route_metrics": orchestrator.route_metrics(),
            "events": unified_events(16),
        }

    @router.get("/workspaces", tags=["catalog"])
    async def list_workspaces() -> list[dict[str, Any]]:
        return control.list_workspaces()

    @router.post("/workspaces", status_code=201, tags=["catalog"])
    async def create_workspace(body: WorkspaceCreate) -> dict[str, Any]:
        return control.create_workspace(body.name, body.description, body.tenant_id)

    @router.get("/agents", tags=["catalog"])
    async def list_agents(workspace_id: str | None = None) -> list[dict[str, Any]]:
        return control.list_agents(workspace_id)

    @router.post("/agents", status_code=201, tags=["catalog"])
    async def create_agent(body: AgentCreate) -> dict[str, Any]:
        try:
            return control.create_agent(body.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/runs", tags=["execution"])
    async def list_runs(limit: int = 100) -> list[dict[str, Any]]:
        return runs(limit)

    @router.get("/runs/{run_id}", tags=["execution"])
    async def get_run(run_id: str) -> dict[str, Any]:
        value = next((item for item in runs(200) if item["run_id"] == run_id), None)
        if value is None:
            raise HTTPException(status_code=404, detail="Run not found")
        value["events"] = [event for event in unified_events(500) if event["run_id"] == run_id]
        return value

    @router.post("/runs", status_code=202, tags=["execution"])
    async def create_run(
        body: RunCreate,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        cached = control.idempotent_get("runs", idempotency_key)
        if cached:
            return cached
        agents = {item["agent_id"]: item for item in control.list_agents(body.workspace_id)}
        if body.agent_id not in agents:
            raise HTTPException(status_code=404, detail="Agent not found in workspace")
        envelope = Envelope(
            channel="control-plane",
            peer_id=body.agent_id,
            session_id=body.session_id,
            text=body.input,
            metadata={
                **body.metadata,
                "workspace_id": body.workspace_id,
                "agent_id": body.agent_id,
            },
        )
        if body.wait:
            value = (await runtime.run_once(envelope)).model_dump(mode="json")
            run_id = value["run_id"]
        else:
            run_id = await runtime.submit(envelope)
            value = {"run_id": run_id, "status": "accepted"}
        control.link_run(run_id, body.workspace_id, body.agent_id, body.input)
        control.event(
            "run.submitted",
            {"agent_id": body.agent_id, "wait": body.wait},
            workspace_id=body.workspace_id,
            run_id=run_id,
        )
        response = {
            **value,
            "id": run_id,
            "workspace_id": body.workspace_id,
            "agent_id": body.agent_id,
        }
        control.idempotent_put("runs", idempotency_key, response)
        return response

    @router.post("/runs/{run_id}/feedback", tags=["governance"])
    async def create_feedback(run_id: str, body: FeedbackCreate) -> dict[str, str]:
        if runtime_store.get_run(run_id) is None:
            raise HTTPException(status_code=404, detail="Run not found")
        feedback_id = runtime_store.add_feedback(run_id, body.rating, body.comment)
        return {"feedback_id": feedback_id}

    @router.get("/sessions/{session_id}/history", tags=["execution"])
    async def session_history(session_id: str, limit: int = 100) -> list[dict[str, Any]]:
        return runtime_store.history(session_id, min(max(limit, 1), 200))

    @router.get("/memory/search", tags=["execution"])
    async def search_memory(q: str, limit: int = 10) -> list[dict[str, Any]]:
        return runtime_store.search_memory(q, min(max(limit, 1), 50))

    @router.get("/jobs", tags=["execution"])
    async def list_jobs() -> list[dict[str, object]]:
        return scheduler.list()

    @router.post("/jobs", status_code=201, tags=["execution"])
    async def create_job(body: JobCreate) -> dict[str, str]:
        return {"job_id": scheduler.create(body.name, body.interval_seconds, body.message)}

    @router.get("/workflows", tags=["execution"])
    async def list_workflows(limit: int = 100) -> list[dict[str, Any]]:
        return workflow_list(limit)

    @router.get("/workflows/{workflow_id}", tags=["execution"])
    async def get_workflow(workflow_id: str) -> dict[str, Any]:
        try:
            value = orchestrator.view(workflow_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        value["id"] = workflow_id
        value["artifacts"] = artifact_list(workflow_id)
        value["events"] = [
            event for event in unified_events(500) if event["workflow_id"] == workflow_id
        ]
        return value

    @router.post("/workflows", status_code=201, tags=["execution"])
    async def create_workflow(
        body: WorkflowCreate,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        cached = control.idempotent_get("workflows", idempotency_key)
        if cached:
            return cached
        nodes = [
            NodeSpec(
                id=node.id,
                objective=node.objective,
                depends_on=node.depends_on,
                capabilities=node.capabilities,
                input=node.input,
                budget=Budget(tokens=node.tokens, cost_usd=node.cost_usd, seconds=node.seconds),
                approval_required=node.approval_required,
                max_attempts=node.max_attempts,
            )
            for node in body.nodes
        ]
        metadata = {**body.metadata, "workspace_id": body.workspace_id}
        try:
            workflow_id = orchestrator.submit(
                WorkflowSpec(name=body.name, nodes=nodes, metadata=metadata)
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        control.event(
            "workflow.submitted",
            {"name": body.name, "nodes": len(nodes)},
            workspace_id=body.workspace_id,
            workflow_id=workflow_id,
        )
        response = {"workflow_id": workflow_id, "id": workflow_id, "status": "running"}
        control.idempotent_put("workflows", idempotency_key, response)
        return response

    @router.get("/approvals", tags=["governance"])
    async def list_approvals() -> list[dict[str, Any]]:
        return approval_list()

    async def decide_approval(approval_id: str, body: ApprovalDecision) -> dict[str, Any]:
        if approval_id.startswith("fleet:"):
            parts = approval_id.split(":", 2)
            if len(parts) != 3:
                raise HTTPException(status_code=404, detail="Approval not found")
            _, workflow_id, node_id = parts
            try:
                orchestrator.approve(workflow_id, node_id, body.approved, body.actor)
                completed = demo.drain(workflow_id) if body.approved else []
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            control.event(
                "approval.decided",
                {"approval_id": approval_id, "approved": body.approved, "actor": body.actor},
                workflow_id=workflow_id,
            )
            return {
                "approval_id": approval_id,
                "approved": body.approved,
                "workflow_id": workflow_id,
                "completed_nodes": completed,
                "status": orchestrator.view(workflow_id)["status"],
            }
        try:
            value = await runtime.decide_approval(approval_id, body.approved, body.actor)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "approval_id": approval_id,
            "approved": body.approved,
            "run": value.model_dump(mode="json"),
        }

    @router.post("/approvals/{approval_id}", tags=["governance"])
    async def approval_decision(approval_id: str, body: ApprovalDecision) -> dict[str, Any]:
        return await decide_approval(approval_id, body)

    @router.post("/approvals/{approval_id}/decision", include_in_schema=False)
    async def approval_decision_alias(approval_id: str, body: ApprovalDecision) -> dict[str, Any]:
        return await decide_approval(approval_id, body)

    @router.get("/skills", tags=["catalog"])
    async def list_skills(q: str = "") -> list[dict[str, Any]]:
        values = skills()
        lowered = q.lower().strip()
        if lowered:
            values = [
                item
                for item in values
                if lowered in item.get("name", "").lower()
                or lowered in item.get("description", "").lower()
            ]
        return values

    @router.get("/tools", tags=["catalog"])
    async def list_tools() -> list[dict[str, Any]]:
        values = []
        for tool in runtime.tools._tools.values():
            decision = runtime.tools.policy.decide(tool)
            values.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                    "risk": tool.risk.value,
                    "policy": decision.action,
                    "policy_reason": decision.reason,
                }
            )
        return values

    @router.get("/policies", tags=["governance"])
    async def policies() -> dict[str, Any]:
        return {
            "default": "deny-undeclared",
            "approval_required": ["medium", "high"],
            "network": {
                "scheme": "https-only",
                "allowlist": sorted(runtime.allowed_hosts),
                "private_addresses": "denied",
            },
            "filesystem": {
                "root": "workspace-scoped",
                "path_escape": "denied",
                "write_approval": "required",
            },
        }

    def artifact_list(workflow_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM artifacts"
        values: tuple[Any, ...] = ()
        if workflow_id:
            sql += " WHERE workflow_id=?"
            values = (workflow_id,)
        rows = fleet_store.query(sql + " ORDER BY created_at DESC", values)
        for row in rows:
            row["sha256"] = row.pop("digest")
            row["download_url"] = f"/api/v1/artifacts/{row['artifact_id']}/content"
            row.pop("path", None)
        return rows

    @router.get("/artifacts", tags=["execution"])
    async def list_artifacts(workflow_id: str | None = None) -> list[dict[str, Any]]:
        return artifact_list(workflow_id)

    @router.get("/artifacts/{artifact_id}/content", tags=["execution"])
    async def artifact_content(artifact_id: str) -> FileResponse:
        rows = fleet_store.query("SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,))
        if not rows:
            raise HTTPException(status_code=404, detail="Artifact not found")
        value = rows[0]
        path = Path(value["path"])
        if not path.is_file():  # noqa: ASYNC240 - FileResponse reads the same local path.
            raise HTTPException(status_code=410, detail="Artifact content is unavailable")
        return FileResponse(path, filename=value["name"], media_type="application/octet-stream")

    @router.get("/workers", tags=["operations"])
    async def list_workers() -> list[dict[str, Any]]:
        values = fleet_store.query("SELECT * FROM workers ORDER BY pool,worker_id")
        for value in values:
            value["capabilities"] = _parse_json(value.pop("capabilities_json"), [])
            value["metadata"] = _parse_json(value.pop("metadata_json"), {})
        return values

    @router.post("/workers", status_code=201, tags=["operations"])
    async def register_worker(body: WorkerCreate) -> dict[str, Any]:
        orchestrator.register(WorkerRegistration(**body.model_dump()))
        control.event(
            "worker.registered",
            {"worker_id": body.worker_id, "pool": body.pool, "capabilities": body.capabilities},
        )
        return {"worker_id": body.worker_id, "status": "online"}

    @router.post("/workers/{worker_id}/claims", tags=["execution"])
    async def claim_work(worker_id: str) -> dict[str, Any]:
        try:
            return {"lease": orchestrator.claim(worker_id)}
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/leases/{lease_token}/heartbeat", tags=["execution"])
    async def heartbeat_lease(lease_token: str, body: LeaseHeartbeat) -> dict[str, float]:
        try:
            expires = orchestrator.heartbeat(body.worker_id, lease_token)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"lease_expires": expires}

    @router.post("/leases/{lease_token}/completion", tags=["execution"])
    async def complete_lease(lease_token: str, body: LeaseCompletion) -> dict[str, Any]:
        try:
            return orchestrator.complete(
                body.worker_id,
                lease_token,
                Completion(
                    output=body.output,
                    artifacts=body.artifacts,
                    tokens_used=body.tokens_used,
                    cost_usd=body.cost_usd,
                    duration_seconds=body.duration_seconds,
                    quality=body.quality,
                ),
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/leases/{lease_token}/failure", status_code=202, tags=["execution"])
    async def fail_lease(lease_token: str, body: LeaseFailure) -> dict[str, str]:
        try:
            orchestrator.fail(
                body.worker_id,
                lease_token,
                body.error,
                body.retryable,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"status": "recorded"}

    @router.get("/routes", tags=["operations"])
    async def list_routes() -> list[dict[str, Any]]:
        return orchestrator.route_metrics()

    @router.get("/events", tags=["operations"])
    async def list_events(limit: int = 200, source: str | None = None) -> list[dict[str, Any]]:
        values = unified_events(min(max(limit, 1), 500))
        return [item for item in values if item["source"] == source] if source else values

    @router.get("/events/stream", tags=["operations"])
    async def event_stream(request: Request) -> StreamingResponse:
        async def generate():
            seen: set[str] = set()
            while not await request.is_disconnected():
                for event in reversed(unified_events(100)):
                    if event["id"] not in seen:
                        seen.add(event["id"])
                        event_json = json.dumps(event)
                        yield (f"id: {event['id']}\nevent: {event['type']}\ndata: {event_json}\n\n")
                if len(seen) > 2_000:
                    seen = set(list(seen)[-1_000:])
                await asyncio.sleep(1)

        return StreamingResponse(generate(), media_type="text/event-stream")

    @router.get("/evaluations", tags=["governance"])
    async def list_evaluations() -> list[dict[str, Any]]:
        return control.evaluations()

    @router.get("/evolution/candidates", tags=["governance"])
    async def evolution_candidates() -> list[dict[str, object]]:
        return evolution.candidates()

    @router.post("/evolution/candidates", status_code=201, tags=["governance"])
    async def propose_evolution() -> dict[str, Any]:
        return evolution.propose().model_dump(mode="json")

    @router.post("/evolution/candidates/{candidate_id}/evaluate", tags=["governance"])
    async def evaluate_evolution(
        candidate_id: str, scenarios: list[EvolutionScenario]
    ) -> dict[str, Any]:
        try:
            return (await evolution.evaluate(candidate_id, scenarios)).model_dump(mode="json")
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/evolution/candidates/{candidate_id}/promote", tags=["governance"])
    async def promote_evolution(candidate_id: str) -> dict[str, int]:
        try:
            return {"prompt_version": evolution.promote(candidate_id)}
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    async def launch_demo(
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        cached = control.idempotent_get("demo", idempotency_key)
        if cached:
            return cached
        run = await runtime.run_once(
            Envelope(
                channel="demo",
                peer_id="agent_coordinator",
                text="Plan and govern an evidence-linked enterprise AI agent market brief",
                metadata={"workspace_id": "ws_default", "scenario": "market-research"},
            )
        )
        control.link_run(run.run_id, "ws_default", "agent_coordinator", run.input_text)
        workflow = demo.launch("ws_default")
        control.event(
            "demo.launched",
            {"scenario": "market-research", "run_id": run.run_id},
            workspace_id="ws_default",
            run_id=run.run_id,
            workflow_id=workflow["workflow_id"],
        )
        response = {
            **workflow,
            "run_id": run.run_id,
            "workspace_id": "ws_default",
            "next_action": "Approve the publish node to resume and emit final artifacts",
        }
        control.idempotent_put("demo", idempotency_key, response)
        return response

    @router.post("/demo/launch", status_code=201, tags=["execution"])
    async def demo_launch(
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        return await launch_demo(idempotency_key)

    @router.post("/demo", status_code=201, include_in_schema=False)
    async def demo_alias(
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        return await launch_demo(idempotency_key)

    @router.get("/system/capabilities", tags=["operations"])
    async def capabilities() -> dict[str, Any]:
        return {
            "protocol": "evoagent.control/v1",
            "features": [
                "durable-sessions",
                "tool-policy",
                "human-approval",
                "workflow-dag",
                "worker-leases",
                "budget-enforcement",
                "content-addressed-artifacts",
                "signed-skill-registry",
                "trace-contract-evaluation",
                "governed-evolution",
                "realtime-agent-runtime",
            ],
            "transports": ["http", "sse", "websocket"],
            "provider_mode": mode,
        }

    app.include_router(router)

    @app.get("/.well-known/agent-card.json", tags=["catalog"])
    async def agent_card() -> dict[str, Any]:
        return {
            "name": "EvoAgent OS Coordinator",
            "description": "Governed durable execution for auditable agent teams",
            "url": "/api/v1/runs",
            "version": "0.1.0",
            "protocolVersion": "0.3.0",
            "capabilities": {
                "streaming": True,
                "pushNotifications": False,
                "stateTransitionHistory": True,
            },
            "defaultInputModes": ["text/plain", "application/json"],
            "defaultOutputModes": ["text/plain", "application/json", "text/markdown"],
            "skills": [
                {"id": item["name"], "name": item["name"], "description": item["description"]}
                for item in BUNDLED_SKILLS
            ],
        }

    @app.websocket("/ws")
    async def websocket_events(socket: WebSocket) -> None:
        authorization = socket.headers.get("authorization")
        if token and authorization != f"Bearer {token}":
            await socket.close(code=1008)
            return
        await socket.accept()
        seen: set[str] = set()
        try:
            while True:
                for event in reversed(unified_events(100)):
                    if event["id"] not in seen:
                        seen.add(event["id"])
                        await socket.send_json({"type": "event", "event": event})
                await asyncio.sleep(1)
        except Exception:  # noqa: BLE001 - disconnects vary by ASGI server.
            return

    static = Path(__file__).with_name("static")
    if static.is_dir():
        app.mount("/assets", StaticFiles(directory=static), name="assets")

        @app.get("/", include_in_schema=False)
        async def console() -> FileResponse:
            return FileResponse(static / "index.html")
    else:

        @app.get("/", include_in_schema=False)
        async def root_status() -> JSONResponse:
            return JSONResponse({"name": "EvoAgent OS", "docs": "/docs"})

    return app
