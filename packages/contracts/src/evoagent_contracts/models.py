from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

JsonObject = dict[str, Any]


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class WriteContract(BaseModel):
    """Base for client-authored payloads.

    Unknown input is rejected because silently dropping a control-plane field
    can change execution or governance behavior.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True, str_strip_whitespace=True)


class ReadContract(BaseModel):
    """Base for server-authored resources with additive-change tolerance."""

    model_config = ConfigDict(extra="allow", populate_by_name=True, str_strip_whitespace=True)


class WorkspaceStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class AgentStatus(StrEnum):
    ACTIVE = "active"
    READY = "ready"
    PAUSED = "paused"
    ARCHIVED = "archived"


class RunStatus(StrEnum):
    ACCEPTED = "accepted"
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkflowStatus(StrEnum):
    DRAFT = "draft"
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    DENIED = "denied"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class WorkspaceCreate(WriteContract):
    name: str = Field(min_length=1, max_length=120)
    slug: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    description: str = Field(default="", max_length=4_000)
    tenant_id: str = Field(default="tenant_local", pattern=r"^[A-Za-z0-9_.-]{2,80}$")
    metadata: JsonObject = Field(default_factory=dict)


class Workspace(ReadContract):
    id: str = Field(
        min_length=1,
        max_length=128,
        validation_alias=AliasChoices("id", "workspace_id"),
    )
    tenant_id: str = Field(default="default", min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=120)
    slug: str = ""
    description: str = ""
    status: WorkspaceStatus = WorkspaceStatus.ACTIVE
    metadata: JsonObject = Field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class AgentManifest(WriteContract):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=4_000)
    role: str = Field(default="General purpose agent", min_length=1, max_length=500)
    model: str = Field(default="offline", min_length=1, max_length=200)
    system_prompt: str = Field(
        default="You are a reliable agent.", min_length=1, max_length=100_000
    )
    temperature: float = Field(default=0.2, ge=0, le=2)
    tools: list[str] = Field(default_factory=list, max_length=500)
    skills: list[str] = Field(default_factory=list, max_length=500)
    capabilities: list[str] = Field(default_factory=list, max_length=500)
    memory: JsonObject = Field(default_factory=dict)
    metadata: JsonObject = Field(default_factory=dict)
    version: str = Field(
        default="1.0.0", pattern=r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$"
    )


class AgentCreate(AgentManifest):
    workspace_id: str = Field(min_length=1, max_length=128)


class AgentSummary(ReadContract):
    id: str = Field(
        min_length=1,
        max_length=128,
        validation_alias=AliasChoices("id", "agent_id"),
    )
    workspace_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    role: str = "General purpose agent"
    model: str = "offline"
    system_prompt: str = ""
    temperature: float = 0.2
    tools: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    memory: JsonObject = Field(default_factory=dict)
    metadata: JsonObject = Field(default_factory=dict)
    version: str = "1.0.0"
    status: AgentStatus = AgentStatus.ACTIVE
    created_at: datetime | None = None
    updated_at: datetime | None = None


class Budget(WriteContract):
    tokens: int = Field(default=50_000, ge=0)
    cost_usd: float = Field(default=5.0, ge=0)
    seconds: int = Field(default=600, ge=1)


class WorkflowNode(WriteContract):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    objective: str = Field(min_length=1, max_length=20_000)
    agent_id: str | None = Field(default=None, max_length=128)
    depends_on: list[str] = Field(default_factory=list, max_length=500)
    capabilities: list[str] = Field(default_factory=list, max_length=500)
    input: JsonObject = Field(default_factory=dict)
    tokens: int = Field(default=50_000, ge=0)
    cost_usd: float = Field(default=5.0, ge=0)
    seconds: int = Field(default=600, ge=1)
    approval_required: bool = False
    max_attempts: int = Field(default=3, ge=1, le=10)

    @model_validator(mode="before")
    @classmethod
    def unpack_budget(cls, value: Any) -> Any:
        """Accept the fleet service's nested budget form at the boundary."""

        if not isinstance(value, dict) or "budget" not in value:
            return value
        unpacked = dict(value)
        budget = Budget.model_validate(unpacked.pop("budget"))
        unpacked.setdefault("tokens", budget.tokens)
        unpacked.setdefault("cost_usd", budget.cost_usd)
        unpacked.setdefault("seconds", budget.seconds)
        return unpacked


class RunCreate(WriteContract):
    workspace_id: str = Field(min_length=1, max_length=128)
    agent_id: str = Field(min_length=1, max_length=128)
    input: str | JsonObject
    workflow_id: str | None = Field(default=None, max_length=128)
    session_id: str | None = Field(default=None, max_length=128)
    correlation_id: str | None = Field(default=None, max_length=256)
    idempotency_key: str | None = Field(default=None, max_length=256)
    wait: bool = True
    budget: Budget | None = None
    metadata: JsonObject = Field(default_factory=dict)


class TokenUsage(ReadContract):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0, ge=0)


class RunSummary(ReadContract):
    id: str = Field(
        min_length=1,
        max_length=128,
        validation_alias=AliasChoices("id", "run_id"),
    )
    workspace_id: str | None = Field(default=None, max_length=128)
    agent_id: str | None = Field(default=None, max_length=128)
    status: RunStatus = RunStatus.QUEUED
    input: str | JsonObject | None = Field(
        default=None,
        validation_alias=AliasChoices("input", "input_text"),
    )
    output: str | JsonObject | None = Field(
        default=None,
        validation_alias=AliasChoices("output", "output_text"),
    )
    workflow_id: str | None = None
    correlation_id: str | None = None
    error: str | None = None
    usage: TokenUsage = Field(default_factory=TokenUsage)
    metadata: JsonObject = Field(default_factory=dict)
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    updated_at: datetime | None = None


class WorkflowCreate(WriteContract):
    workspace_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=4_000)
    nodes: list[WorkflowNode] = Field(min_length=1, max_length=500)
    input: JsonObject = Field(default_factory=dict)
    metadata: JsonObject = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def validate_dag(self) -> WorkflowCreate:
        node_ids = [node.id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("workflow node IDs must be unique")

        known = set(node_ids)
        edges = {node.id: node.depends_on for node in self.nodes}
        for node in self.nodes:
            missing = set(node.depends_on) - known
            if missing:
                raise ValueError(f"node {node.id} has unknown dependencies: {sorted(missing)}")
            if node.id in node.depends_on:
                raise ValueError(f"node {node.id} cannot depend on itself")

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str) -> None:
            if node_id in visiting:
                raise ValueError("workflow contains a dependency cycle")
            if node_id in visited:
                return
            visiting.add(node_id)
            for dependency in edges[node_id]:
                visit(dependency)
            visiting.remove(node_id)
            visited.add(node_id)

        for node_id in node_ids:
            visit(node_id)
        return self


class WorkflowSummary(ReadContract):
    id: str = Field(
        min_length=1,
        max_length=128,
        validation_alias=AliasChoices("id", "workflow_id"),
    )
    workspace_id: str | None = Field(default=None, max_length=128)
    name: str = Field(default="", max_length=120)
    description: str = ""
    status: WorkflowStatus = WorkflowStatus.QUEUED
    nodes: list[JsonObject] = Field(default_factory=list)
    node_counts: dict[str, int] = Field(default_factory=dict)
    input: JsonObject = Field(default_factory=dict)
    output: JsonObject = Field(default_factory=dict)
    metadata: JsonObject = Field(default_factory=dict)
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    updated_at: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def derive_workspace(cls, value: Any) -> Any:
        if not isinstance(value, dict) or value.get("workspace_id"):
            return value
        normalized = dict(value)
        metadata = normalized.get("metadata")
        if isinstance(metadata, dict) and metadata.get("workspace_id"):
            normalized["workspace_id"] = metadata["workspace_id"]
        return normalized


class ApprovalDecision(WriteContract):
    approved: bool
    actor: str = Field(default="operator", min_length=1, max_length=120)
    reason: str | None = Field(default=None, max_length=10_000)


Decision = ApprovalDecision


class ApprovalSummary(ReadContract):
    id: str = Field(
        min_length=1,
        max_length=128,
        validation_alias=AliasChoices("id", "approval_id"),
    )
    workspace_id: str | None = Field(default=None, max_length=128)
    action: str = Field(
        default="agent.action",
        min_length=1,
        max_length=500,
        validation_alias=AliasChoices("action", "subject"),
    )
    status: ApprovalStatus = ApprovalStatus.PENDING
    run_id: str | None = None
    workflow_id: str | None = None
    node_id: str | None = None
    requested_by: str | None = None
    reason: str = ""
    payload: JsonObject = Field(
        default_factory=dict,
        validation_alias=AliasChoices("payload", "arguments"),
    )
    decision: ApprovalDecision | None = None
    requested_at: datetime | None = Field(
        default=None,
        validation_alias=AliasChoices("requested_at", "created_at"),
    )
    decided_at: datetime | None = None
    expires_at: datetime | None = None


class ArtifactSummary(ReadContract):
    id: str = Field(
        min_length=1,
        max_length=128,
        validation_alias=AliasChoices("id", "artifact_id"),
    )
    workspace_id: str | None = Field(default=None, max_length=128)
    name: str = Field(min_length=1, max_length=500)
    mime_type: str = Field(default="application/octet-stream", min_length=1, max_length=255)
    uri: str = Field(
        min_length=1,
        max_length=8_000,
        validation_alias=AliasChoices("uri", "download_url"),
    )
    run_id: str | None = None
    workflow_id: str | None = None
    node_id: str | None = None
    size_bytes: int | None = Field(
        default=None,
        ge=0,
        validation_alias=AliasChoices("size_bytes", "size"),
    )
    checksum: str | None = Field(
        default=None,
        validation_alias=AliasChoices("checksum", "sha256"),
    )
    metadata: JsonObject = Field(default_factory=dict)
    created_at: datetime | None = None


class UnifiedEvent(ReadContract):
    """Cross-service event envelope.

    Identifier fields deliberately have transport-neutral names. A producer
    can correlate events emitted by the runtime, fleet, forge, observability,
    and realtime services without leaking their internal database schemas.
    """

    id: str = Field(
        default_factory=lambda: new_id("evt"),
        min_length=1,
        max_length=128,
        validation_alias=AliasChoices("id", "event_id"),
    )
    source: str = Field(min_length=1, max_length=500)
    type: str = Field(min_length=1, max_length=500)
    correlation: str | None = Field(default=None, max_length=256)
    causation: str | None = Field(default=None, max_length=256)
    tenant: str | None = Field(
        default=None,
        max_length=128,
        validation_alias=AliasChoices("tenant", "tenant_id"),
    )
    workspace: str | None = Field(
        default=None,
        max_length=128,
        validation_alias=AliasChoices("workspace", "workspace_id"),
    )
    agent: str | None = Field(
        default=None,
        max_length=128,
        validation_alias=AliasChoices("agent", "agent_id"),
    )
    run: str | None = Field(
        default=None,
        max_length=128,
        validation_alias=AliasChoices("run", "run_id"),
    )
    workflow: str | None = Field(
        default=None,
        max_length=128,
        validation_alias=AliasChoices("workflow", "workflow_id"),
    )
    node: str | None = Field(
        default=None,
        max_length=128,
        validation_alias=AliasChoices("node", "node_id"),
    )
    payload: JsonObject = Field(default_factory=dict)
    timestamp: datetime = Field(
        default_factory=utc_now,
        validation_alias=AliasChoices("timestamp", "created_at"),
    )
    sequence: int | None = Field(default=None, ge=0)
    schema_version: Literal["1.0"] = "1.0"
    metadata: JsonObject = Field(default_factory=dict)


class OverviewCounts(ReadContract):
    workspaces: int = Field(default=0, ge=0)
    agents: int = Field(default=0, ge=0)
    runs: int = Field(default=0, ge=0)
    active_workflows: int = Field(default=0, ge=0)
    pending_approvals: int = Field(default=0, ge=0)
    skills: int = Field(default=0, ge=0)
    online_workers: int = Field(default=0, ge=0)


class OverviewMetrics(ReadContract):
    tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0, ge=0)
    success_rate: float = Field(default=0, ge=0)
    quality: float = Field(default=0, ge=0)
    latency_ms: float = Field(default=0, ge=0)


class Overview(ReadContract):
    counts: OverviewCounts = Field(default_factory=OverviewCounts)
    metrics: OverviewMetrics = Field(default_factory=OverviewMetrics)
    recent_runs: list[RunSummary] = Field(default_factory=list)
    workflows: list[WorkflowSummary] = Field(default_factory=list)
    approvals: list[ApprovalSummary] = Field(default_factory=list)
    route_metrics: list[JsonObject] = Field(default_factory=list)
    events: list[UnifiedEvent] = Field(default_factory=list)


class SkillSummary(ReadContract):
    id: str | None = Field(default=None, max_length=256)
    name: str = Field(min_length=1, max_length=256)
    version: str = "0.0.0"
    status: str = "available"
    description: str = ""
    capabilities: list[str] = Field(default_factory=list)
    metadata: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def derive_id(self) -> SkillSummary:
        if self.id is None:
            self.id = self.name
        return self


PUBLIC_MODELS: tuple[type[BaseModel], ...] = (
    WorkspaceCreate,
    Workspace,
    AgentManifest,
    AgentCreate,
    AgentSummary,
    Budget,
    WorkflowNode,
    RunCreate,
    TokenUsage,
    RunSummary,
    WorkflowCreate,
    WorkflowSummary,
    ApprovalDecision,
    ApprovalSummary,
    ArtifactSummary,
    UnifiedEvent,
    OverviewCounts,
    OverviewMetrics,
    Overview,
    SkillSummary,
)
