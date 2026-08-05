from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from harnesslab import __version__
from harnesslab.adapters import DeepSeekAPIAdapter, DemoAdapter, OpenAICompatibleAdapter
from harnesslab.conformance import ConformanceReport, run_conformance
from harnesslab.models import RunRecord, RunRequest
from harnesslab.runtime import HarnessRuntime
from harnesslab.tools import build_demo_registry

STATIC_DIR = Path(__file__).parent / "static"
TERMINAL_EVENTS = {"run.completed", "run.failed", "run.cancelled"}


def build_runtime() -> HarnessRuntime:
    runtime = HarnessRuntime(build_demo_registry())
    runtime.register_adapter(DemoAdapter())
    if api_key := os.getenv("DEEPSEEK_API_KEY"):
        runtime.register_adapter(DeepSeekAPIAdapter(api_key=api_key))
    if base_url := os.getenv("HARNESSLAB_BASE_URL"):
        runtime.register_adapter(
            OpenAICompatibleAdapter(
                base_url=base_url,
                api_key=os.getenv("HARNESSLAB_API_KEY", ""),
                model=os.getenv("HARNESSLAB_MODEL", "default"),
            )
        )
    return runtime


@asynccontextmanager
async def lifespan(app: FastAPI):
    runtime: HarnessRuntime = app.state.runtime
    seed = await runtime.run(
        "Review the checkout authorization change and report the highest-risk regression.",
        adapter_name="demo",
    )
    app.state.conformance = await run_conformance(runtime, "demo")
    app.state.featured_run_id = seed.id
    yield


def create_app(runtime: HarnessRuntime | None = None, *, seed_demo: bool = True) -> FastAPI:
    chosen_lifespan = lifespan if seed_demo else None
    app = FastAPI(
        title="HarnessLab",
        version=__version__,
        description="Protocol-first conformance and trace console for agent harnesses.",
        lifespan=chosen_lifespan,
    )
    app.state.runtime = runtime or build_runtime()
    app.state.conformance = None
    app.state.background_runs = set()

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/api/meta")
    async def meta() -> dict[str, object]:
        active_runtime: HarnessRuntime = app.state.runtime
        return {
            "name": "HarnessLab",
            "version": __version__,
            "protocol_status": "adapter-ready",
            "adapters": list(active_runtime.adapters),
            "tools": [spec.model_dump() for spec in active_runtime.tools.specs()],
            "featured_run_id": getattr(app.state, "featured_run_id", None),
        }

    @app.get("/api/runs", response_model=list[RunRecord])
    async def list_runs() -> list[RunRecord]:
        return app.state.runtime.list_runs()

    @app.get("/api/runs/{run_id}", response_model=RunRecord)
    async def get_run(run_id: str) -> RunRecord:
        try:
            return app.state.runtime.get_run(run_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="Run not found") from exc

    @app.post("/api/runs", response_model=RunRecord, status_code=202)
    async def create_run(request: RunRequest) -> RunRecord:
        active_runtime: HarnessRuntime = app.state.runtime
        if request.adapter not in active_runtime.adapters:
            raise HTTPException(status_code=400, detail=f"Unknown adapter: {request.adapter}")
        run_id = f"run_{os.urandom(6).hex()}"
        task = asyncio.create_task(
            active_runtime.run(
                request.task,
                adapter_name=request.adapter,
                max_turns=request.max_turns,
                run_id=run_id,
            )
        )
        app.state.background_runs.add(task)
        task.add_done_callback(app.state.background_runs.discard)
        await asyncio.sleep(0)
        return active_runtime.get_run(run_id)

    @app.get("/api/runs/{run_id}/events")
    async def run_events(
        run_id: str,
        after: int = Query(default=0, ge=0),
    ) -> StreamingResponse:
        try:
            active_runtime: HarnessRuntime = app.state.runtime
            active_runtime.get_run(run_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="Run not found") from exc

        async def stream() -> AsyncIterator[str]:
            async for event in active_runtime.events.subscribe(run_id, after=after):
                payload = event.model_dump_json()
                yield f"id: {event.sequence}\nevent: {event.type}\ndata: {payload}\n\n"
                if event.type in TERMINAL_EVENTS:
                    return

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/conformance", response_model=ConformanceReport)
    async def conformance(adapter: str = "demo") -> ConformanceReport:
        active_runtime: HarnessRuntime = app.state.runtime
        if adapter not in active_runtime.adapters:
            raise HTTPException(status_code=400, detail=f"Unknown adapter: {adapter}")
        report = await run_conformance(active_runtime, adapter)
        app.state.conformance = report
        return report

    @app.get("/api/conformance", response_model=ConformanceReport | None)
    async def get_conformance() -> ConformanceReport | None:
        return app.state.conformance

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app()
