from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class NodeStatus(StrEnum):
    BLOCKED = "blocked"
    QUEUED = "queued"
    AWAITING_APPROVAL = "awaiting_approval"
    LEASED = "leased"
    COMPLETED = "completed"
    FAILED = "failed"


class Budget(BaseModel):
    tokens: int = Field(default=50_000, ge=0)
    cost_usd: float = Field(default=5.0, ge=0)
    seconds: int = Field(default=600, ge=1)


class NodeSpec(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    objective: str = Field(min_length=1, max_length=20_000)
    depends_on: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    input: dict[str, Any] = Field(default_factory=dict)
    budget: Budget = Field(default_factory=Budget)
    approval_required: bool = False
    max_attempts: int = Field(default=3, ge=1, le=10)


class WorkflowSpec(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    nodes: list[NodeSpec] = Field(min_length=1, max_length=500)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_dag(self) -> WorkflowSpec:
        ids = [node.id for node in self.nodes]
        if len(ids) != len(set(ids)):
            raise ValueError("Node IDs must be unique")
        known = set(ids)
        for node in self.nodes:
            missing = set(node.depends_on) - known
            if missing:
                raise ValueError(f"Node {node.id} has unknown dependencies: {sorted(missing)}")
            if node.id in node.depends_on:
                raise ValueError(f"Node {node.id} cannot depend on itself")
        visiting: set[str] = set()
        visited: set[str] = set()
        edges = {node.id: node.depends_on for node in self.nodes}

        def visit(node_id: str) -> None:
            if node_id in visiting:
                raise ValueError("Workflow contains a dependency cycle")
            if node_id in visited:
                return
            visiting.add(node_id)
            for parent in edges[node_id]:
                visit(parent)
            visiting.remove(node_id)
            visited.add(node_id)

        for node_id in ids:
            visit(node_id)
        return self


class WorkerRegistration(BaseModel):
    worker_id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,80}$")
    capabilities: list[str] = Field(default_factory=list)
    pool: str = "default"
    max_concurrency: int = Field(default=1, ge=1, le=100)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Completion(BaseModel):
    output: dict[str, Any] = Field(default_factory=dict)
    artifacts: dict[str, str] = Field(default_factory=dict)
    tokens_used: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0, ge=0)
    duration_seconds: float = Field(default=0, ge=0)
    quality: float = Field(default=1, ge=0, le=1)


class Failure(BaseModel):
    error: str = Field(min_length=1, max_length=20_000)
    retryable: bool = True


class Approval(BaseModel):
    approved: bool
    actor: str = Field(default="operator", min_length=1, max_length=120)
