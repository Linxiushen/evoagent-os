from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from .models import Completion, NodeStatus, WorkerRegistration, WorkflowSpec
from .store import Store, identifier, now


class Orchestrator:
    def __init__(self, store: Store, artifact_root: Path | str, lease_seconds: int = 60) -> None:
        self.store = store
        self.artifact_root = Path(artifact_root).resolve()
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self.lease_seconds = lease_seconds

    def submit(self, spec: WorkflowSpec) -> str:
        workflow_id = identifier("wf")
        timestamp = now()
        with self.store.lock:
            self.store.connection.execute(
                "INSERT INTO workflows VALUES(?,?,?,?,?,?)",
                (
                    workflow_id,
                    spec.name,
                    "running",
                    json.dumps(spec.metadata),
                    timestamp,
                    timestamp,
                ),
            )
            for node in spec.nodes:
                if node.depends_on:
                    status = NodeStatus.BLOCKED
                elif node.approval_required:
                    status = NodeStatus.AWAITING_APPROVAL
                else:
                    status = NodeStatus.QUEUED
                self.store.connection.execute(
                    """INSERT INTO nodes(
                       workflow_id,node_id,objective,status,deps_json,capabilities_json,input_json,
                       budget_json,approval_required,max_attempts,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        workflow_id,
                        node.id,
                        node.objective,
                        status.value,
                        json.dumps(node.depends_on),
                        json.dumps(node.capabilities),
                        json.dumps(node.input),
                        node.budget.model_dump_json(),
                        int(node.approval_required),
                        node.max_attempts,
                        timestamp,
                        timestamp,
                    ),
                )
            self.store.connection.commit()
        self.store.event(
            "workflow.submitted", {"name": spec.name, "nodes": len(spec.nodes)}, workflow_id
        )
        return workflow_id

    def register(self, worker: WorkerRegistration) -> None:
        self.store.execute(
            """INSERT INTO workers VALUES(?,?,?,?,?,?,?) ON CONFLICT(worker_id) DO UPDATE SET
               capabilities_json=excluded.capabilities_json,pool=excluded.pool,
               max_concurrency=excluded.max_concurrency,metadata_json=excluded.metadata_json,
               status='online',last_seen=excluded.last_seen""",
            (
                worker.worker_id,
                json.dumps(worker.capabilities),
                worker.pool,
                worker.max_concurrency,
                json.dumps(worker.metadata),
                "online",
                now(),
            ),
        )
        self.store.execute(
            """INSERT INTO route_metrics(pool,updated_at) VALUES(?,?)
               ON CONFLICT(pool) DO NOTHING""",
            (worker.pool, now()),
        )

    def claim(self, worker_id: str) -> dict[str, Any] | None:
        self.sweep_expired()
        with self.store.lock:
            worker = self.store.connection.execute(
                "SELECT * FROM workers WHERE worker_id=?", (worker_id,)
            ).fetchone()
            if worker is None:
                raise ValueError("Worker is not registered")
            active = self.store.connection.execute(
                "SELECT COUNT(*) AS n FROM nodes WHERE leased_by=? AND status='leased'",
                (worker_id,),
            ).fetchone()["n"]
            if active >= worker["max_concurrency"]:
                return None
            capabilities = set(json.loads(worker["capabilities_json"]))
            candidates = self.store.connection.execute(
                "SELECT * FROM nodes WHERE status='queued' ORDER BY created_at,node_id"
            ).fetchall()
            selected = next(
                (
                    row
                    for row in candidates
                    if set(json.loads(row["capabilities_json"])) <= capabilities
                ),
                None,
            )
            if selected is None:
                return None
            token = identifier("lease")
            expires = time.time() + self.lease_seconds
            changed = self.store.connection.execute(
                """UPDATE nodes SET status='leased',leased_by=?,lease_token=?,lease_expires=?,
                   attempts=attempts+1,updated_at=? WHERE workflow_id=? AND node_id=? AND status='queued'""",
                (worker_id, token, expires, now(), selected["workflow_id"], selected["node_id"]),
            ).rowcount
            self.store.connection.execute(
                "UPDATE workers SET last_seen=? WHERE worker_id=?", (now(), worker_id)
            )
            self.store.connection.commit()
            if not changed:
                return None
        self.store.event(
            "node.leased",
            {"worker_id": worker_id, "lease_expires": expires},
            selected["workflow_id"],
            selected["node_id"],
        )
        return {
            "workflow_id": selected["workflow_id"],
            "node_id": selected["node_id"],
            "objective": selected["objective"],
            "input": json.loads(selected["input_json"]),
            "budget": json.loads(selected["budget_json"]),
            "lease_token": token,
            "lease_expires": expires,
        }

    def heartbeat(self, worker_id: str, lease_token: str) -> float:
        expires = time.time() + self.lease_seconds
        changed = self.store.execute(
            """UPDATE nodes SET lease_expires=?,updated_at=?
               WHERE leased_by=? AND lease_token=? AND status='leased'""",
            (expires, now(), worker_id, lease_token),
        ).rowcount
        if not changed:
            raise ValueError("Lease is no longer active")
        self.store.execute("UPDATE workers SET last_seen=? WHERE worker_id=?", (now(), worker_id))
        return expires

    def complete(self, worker_id: str, lease_token: str, result: Completion) -> dict[str, Any]:
        row = self._lease(worker_id, lease_token)
        budget = json.loads(row["budget_json"])
        if result.tokens_used > budget["tokens"] or result.cost_usd > budget["cost_usd"]:
            raise ValueError("Completion exceeds node budget")
        artifacts = [
            self._artifact(row["workflow_id"], row["node_id"], name, content)
            for name, content in result.artifacts.items()
        ]
        self.store.execute(
            """UPDATE nodes SET status='completed',output_json=?,tokens_used=?,cost_usd=?,
               duration_seconds=?,leased_by=NULL,lease_token=NULL,lease_expires=NULL,updated_at=?
               WHERE workflow_id=? AND node_id=?""",
            (
                json.dumps(result.output),
                result.tokens_used,
                result.cost_usd,
                result.duration_seconds,
                now(),
                row["workflow_id"],
                row["node_id"],
            ),
        )
        pool = self.store.query("SELECT pool FROM workers WHERE worker_id=?", (worker_id,))[0][
            "pool"
        ]
        self._record_route(pool, True, result.quality, result.cost_usd, result.duration_seconds)
        self.store.event(
            "node.completed",
            {"worker_id": worker_id, "artifacts": artifacts},
            row["workflow_id"],
            row["node_id"],
        )
        self._advance(row["workflow_id"])
        return {
            "workflow_id": row["workflow_id"],
            "node_id": row["node_id"],
            "artifacts": artifacts,
        }

    def fail(self, worker_id: str, lease_token: str, error: str, retryable: bool = True) -> None:
        row = self._lease(worker_id, lease_token)
        retry = retryable and row["attempts"] < row["max_attempts"]
        status = NodeStatus.QUEUED if retry else NodeStatus.FAILED
        self.store.execute(
            """UPDATE nodes SET status=?,error=?,leased_by=NULL,lease_token=NULL,lease_expires=NULL,updated_at=?
               WHERE workflow_id=? AND node_id=?""",
            (status.value, error, now(), row["workflow_id"], row["node_id"]),
        )
        pool = self.store.query("SELECT pool FROM workers WHERE worker_id=?", (worker_id,))[0][
            "pool"
        ]
        self._record_route(pool, False, 0, 0, 0)
        self.store.event(
            "node.retrying" if retry else "node.failed",
            {"worker_id": worker_id, "error": error},
            row["workflow_id"],
            row["node_id"],
        )
        if not retry:
            self.store.execute(
                "UPDATE workflows SET status='failed',updated_at=? WHERE workflow_id=?",
                (now(), row["workflow_id"]),
            )

    def approve(self, workflow_id: str, node_id: str, approved: bool, actor: str) -> None:
        status = NodeStatus.QUEUED if approved else NodeStatus.FAILED
        changed = self.store.execute(
            """UPDATE nodes SET status=?,approval_actor=?,error=?,updated_at=?
               WHERE workflow_id=? AND node_id=? AND status='awaiting_approval'""",
            (
                status.value,
                actor,
                None if approved else "Approval denied",
                now(),
                workflow_id,
                node_id,
            ),
        ).rowcount
        if not changed:
            raise ValueError("Node is not awaiting approval")
        self.store.event(
            "node.approval", {"approved": approved, "actor": actor}, workflow_id, node_id
        )
        if not approved:
            self.store.execute(
                "UPDATE workflows SET status='failed',updated_at=? WHERE workflow_id=?",
                (now(), workflow_id),
            )

    def sweep_expired(self) -> int:
        expired = self.store.query(
            "SELECT * FROM nodes WHERE status='leased' AND lease_expires<?", (time.time(),)
        )
        for row in expired:
            retry = row["attempts"] < row["max_attempts"]
            self.store.execute(
                """UPDATE nodes SET status=?,error='Lease expired',leased_by=NULL,lease_token=NULL,
                   lease_expires=NULL,updated_at=? WHERE workflow_id=? AND node_id=?""",
                (
                    NodeStatus.QUEUED.value if retry else NodeStatus.FAILED.value,
                    now(),
                    row["workflow_id"],
                    row["node_id"],
                ),
            )
            self.store.event("lease.expired", {"retry": retry}, row["workflow_id"], row["node_id"])
        return len(expired)

    def view(self, workflow_id: str) -> dict[str, Any]:
        workflows = self.store.query("SELECT * FROM workflows WHERE workflow_id=?", (workflow_id,))
        if not workflows:
            raise ValueError("Workflow not found")
        workflow = workflows[0]
        workflow["metadata"] = json.loads(workflow.pop("metadata_json"))
        nodes = self.store.query(
            "SELECT * FROM nodes WHERE workflow_id=? ORDER BY created_at,node_id", (workflow_id,)
        )
        for node in nodes:
            for key in ("deps", "capabilities", "input", "output", "budget"):
                raw = node.pop(f"{key}_json")
                node[key] = json.loads(raw) if raw else None
        return {**workflow, "nodes": nodes}

    def list_workflows(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.store.query(
            "SELECT * FROM workflows ORDER BY created_at DESC LIMIT ?", (limit,)
        )

    def route_metrics(self) -> list[dict[str, Any]]:
        return self.store.query("SELECT * FROM route_metrics ORDER BY score DESC,pool")

    def _lease(self, worker_id: str, lease_token: str) -> dict[str, Any]:
        rows = self.store.query(
            "SELECT * FROM nodes WHERE leased_by=? AND lease_token=? AND status='leased'",
            (worker_id, lease_token),
        )
        if not rows or float(rows[0]["lease_expires"]) < time.time():
            raise ValueError("Lease is no longer active")
        return rows[0]

    def _advance(self, workflow_id: str) -> None:
        nodes = self.store.query("SELECT * FROM nodes WHERE workflow_id=?", (workflow_id,))
        statuses = {row["node_id"]: row["status"] for row in nodes}
        for row in nodes:
            if row["status"] != NodeStatus.BLOCKED:
                continue
            if all(
                statuses[parent] == NodeStatus.COMPLETED for parent in json.loads(row["deps_json"])
            ):
                target = (
                    NodeStatus.AWAITING_APPROVAL if row["approval_required"] else NodeStatus.QUEUED
                )
                self.store.execute(
                    "UPDATE nodes SET status=?,updated_at=? WHERE workflow_id=? AND node_id=?",
                    (target.value, now(), workflow_id, row["node_id"]),
                )
                self.store.event(
                    "node.ready", {"status": target.value}, workflow_id, row["node_id"]
                )
        refreshed = self.store.query("SELECT status FROM nodes WHERE workflow_id=?", (workflow_id,))
        if refreshed and all(row["status"] == NodeStatus.COMPLETED for row in refreshed):
            self.store.execute(
                "UPDATE workflows SET status='completed',updated_at=? WHERE workflow_id=?",
                (now(), workflow_id),
            )
            self.store.event("workflow.completed", {}, workflow_id)

    def _artifact(self, workflow_id: str, node_id: str, name: str, content: str) -> dict[str, Any]:
        if Path(name).name != name or not name:
            raise ValueError("Artifact name must be a plain filename")
        data = content.encode()
        digest = hashlib.sha256(data).hexdigest()
        directory = self.artifact_root / workflow_id / node_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{digest[:12]}-{name}"
        path.write_bytes(data)
        artifact_id = identifier("artifact")
        self.store.execute(
            "INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?)",
            (artifact_id, workflow_id, node_id, name, digest, len(data), str(path), now()),
        )
        return {"artifact_id": artifact_id, "name": name, "sha256": digest, "size": len(data)}

    def _record_route(
        self, pool: str, success: bool, quality: float, cost: float, latency: float
    ) -> None:
        self.store.execute(
            """UPDATE route_metrics SET runs=runs+1,successes=successes+?,quality_sum=quality_sum+?,
               cost_sum=cost_sum+?,latency_sum=latency_sum+?,score=(successes+?+1.0)/(runs+2.0)*0.7+
               ((quality_sum+?)/(runs+1.0))*0.3,updated_at=? WHERE pool=?""",
            (int(success), quality, cost, latency, int(success), quality, now(), pool),
        )
