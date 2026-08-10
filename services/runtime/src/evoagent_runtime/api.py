from __future__ import annotations

import hashlib
import hmac
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .evolution import EvolutionEngine
from .models import ApprovalDecision, Envelope, EvolutionScenario, Feedback
from .providers import OfflineProvider, OpenAICompatibleProvider
from .runtime import AgentRuntime
from .scheduler import Scheduler
from .store import Store


class JobCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    interval_seconds: int = Field(ge=5, le=31_536_000)
    message: str = Field(min_length=1, max_length=100_000)


class EvolutionEvaluation(BaseModel):
    scenarios: list[EvolutionScenario]


def create_app(
    database: Path | str | None = None,
    workspace: Path | str | None = None,
    provider_mode: str | None = None,
    gateway_token: str | None = None,
    allowed_hosts: set[str] | None = None,
) -> FastAPI:
    state_dir = Path(os.getenv("EVOAGENT_STATE_DIR", ".evoagent"))
    store = Store(database or state_dir / "runtime.sqlite")
    mode = provider_mode or os.getenv("EVOAGENT_PROVIDER", "offline")
    provider = OpenAICompatibleProvider() if mode == "openai" else OfflineProvider()
    runtime = AgentRuntime(
        store,
        Path(workspace or os.getenv("EVOAGENT_WORKSPACE", ".evoagent/workspace")),
        provider,
        allowed_hosts=allowed_hosts
        or {
            host.strip()
            for host in os.getenv("EVOAGENT_HTTP_ALLOWLIST", "").split(",")
            if host.strip()
        },
    )
    scheduler = Scheduler(store, runtime)
    evolution = EvolutionEngine(store, provider)
    token = gateway_token if gateway_token is not None else os.getenv("EVOAGENT_GATEWAY_TOKEN", "")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        del app
        await scheduler.start()
        yield
        await scheduler.stop()
        store.close()

    app = FastAPI(
        title="EvoAgent Runtime",
        version="0.1.0",
        description="Local-first agent gateway with governed self-evolution",
        lifespan=lifespan,
    )
    app.state.store = store
    app.state.runtime = runtime
    app.state.scheduler = scheduler
    app.state.evolution = evolution
    app.state.gateway_token = token

    def authorize(authorization: Annotated[str | None, Header()] = None) -> None:
        if token and authorization != f"Bearer {token}":
            raise HTTPException(status_code=401, detail="Invalid gateway token")

    auth = [Depends(authorize)]

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "provider": mode,
            "prompt_version": store.active_prompt()[0],
            "pending_approvals": len(store.pending_approvals()),
        }

    @app.post("/v1/messages", dependencies=auth, status_code=202)
    async def submit_message(envelope: Envelope) -> dict[str, str]:
        run_id = await runtime.submit(envelope)
        return {"run_id": run_id, "status": "accepted"}

    @app.post("/v1/messages/wait", dependencies=auth)
    async def message_wait(envelope: Envelope) -> dict[str, Any]:
        return (await runtime.run_once(envelope)).model_dump()

    @app.get("/v1/runs", dependencies=auth)
    async def runs(limit: int = 50) -> list[dict[str, Any]]:
        return store.list_runs(min(max(limit, 1), 200))

    @app.get("/v1/runs/{run_id}", dependencies=auth)
    async def run(run_id: str) -> dict[str, Any]:
        try:
            return runtime.run_view(run_id).model_dump()
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/v1/sessions/{session_id}/history", dependencies=auth)
    async def history(session_id: str, limit: int = 50) -> list[dict[str, Any]]:
        return store.history(session_id, min(max(limit, 1), 200))

    @app.get("/v1/events", dependencies=auth)
    async def events(after: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        return store.events(max(after, 0), min(max(limit, 1), 500))

    @app.get("/v1/approvals", dependencies=auth)
    async def approvals() -> list[dict[str, Any]]:
        return store.pending_approvals()

    @app.post("/v1/approvals/{approval_id}", dependencies=auth)
    async def decide(approval_id: str, decision: ApprovalDecision) -> dict[str, Any]:
        try:
            return (
                await runtime.decide_approval(approval_id, decision.approved, decision.actor)
            ).model_dump()
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/v1/runs/{run_id}/feedback", dependencies=auth)
    async def feedback(run_id: str, body: Feedback) -> dict[str, str]:
        if store.get_run(run_id) is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return {"feedback_id": store.add_feedback(run_id, body.rating, body.comment)}

    @app.get("/v1/jobs", dependencies=auth)
    async def jobs() -> list[dict[str, object]]:
        return scheduler.list()

    @app.post("/v1/jobs", dependencies=auth, status_code=201)
    async def create_job(body: JobCreate) -> dict[str, str]:
        return {"job_id": scheduler.create(body.name, body.interval_seconds, body.message)}

    @app.get("/v1/evolution/candidates", dependencies=auth)
    async def candidates() -> list[dict[str, object]]:
        return evolution.candidates()

    @app.post("/v1/evolution/candidates", dependencies=auth, status_code=201)
    async def propose() -> dict[str, Any]:
        return evolution.propose().model_dump()

    @app.post("/v1/evolution/candidates/{candidate_id}/evaluate", dependencies=auth)
    async def evaluate(candidate_id: str, body: EvolutionEvaluation) -> dict[str, Any]:
        try:
            return (await evolution.evaluate(candidate_id, body.scenarios)).model_dump()
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/v1/evolution/candidates/{candidate_id}/promote", dependencies=auth)
    async def promote(candidate_id: str) -> dict[str, int]:
        try:
            return {"prompt_version": evolution.promote(candidate_id)}
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/v1/channels/webhook/{channel}", dependencies=auth, status_code=202)
    async def webhook(
        channel: str,
        request: Request,
        signature: Annotated[str | None, Header(alias="X-EvoAgent-Signature")] = None,
    ) -> dict[str, str]:
        body = await request.body()
        secret = os.getenv(f"EVOAGENT_CHANNEL_{channel.upper()}_SECRET", "")
        if secret:
            expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
            if not signature or not hmac.compare_digest(signature, expected):
                raise HTTPException(status_code=401, detail="Invalid channel signature")
        payload = await request.json()
        envelope = Envelope(
            channel=channel,
            peer_id=str(payload.get("peer_id", "webhook")),
            text=str(payload.get("text", "")),
            metadata=payload.get("metadata") or {},
        )
        return {"run_id": await runtime.submit(envelope), "status": "accepted"}

    @app.websocket("/ws")
    async def websocket_gateway(socket: WebSocket) -> None:
        await socket.accept()
        try:
            first = await socket.receive_json()
            if first.get("type") != "connect" or (token and first.get("token") != token):
                await socket.send_json({"type": "error", "error": "connect/auth required"})
                await socket.close(code=1008)
                return
            await socket.send_json(
                {
                    "type": "hello-ok",
                    "methods": ["agent", "run.get", "events", "approvals"],
                    "version": "0.1.0",
                }
            )
            while True:
                frame = await socket.receive_json()
                request_id = frame.get("id")
                method = frame.get("method")
                params = frame.get("params") or {}
                if method == "agent":
                    payload = {
                        "run_id": await runtime.submit(Envelope(**params)),
                        "status": "accepted",
                    }
                elif method == "run.get":
                    payload = runtime.run_view(params["run_id"]).model_dump()
                elif method == "events":
                    payload = store.events(int(params.get("after", 0)))
                elif method == "approvals":
                    payload = store.pending_approvals()
                else:
                    await socket.send_json(
                        {"type": "res", "id": request_id, "ok": False, "error": "unknown method"}
                    )
                    continue
                await socket.send_json(
                    {"type": "res", "id": request_id, "ok": True, "payload": payload}
                )
        except WebSocketDisconnect:
            return

    static_dir = Path(__file__).with_name("static")
    app.mount("/assets", StaticFiles(directory=static_dir), name="assets")

    @app.get("/", include_in_schema=False)
    async def dashboard() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    return app
