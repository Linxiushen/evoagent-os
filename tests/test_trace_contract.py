from __future__ import annotations

import pytest

from harnesslab.adapters import DemoAdapter, RegressionFixtureAdapter
from harnesslab.models import AdapterTurn, ToolCall, ToolSpec, TraceEvent
from harnesslab.runtime import HarnessRuntime
from harnesslab.tools import ToolRegistry, build_demo_registry
from harnesslab.trace_contract import (
    build_trace_artifact,
    compare_trace_artifacts,
    project_trace,
    redact_data,
)


@pytest.mark.asyncio
async def test_deterministic_runs_share_protocol_and_content_fingerprints() -> None:
    runtime = HarnessRuntime(build_demo_registry())
    runtime.register_adapter(DemoAdapter())

    first = build_trace_artifact(await runtime.run("Review the checkout authorization change."))
    second = build_trace_artifact(await runtime.run("Review the checkout authorization change."))

    comparison = compare_trace_artifacts(first, second)
    assert first.protocol_fingerprint == second.protocol_fingerprint
    assert first.content_fingerprint == second.content_fingerprint
    assert comparison.compatible is True
    assert comparison.protocol_score == 100
    assert comparison.content_match is True
    assert comparison.differences == []


@pytest.mark.asyncio
async def test_trace_comparison_detects_tool_path_regression() -> None:
    runtime = HarnessRuntime(build_demo_registry())
    runtime.register_adapter(DemoAdapter())
    runtime.register_adapter(RegressionFixtureAdapter())
    baseline_run = await runtime.run("Review the checkout authorization change.")
    candidate_run = await runtime.run(
        "Review the checkout authorization change.",
        adapter_name="regression-fixture",
    )

    comparison = compare_trace_artifacts(
        build_trace_artifact(baseline_run),
        build_trace_artifact(candidate_run),
    )

    assert comparison.compatible is False
    assert {item.area for item in comparison.differences} >= {"tool-path", "content"}


@pytest.mark.asyncio
async def test_trace_comparison_detects_terminal_status_regression() -> None:
    runtime = HarnessRuntime(build_demo_registry())
    runtime.register_adapter(DemoAdapter())
    baseline_run = await runtime.run("Review the checkout authorization change.")
    candidate_run = baseline_run.model_copy(deep=True)
    candidate_run.id = "run_terminal_regression"
    candidate_run.status = "failed"

    comparison = compare_trace_artifacts(
        build_trace_artifact(baseline_run),
        build_trace_artifact(candidate_run),
    )

    assert comparison.compatible is False
    assert any(item.area == "terminal-state" for item in comparison.differences)


def test_trace_contract_rejects_approved_call_without_completion() -> None:
    events = [
        TraceEvent(run_id="run_invalid", sequence=1, type="run.started"),
        TraceEvent(
            run_id="run_invalid",
            sequence=2,
            type="tool.requested",
            payload={"call_id": "call_1", "name": "inspect_change"},
        ),
        TraceEvent(
            run_id="run_invalid",
            sequence=3,
            type="tool.approved",
            payload={"call_id": "call_1", "name": "inspect_change"},
        ),
        TraceEvent(run_id="run_invalid", sequence=4, type="run.completed"),
    ]

    projection = project_trace(events, status="completed")

    assert "missing-completion:call_1" in projection.violations


def test_redaction_covers_nested_keys_and_inline_credentials() -> None:
    value = {
        "authorization": "Bearer live-token-value",
        "nested": {
            "apiKey": "key-abcdefghijklmnop",
            "note": "call with Bearer abc.def.ghi",
        },
    }

    assert redact_data(value) == {
        "authorization": "[REDACTED]",
        "nested": {
            "apiKey": "[REDACTED]",
            "note": "call with Bearer [REDACTED]",
        },
    }


@pytest.mark.asyncio
async def test_runtime_redacts_events_before_publication() -> None:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="credential_echo",
            description="Return a credential-shaped fixture",
            input_schema={"type": "object", "properties": {"token": {"type": "string"}}},
        ),
        lambda _args: {"api_key": "key-abcdefghijklmnop"},
    )

    class CredentialAdapter:
        name = "credential-fixture"

        async def complete(self, messages, tools):
            if not any(message.role == "tool" for message in messages):
                return AdapterTurn(
                    tool_calls=[
                        ToolCall(
                            id="credential_1",
                            name="credential_echo",
                            arguments={"token": "token-abcdefghijklmnop"},
                        )
                    ],
                    finish_reason="tool_calls",
                )
            return AdapterTurn(text="done")

    runtime = HarnessRuntime(registry)
    runtime.register_adapter(CredentialAdapter())
    run = await runtime.run("Exercise credential redaction", adapter_name="credential-fixture")
    events = runtime.events.history(run.id)
    requested = next(event for event in events if event.type == "tool.requested")
    completed = next(event for event in events if event.type == "tool.completed")

    assert requested.payload["arguments"]["token"] == "[REDACTED]"
    assert completed.payload["result"]["api_key"] == "[REDACTED]"
    assert "messages" not in run.model_dump(mode="json")
