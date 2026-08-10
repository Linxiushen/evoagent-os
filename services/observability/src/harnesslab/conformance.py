from __future__ import annotations

from pydantic import BaseModel

from harnesslab.models import RunStatus
from harnesslab.runtime import HarnessRuntime
from harnesslab.trace_contract import TraceArtifact, build_trace_artifact


class ConformanceCheck(BaseModel):
    id: str
    title: str
    status: str
    evidence: str


class ConformanceReport(BaseModel):
    adapter: str
    passed: int
    total: int
    checks: list[ConformanceCheck]
    run_id: str


async def run_conformance(
    runtime: HarnessRuntime,
    adapter_name: str = "demo",
) -> ConformanceReport:
    run = await runtime.run(
        "Review the checkout authorization change and report the highest-risk regression.",
        adapter_name=adapter_name,
    )
    events = runtime.events.history(run.id)
    requested = [event for event in events if event.type == "tool.requested"]
    completed = [event for event in events if event.type == "tool.completed"]
    schemas = runtime.tools.specs()
    artifact = build_trace_artifact(run)
    event_positions = {
        (event.type, event.payload.get("call_id")): event.sequence
        for event in events
        if event.payload.get("call_id")
    }
    policy_ordered = all(
        event_positions.get(("tool.requested", event.payload["call_id"]), 0)
        < event.sequence
        < event_positions.get(("tool.completed", event.payload["call_id"]), 10**9)
        for event in events
        if event.type == "tool.approved"
    )
    checks = [
        _check(
            "schema-fidelity",
            "Tool schema fidelity",
            bool(schemas)
            and all(spec.input_schema.get("type") == "object" for spec in schemas)
            and len({spec.name for spec in schemas}) == len(schemas),
            f"{len(schemas)} unique JSON Schema tool contracts discovered",
        ),
        _check(
            "ordered-events",
            "Monotonic event stream",
            [event.sequence for event in events] == list(range(1, len(events) + 1)),
            f"{len(events)} events retained in strict per-run order",
        ),
        _check(
            "tool-roundtrip",
            "Tool call round trip",
            len(requested) > 0
            and [event.payload["call_id"] for event in requested]
            == [event.payload["call_id"] for event in completed],
            f"{len(completed)} requested calls returned correlated results",
        ),
        _check(
            "approval-boundary",
            "Read-only approval boundary",
            len([event for event in events if event.type == "tool.approved"]) == len(requested),
            "Every tool crossed an explicit policy event before execution",
        ),
        _check(
            "terminal-state",
            "Single terminal state",
            run.status == RunStatus.COMPLETED
            and len(
                [
                    event
                    for event in events
                    if event.type in {"run.completed", "run.failed", "run.cancelled"}
                ]
            )
            == 1
            and events[-1].type == "run.completed",
            f"Run ended as {run.status} with one final event",
        ),
        _check(
            "evidence-answer",
            "Evidence-bearing answer",
            bool(run.answer) and "src/checkout/policy.py" in run.answer,
            "Final answer preserves file-level evidence from the tool result",
        ),
        _check(
            "policy-order",
            "Policy precedes execution",
            policy_ordered,
            "Every approval follows its request and precedes tool completion",
        ),
        _check(
            "trace-contract",
            "Trace Contract invariants",
            not artifact.projection.violations,
            f"{artifact.contract_version} projection has no violations",
        ),
        _check(
            "artifact-roundtrip",
            "Artifact round trip",
            TraceArtifact.model_validate_json(artifact.model_dump_json()).protocol_fingerprint
            == artifact.protocol_fingerprint,
            "Portable artifact validates with an unchanged protocol fingerprint",
        ),
        _check(
            "context-isolation",
            "Raw context isolation",
            "messages" not in run.model_dump(mode="json"),
            "Serialized run records exclude raw model and tool context",
        ),
    ]
    passed = sum(check.status == "passed" for check in checks)
    return ConformanceReport(
        adapter=adapter_name,
        passed=passed,
        total=len(checks),
        checks=checks,
        run_id=run.id,
    )


def _check(check_id: str, title: str, passed: bool, evidence: str) -> ConformanceCheck:
    return ConformanceCheck(
        id=check_id,
        title=title,
        status="passed" if passed else "failed",
        evidence=evidence,
    )
