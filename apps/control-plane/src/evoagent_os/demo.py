from __future__ import annotations

from typing import Any

from evoagent_fleet.models import Budget, Completion, NodeSpec, WorkerRegistration, WorkflowSpec
from evoagent_fleet.orchestrator import Orchestrator

DEMO_WORKER = "worker_local_deterministic"
DEMO_CAPABILITIES = ["research", "analysis", "writing", "artifacts", "citations"]


class DemoCoordinator:
    def __init__(self, orchestrator: Orchestrator) -> None:
        self.orchestrator = orchestrator
        self.orchestrator.register(
            WorkerRegistration(
                worker_id=DEMO_WORKER,
                capabilities=DEMO_CAPABILITIES,
                pool="offline-deterministic",
                max_concurrency=2,
                metadata={"provider": "offline", "reproducible": True},
            )
        )

    def launch(self, workspace_id: str = "ws_default") -> dict[str, Any]:
        spec = WorkflowSpec(
            name="AI agent market intelligence brief",
            metadata={
                "workspace_id": workspace_id,
                "scenario": "market-research",
                "provider": "deterministic/offline",
            },
            nodes=[
                NodeSpec(
                    id="research",
                    objective="Collect a source ledger and segment the enterprise agent market",
                    capabilities=["research", "citations"],
                    budget=Budget(tokens=12_000, cost_usd=0.35, seconds=90),
                ),
                NodeSpec(
                    id="analysis",
                    objective=(
                        "Score opportunities by urgency, willingness to pay, and delivery risk"
                    ),
                    depends_on=["research"],
                    capabilities=["analysis"],
                    budget=Budget(tokens=8_000, cost_usd=0.25, seconds=60),
                ),
                NodeSpec(
                    id="draft",
                    objective="Create an evidence-linked executive brief and recommendation",
                    depends_on=["analysis"],
                    capabilities=["writing", "artifacts"],
                    budget=Budget(tokens=10_000, cost_usd=0.30, seconds=90),
                ),
                NodeSpec(
                    id="publish",
                    objective="Publish approved Markdown, HTML, and CSV deliverables",
                    depends_on=["draft"],
                    capabilities=["artifacts"],
                    budget=Budget(tokens=2_000, cost_usd=0.05, seconds=30),
                    approval_required=True,
                ),
            ],
        )
        workflow_id = self.orchestrator.submit(spec)
        completed = self.drain(workflow_id)
        return {
            "workflow_id": workflow_id,
            "status": self.orchestrator.view(workflow_id)["status"],
            "completed_nodes": completed,
            "approval_id": f"fleet:{workflow_id}:publish",
        }

    def drain(self, workflow_id: str) -> list[str]:
        completed: list[str] = []
        for _ in range(100):
            lease = self.orchestrator.claim(DEMO_WORKER, workflow_id=workflow_id)
            if lease is None:
                break
            node_id = lease["node_id"]
            result = self._result(node_id)
            self.orchestrator.complete(DEMO_WORKER, lease["lease_token"], result)
            completed.append(node_id)
        return completed

    @staticmethod
    def _result(node_id: str) -> Completion:
        artifacts: dict[str, str] = {}
        output: dict[str, Any]
        if node_id == "research":
            output = {"segments": 4, "sources": 6, "confidence": 0.86}
            artifacts = {
                "source-ledger.md": (
                    "# Source ledger\n\n"
                    "Offline demonstration evidence set with six traceable synthetic records.\n"
                    "Replace these fixtures with approved connectors in a live workspace.\n"
                )
            }
        elif node_id == "analysis":
            output = {
                "leading_opportunity": "governed durable agent operations",
                "quality": 0.91,
                "risks": ["integration cost", "evaluation drift", "permission scope"],
            }
            artifacts = {
                "opportunity-score.csv": (
                    "segment,urgency,willingness_to_pay,delivery_risk,score\n"
                    "regulated-operations,0.92,0.88,0.54,0.86\n"
                    "knowledge-workflows,0.80,0.76,0.38,0.79\n"
                    "consumer-assistants,0.61,0.42,0.65,0.49\n"
                )
            }
        elif node_id == "draft":
            output = {"recommendation": "Launch a governed operations pilot", "quality": 0.93}
            artifacts = {
                "executive-brief-draft.md": (
                    "# Executive brief\n\n"
                    "## Recommendation\n\n"
                    "Prioritize governed, durable agent workflows where auditability and "
                    "recovery are buying criteria.\n\n"
                    "## Evidence\n\n"
                    "The deterministic demo preserves provenance, budget accounting, and "
                    "approval state.\n"
                )
            }
        else:
            output = {"published": True, "formats": ["markdown", "html", "csv"]}
            artifacts = {
                "market-brief.md": "# Governed Agent Market Brief\n\nApproved release artifact.\n",
                "market-brief.html": (
                    "<!doctype html><html><body><main><h1>Governed Agent Market Brief</h1>"
                    "<p>Approved release artifact with traceable provenance.</p>"
                    "</main></body></html>"
                ),
                "evidence.csv": (
                    "claim,source,confidence\nAuditability drives adoption,fixture-001,0.86\n"
                ),
            }
        return Completion(
            output=output,
            artifacts=artifacts,
            tokens_used={"research": 4200, "analysis": 2700, "draft": 3100}.get(node_id, 650),
            cost_usd={"research": 0.11, "analysis": 0.07, "draft": 0.09}.get(node_id, 0.01),
            duration_seconds={"research": 2.8, "analysis": 1.7, "draft": 2.1}.get(node_id, 0.4),
            quality={"research": 0.86, "analysis": 0.91, "draft": 0.93}.get(node_id, 0.95),
        )
