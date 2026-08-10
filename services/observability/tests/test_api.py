from __future__ import annotations

import httpx
import pytest

from harnesslab.adapters import DemoAdapter
from harnesslab.api import create_app
from harnesslab.runtime import HarnessRuntime
from harnesslab.tools import build_demo_registry


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


@pytest.mark.asyncio
async def test_runs_can_be_exported_and_compared_as_trace_contracts() -> None:
    runtime = HarnessRuntime(build_demo_registry())
    runtime.register_adapter(DemoAdapter())
    first = await runtime.run("Review this change")
    second = await runtime.run("Review this change")
    app = create_app(runtime=runtime, seed_demo=False)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        run_response = await client.get(f"/api/runs/{first.id}")
        artifact = await client.get(f"/api/runs/{first.id}/artifact")
        comparison = await client.post(
            "/api/compare",
            json={"baseline_run_id": first.id, "candidate_run_id": second.id},
        )

    assert artifact.status_code == 200
    assert "messages" not in run_response.json()
    assert artifact.json()["contract_version"] == "harnesslab.trace/v1"
    assert comparison.status_code == 200
    assert comparison.json()["compatible"] is True
    assert comparison.json()["protocol_score"] == 100
