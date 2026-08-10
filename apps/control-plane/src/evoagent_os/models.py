from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    description: str = Field(default="", max_length=2_000)
    tenant_id: str = Field(default="tenant_local", pattern=r"^[A-Za-z0-9_.-]{2,80}$")


class AgentCreate(BaseModel):
    workspace_id: str = "ws_default"
    name: str = Field(min_length=2, max_length=120)
    role: str = Field(min_length=2, max_length=120)
    description: str = Field(default="", max_length=2_000)
    model: str = "deterministic/offline"
    capabilities: list[str] = Field(default_factory=list, max_length=100)
    tools: list[str] = Field(default_factory=list, max_length=100)
    skills: list[str] = Field(default_factory=list, max_length=100)
    system_prompt: str = Field(default="", max_length=20_000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunCreate(BaseModel):
    agent_id: str = "agent_coordinator"
    workspace_id: str = "ws_default"
    input: str = Field(min_length=1, max_length=100_000)
    session_id: str | None = Field(default=None, max_length=120)
    wait: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkflowNodeCreate(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    objective: str = Field(min_length=1, max_length=20_000)
    depends_on: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    input: dict[str, Any] = Field(default_factory=dict)
    tokens: int = Field(default=20_000, ge=0)
    cost_usd: float = Field(default=1.0, ge=0)
    seconds: int = Field(default=180, ge=1)
    approval_required: bool = False
    max_attempts: int = Field(default=3, ge=1, le=10)


class WorkflowCreate(BaseModel):
    workspace_id: str = "ws_default"
    name: str = Field(min_length=2, max_length=120)
    description: str = Field(default="", max_length=2_000)
    nodes: list[WorkflowNodeCreate] = Field(min_length=1, max_length=500)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ApprovalDecision(BaseModel):
    approved: bool
    actor: str = Field(default="operator", min_length=1, max_length=120)
    reason: str = Field(default="", max_length=2_000)


class FeedbackCreate(BaseModel):
    rating: int = Field(ge=-1, le=1)
    comment: str = Field(default="", max_length=10_000)


class WorkerCreate(BaseModel):
    worker_id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,80}$")
    capabilities: list[str] = Field(default_factory=list)
    pool: str = Field(default="default", min_length=1, max_length=80)
    max_concurrency: int = Field(default=1, ge=1, le=100)
    metadata: dict[str, Any] = Field(default_factory=dict)


class LeaseCompletion(BaseModel):
    worker_id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,80}$")
    output: dict[str, Any] = Field(default_factory=dict)
    artifacts: dict[str, str] = Field(default_factory=dict)
    tokens_used: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0, ge=0)
    duration_seconds: float = Field(default=0, ge=0)
    quality: float = Field(default=1, ge=0, le=1)


class LeaseFailure(BaseModel):
    worker_id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,80}$")
    error: str = Field(min_length=1, max_length=20_000)
    retryable: bool = True


class LeaseHeartbeat(BaseModel):
    worker_id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,80}$")


class JobCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    interval_seconds: int = Field(ge=5, le=31_536_000)
    message: str = Field(min_length=1, max_length=100_000)
