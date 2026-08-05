from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Message(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


class ToolSpec(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]
    read_only: bool = True
    source: str = "local"

    def as_openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any]


class AdapterTurn(BaseModel):
    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, int] = Field(default_factory=dict)
    raw: dict[str, Any] | None = None


class TraceEvent(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    run_id: str
    sequence: int
    type: str
    at: datetime = Field(default_factory=utc_now)
    duration_ms: float | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class RunRecord(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    id: str
    task: str
    adapter: str
    status: RunStatus = RunStatus.QUEUED
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    answer: str = ""
    error: str | None = None
    messages: list[Message] = Field(default_factory=list)
    events: list[TraceEvent] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunRequest(BaseModel):
    task: str = Field(min_length=3, max_length=4000)
    adapter: str = "demo"
    max_turns: int = Field(default=6, ge=1, le=20)


class CapabilityDocument(BaseModel):
    protocol: str
    protocol_version: str
    features: list[str] = Field(default_factory=list)
    transport: list[str] = Field(default_factory=list)
    auth: list[str] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)
