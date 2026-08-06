from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from difflib import SequenceMatcher
from typing import Any, Literal

from pydantic import BaseModel, Field

from harnesslab.models import RunRecord, TraceEvent, utc_now

TRACE_CONTRACT_VERSION = "harnesslab.trace/v1"

_SENSITIVE_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "token",
}
_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]+")
_KEY_PATTERN = re.compile(r"\b(?:sk|key|token)-[a-zA-Z0-9_-]{12,}\b")


def redact_data(value: Any, *, key: str | None = None) -> Any:
    """Return a JSON-compatible copy with common credential material removed."""
    if key and _is_sensitive_key(key):
        return "[REDACTED]"
    if isinstance(value, BaseModel):
        return redact_data(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {
            str(item_key): redact_data(item, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [redact_data(item) for item in value]
    if isinstance(value, str):
        return _KEY_PATTERN.sub("[REDACTED]", _BEARER_PATTERN.sub("Bearer [REDACTED]", value))
    return value


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return normalized in _SENSITIVE_KEYS or normalized.endswith(("_token", "_secret", "_password"))


class TraceProjection(BaseModel):
    event_types: list[str]
    model_path: list[str]
    tool_path: list[str]
    policy_path: list[str]
    terminal_event: str | None = None
    terminal_status: str
    violations: list[str] = Field(default_factory=list)


class TraceArtifact(BaseModel):
    contract_version: Literal["harnesslab.trace/v1"] = TRACE_CONTRACT_VERSION
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())
    source_run_id: str
    task: str
    adapter: str
    status: str
    protocol_fingerprint: str
    content_fingerprint: str
    projection: TraceProjection
    events: list[TraceEvent]


class TraceDifference(BaseModel):
    area: str
    severity: Literal["breaking", "notice"]
    expected: Any
    actual: Any
    detail: str


class TraceComparison(BaseModel):
    contract_version: str = TRACE_CONTRACT_VERSION
    compatible: bool
    protocol_score: int = Field(ge=0, le=100)
    baseline_run_id: str
    candidate_run_id: str
    baseline_fingerprint: str
    candidate_fingerprint: str
    content_match: bool
    differences: list[TraceDifference] = Field(default_factory=list)


def build_trace_artifact(run: RunRecord) -> TraceArtifact:
    events = [_redacted_event(event) for event in run.events]
    projection = project_trace(events, status=str(run.status))
    protocol_payload = projection.model_dump(mode="json")
    content_payload = [
        {
            "type": event.type,
            "payload": _stable_payload(event.payload),
        }
        for event in events
    ]
    return TraceArtifact(
        source_run_id=run.id,
        task=redact_data(run.task),
        adapter=run.adapter,
        status=str(run.status),
        protocol_fingerprint=_fingerprint(protocol_payload),
        content_fingerprint=_fingerprint(content_payload),
        projection=projection,
        events=events,
    )


def project_trace(events: Iterable[TraceEvent], *, status: str) -> TraceProjection:
    ordered = list(events)
    event_types = [event.type for event in ordered]
    model_path = [
        f"{event.type}:{event.payload.get('finish_reason', event.payload.get('tool_count', '-'))}"
        for event in ordered
        if event.type.startswith("model.")
    ]
    tool_path = [
        f"{event.type}:{event.payload.get('name', 'unknown')}"
        for event in ordered
        if event.type in {"tool.requested", "tool.completed"}
    ]
    policy_path = [
        ":".join(
            (
                event.type,
                str(event.payload.get("name", "unknown")),
                str(event.payload.get("policy", event.payload.get("reason", "-"))),
            )
        )
        for event in ordered
        if event.type in {"tool.approved", "tool.denied"}
    ]
    terminal = [
        event.type
        for event in ordered
        if event.type in {"run.completed", "run.failed", "run.cancelled"}
    ]
    return TraceProjection(
        event_types=event_types,
        model_path=model_path,
        tool_path=tool_path,
        policy_path=policy_path,
        terminal_event=terminal[-1] if terminal else None,
        terminal_status=status,
        violations=_trace_violations(ordered, terminal),
    )


def compare_trace_artifacts(
    baseline: TraceArtifact,
    candidate: TraceArtifact,
) -> TraceComparison:
    differences: list[TraceDifference] = []
    if baseline.projection.violations:
        differences.append(
            TraceDifference(
                area="baseline-invariants",
                severity="breaking",
                expected=[],
                actual=baseline.projection.violations,
                detail="Baseline violates Trace Contract invariants",
            )
        )
    _compare_path(
        differences,
        "event-sequence",
        baseline.projection.event_types,
        candidate.projection.event_types,
        "Lifecycle event order changed",
    )
    _compare_path(
        differences,
        "tool-path",
        baseline.projection.tool_path,
        candidate.projection.tool_path,
        "Tool request/completion path changed",
    )
    _compare_path(
        differences,
        "policy-path",
        baseline.projection.policy_path,
        candidate.projection.policy_path,
        "Approval path changed",
    )
    baseline_terminal = {
        "event": baseline.projection.terminal_event,
        "status": baseline.projection.terminal_status,
    }
    candidate_terminal = {
        "event": candidate.projection.terminal_event,
        "status": candidate.projection.terminal_status,
    }
    if baseline_terminal != candidate_terminal:
        differences.append(
            TraceDifference(
                area="terminal-state",
                severity="breaking",
                expected=baseline_terminal,
                actual=candidate_terminal,
                detail="Run terminal event or status changed",
            )
        )
    if candidate.projection.violations:
        differences.append(
            TraceDifference(
                area="invariants",
                severity="breaking",
                expected=[],
                actual=candidate.projection.violations,
                detail="Candidate violates Trace Contract invariants",
            )
        )
    content_match = baseline.content_fingerprint == candidate.content_fingerprint
    if not content_match:
        differences.append(
            TraceDifference(
                area="content",
                severity="notice",
                expected=baseline.content_fingerprint,
                actual=candidate.content_fingerprint,
                detail="Redacted event payloads changed",
            )
        )
    score = round(
        100
        * SequenceMatcher(
            a=baseline.projection.event_types,
            b=candidate.projection.event_types,
            autojunk=False,
        ).ratio()
    )
    return TraceComparison(
        compatible=not any(item.severity == "breaking" for item in differences),
        protocol_score=score,
        baseline_run_id=baseline.source_run_id,
        candidate_run_id=candidate.source_run_id,
        baseline_fingerprint=baseline.protocol_fingerprint,
        candidate_fingerprint=candidate.protocol_fingerprint,
        content_match=content_match,
        differences=differences,
    )


def _compare_path(
    differences: list[TraceDifference],
    area: str,
    expected: list[str],
    actual: list[str],
    detail: str,
) -> None:
    if expected != actual:
        differences.append(
            TraceDifference(
                area=area,
                severity="breaking",
                expected=expected,
                actual=actual,
                detail=detail,
            )
        )


def _trace_violations(events: list[TraceEvent], terminal: list[str]) -> list[str]:
    violations: list[str] = []
    if [event.sequence for event in events] != list(range(1, len(events) + 1)):
        violations.append("non-monotonic-sequence")
    if len(terminal) != 1:
        violations.append("terminal-event-count")
    elif not events or events[-1].type != terminal[0]:
        violations.append("terminal-event-not-last")

    requested: dict[str, int] = {}
    policy: dict[str, int] = {}
    policy_kind: dict[str, str] = {}
    resolved: dict[str, int] = {}
    for index, event in enumerate(events):
        call_id = event.payload.get("call_id")
        if not call_id:
            continue
        if event.type == "tool.requested":
            normalized_id = str(call_id)
            if normalized_id in requested:
                violations.append(f"duplicate-request:{normalized_id}")
            requested[normalized_id] = index
        elif event.type in {"tool.approved", "tool.denied"}:
            normalized_id = str(call_id)
            if normalized_id in policy:
                violations.append(f"duplicate-policy:{normalized_id}")
            policy[normalized_id] = index
            policy_kind[normalized_id] = event.type
        elif event.type == "tool.completed":
            normalized_id = str(call_id)
            if normalized_id in resolved:
                violations.append(f"duplicate-completion:{normalized_id}")
            resolved[normalized_id] = index
    for call_id, request_index in requested.items():
        policy_index = policy.get(call_id)
        completion_index = resolved.get(call_id)
        if policy_index is None:
            violations.append(f"missing-policy:{call_id}")
        elif policy_index <= request_index:
            violations.append(f"policy-before-request:{call_id}")
        if policy_kind.get(call_id) == "tool.approved" and completion_index is None:
            violations.append(f"missing-completion:{call_id}")
        if policy_kind.get(call_id) == "tool.denied" and completion_index is not None:
            violations.append(f"denied-tool-completed:{call_id}")
        if (
            completion_index is not None
            and policy_index is not None
            and completion_index <= policy_index
        ):
            violations.append(f"completion-before-policy:{call_id}")
    for call_id in policy.keys() - requested.keys():
        violations.append(f"policy-without-request:{call_id}")
    for call_id in resolved.keys() - requested.keys():
        violations.append(f"completion-without-request:{call_id}")
    return violations


def _stable_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    volatile = {"usage", "text", "answer"}
    return {
        key: value
        for key, value in redact_data(payload).items()
        if key not in volatile
    }


def _redacted_event(event: TraceEvent) -> TraceEvent:
    return event.model_copy(update={"payload": redact_data(event.payload)}, deep=True)


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"
