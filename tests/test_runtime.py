from __future__ import annotations

import pytest

from harnesslab.adapters import DemoAdapter
from harnesslab.models import RunStatus, ToolSpec
from harnesslab.runtime import HarnessRuntime
from harnesslab.tools import build_demo_registry


@pytest.mark.asyncio
async def test_demo_run_preserves_order_and_correlates_tools() -> None:
    runtime = HarnessRuntime(build_demo_registry())
    runtime.register_adapter(DemoAdapter())

    run = await runtime.run("Review the checkout authorization change.")
    events = runtime.events.history(run.id)

    assert run.status == RunStatus.COMPLETED
    assert "src/checkout/policy.py:42" in run.answer
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    requested_ids = [event.payload["call_id"] for event in events if event.type == "tool.requested"]
    completed_ids = [event.payload["call_id"] for event in events if event.type == "tool.completed"]
    assert requested_ids == completed_ids == ["call_search_1", "call_inspect_1"]


@pytest.mark.asyncio
async def test_unknown_adapter_fails_before_creating_run() -> None:
    runtime = HarnessRuntime(build_demo_registry())

    with pytest.raises(ValueError, match="Unknown adapter"):
        await runtime.run("Do useful work", adapter_name="missing")

    assert runtime.runs == {}


@pytest.mark.asyncio
async def test_write_tool_fails_closed() -> None:
    registry = build_demo_registry()
    registry.register(
        ToolSpec(
            name="delete_file",
            description="Delete a file",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            read_only=False,
        ),
        lambda _args: "deleted",
    )

    class WriteAdapter:
        name = "write-test"

        async def complete(self, messages, tools):
            from harnesslab.models import AdapterTurn, ToolCall

            return AdapterTurn(
                tool_calls=[ToolCall(id="write_1", name="delete_file", arguments={})],
                finish_reason="tool_calls",
            )

    runtime = HarnessRuntime(registry)
    runtime.register_adapter(WriteAdapter())
    run = await runtime.run("Delete the fixture", adapter_name="write-test")

    assert run.status == RunStatus.FAILED
    assert "explicit approval provider" in (run.error or "")
    assert not any(event.type == "tool.completed" for event in runtime.events.history(run.id))
