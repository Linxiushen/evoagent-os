from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:20]}"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RunStatus(StrEnum):
    ACCEPTED = "accepted"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"


class Envelope(BaseModel):
    channel: str = "api"
    peer_id: str = "local"
    session_id: str | None = None
    text: str = Field(min_length=1, max_length=100_000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolCall(BaseModel):
    id: str = Field(default_factory=lambda: new_id("call"))
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ModelTurn(BaseModel):
    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = "offline"


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    name: str | None = None
    tool_call_id: str | None = None


class RunView(BaseModel):
    run_id: str
    session_id: str
    status: RunStatus
    input_text: str
    output_text: str | None = None
    error: str | None = None
    prompt_version: int
    created_at: str
    updated_at: str


class ApprovalDecision(BaseModel):
    approved: bool
    actor: str = "operator"


class Feedback(BaseModel):
    rating: int = Field(ge=-1, le=1)
    comment: str = Field(default="", max_length=10_000)


class EvolutionScenario(BaseModel):
    input: str
    required: list[str] = Field(default_factory=list)
    forbidden: list[str] = Field(default_factory=list)


class CandidateView(BaseModel):
    candidate_id: str
    parent_version: int
    prompt: str
    status: str
    baseline_score: float | None = None
    candidate_score: float | None = None
    safety: float | None = None
    created_at: str
