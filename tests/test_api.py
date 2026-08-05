from __future__ import annotations

import httpx
import pytest

from harnesslab.api import create_app


@pytest.mark.asyncio
async def test_health_and_meta_are_available_without_model_credentials() -> None:
    app = create_app(seed_demo=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        health = await client.get("/healthz")
        meta = await client.get("/api/meta")

    assert health.json()["status"] == "ok"
    assert "demo" in meta.json()["adapters"]
    assert {tool["name"] for tool in meta.json()["tools"]} == {
        "search_repository",
        "inspect_change",
    }


@pytest.mark.asyncio
async def test_unknown_adapter_is_rejected() -> None:
    app = create_app(seed_demo=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/runs",
            json={"task": "Review this change", "adapter": "missing"},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "Unknown adapter: missing"

