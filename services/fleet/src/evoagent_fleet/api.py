from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .models import Approval, Completion, Failure, WorkerRegistration, WorkflowSpec
from .orchestrator import Orchestrator
from .store import Store


class Claim(BaseModel):
    worker_id: str


class Heartbeat(BaseModel):
    worker_id: str
    lease_token: str


class ResultEnvelope(BaseModel):
    worker_id: str
    lease_token: str
    result: Completion


class FailureEnvelope(BaseModel):
    worker_id: str
    lease_token: str
    failure: Failure


def create_app(
    database: Path | str = ".fleet/fleet.sqlite", artifacts: Path | str = ".fleet/artifacts"
) -> FastAPI:
    store = Store(database)
    orchestrator = Orchestrator(store, artifacts)
    app = FastAPI(title="EvoAgent Fleet", version="0.1.1")
    app.state.store = store
    app.state.orchestrator = orchestrator

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "running_workflows": len(store.query("SELECT 1 FROM workflows WHERE status='running'")),
            "online_workers": len(store.query("SELECT 1 FROM workers WHERE status='online'")),
        }

    @app.post("/v1/workflows", status_code=201)
    async def submit(spec: WorkflowSpec) -> dict[str, str]:
        return {"workflow_id": orchestrator.submit(spec)}

    @app.get("/v1/workflows")
    async def workflows(limit: int = 50) -> list[dict[str, Any]]:
        return orchestrator.list_workflows(min(max(limit, 1), 100))

    @app.get("/v1/workflows/{workflow_id}")
    async def workflow(workflow_id: str) -> dict[str, Any]:
        try:
            return orchestrator.view(workflow_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/v1/workers", status_code=201)
    async def register(worker: WorkerRegistration) -> dict[str, str]:
        orchestrator.register(worker)
        return {"worker_id": worker.worker_id, "status": "online"}

    @app.post("/v1/claims")
    async def claim(body: Claim) -> dict[str, Any]:
        try:
            return {"lease": orchestrator.claim(body.worker_id)}
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/v1/heartbeats")
    async def heartbeat(body: Heartbeat) -> dict[str, float]:
        try:
            return {"lease_expires": orchestrator.heartbeat(body.worker_id, body.lease_token)}
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/v1/completions")
    async def complete(body: ResultEnvelope) -> dict[str, Any]:
        try:
            return orchestrator.complete(body.worker_id, body.lease_token, body.result)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/v1/failures", status_code=202)
    async def fail(body: FailureEnvelope) -> dict[str, str]:
        try:
            orchestrator.fail(
                body.worker_id, body.lease_token, body.failure.error, body.failure.retryable
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"status": "recorded"}

    @app.post("/v1/workflows/{workflow_id}/nodes/{node_id}/approval")
    async def approval(workflow_id: str, node_id: str, body: Approval) -> dict[str, str]:
        try:
            orchestrator.approve(workflow_id, node_id, body.approved, body.actor)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"status": "approved" if body.approved else "denied"}

    @app.get("/v1/events")
    async def events(after: int = 0) -> list[dict[str, Any]]:
        return store.events(max(after, 0))

    @app.get("/v1/routes")
    async def routes() -> list[dict[str, Any]]:
        return orchestrator.route_metrics()

    static = Path(__file__).with_name("static")
    app.mount("/assets", StaticFiles(directory=static), name="assets")

    @app.get("/", include_in_schema=False)
    async def dashboard() -> FileResponse:
        return FileResponse(static / "index.html")

    return app
