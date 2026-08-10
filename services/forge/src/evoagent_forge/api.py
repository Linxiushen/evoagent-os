from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .registry import Registry


def create_app(registry_root: Path | str = ".forge-registry") -> FastAPI:
    registry = Registry(registry_root)
    app = FastAPI(title="EvoAgent Forge Registry", version="0.1.0")
    app.state.registry = registry

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/releases")
    async def releases(q: str = "", limit: int = 50) -> list[dict[str, Any]]:
        return registry.search(q, min(max(limit, 1), 100))

    @app.get("/v1/releases/{name}/{version}")
    async def release(name: str, version: str) -> dict[str, Any]:
        value = registry.get(name, version)
        if value is None:
            raise HTTPException(status_code=404, detail="Release not found")
        return value

    static = Path(__file__).with_name("static")
    app.mount("/assets", StaticFiles(directory=static), name="assets")

    @app.get("/", include_in_schema=False)
    async def dashboard() -> FileResponse:
        return FileResponse(static / "index.html")

    return app
