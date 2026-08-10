from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS workspaces(
  workspace_id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,name TEXT NOT NULL,slug TEXT NOT NULL,
  description TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(tenant_id,slug)
);
CREATE TABLE IF NOT EXISTS agents(
  agent_id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
  name TEXT NOT NULL,role TEXT NOT NULL,description TEXT NOT NULL,model TEXT NOT NULL,
  capabilities_json TEXT NOT NULL,tools_json TEXT NOT NULL DEFAULT '[]',
  skills_json TEXT NOT NULL DEFAULT '[]',system_prompt TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',status TEXT NOT NULL,
  created_at TEXT NOT NULL,updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS run_links(
  run_id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,agent_id TEXT NOT NULL,objective TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS control_events(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT UNIQUE NOT NULL,kind TEXT NOT NULL,
  workspace_id TEXT,run_id TEXT,workflow_id TEXT,payload_json TEXT NOT NULL,created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evaluations(
  evaluation_id TEXT PRIMARY KEY,name TEXT NOT NULL,status TEXT NOT NULL,score REAL NOT NULL,
  baseline TEXT NOT NULL,candidate TEXT NOT NULL,report_json TEXT NOT NULL,created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS idempotency_keys(
  scope TEXT NOT NULL,key TEXT NOT NULL,response_json TEXT NOT NULL,created_at TEXT NOT NULL,
  PRIMARY KEY(scope,key)
);
"""


def now() -> str:
    return datetime.now(UTC).isoformat()


def identifier(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:20]}"


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:64] or f"workspace-{uuid4().hex[:8]}"


class ControlStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.connection.executescript(SCHEMA)
            self._migrate()
            self._seed()

    def close(self) -> None:
        with self.lock:
            self.connection.close()

    def _seed(self) -> None:
        timestamp = now()
        self.connection.execute(
            """INSERT OR IGNORE INTO workspaces VALUES(?,?,?,?,?,?)""",
            (
                "ws_default",
                "tenant_local",
                "Agent Operations",
                "agent-operations",
                "Default local-first governed agent workspace",
                timestamp,
            ),
        )
        agents = (
            (
                "agent_coordinator",
                "Coordinator",
                "planner",
                "Plans and governs multi-agent work",
                ["planning", "delegation", "approval"],
            ),
            (
                "agent_researcher",
                "Researcher",
                "researcher",
                "Collects evidence with explicit provenance",
                ["research", "retrieval", "citations"],
            ),
            (
                "agent_analyst",
                "Analyst",
                "analyst",
                "Synthesizes evidence under quality and cost gates",
                ["analysis", "evaluation"],
            ),
            (
                "agent_writer",
                "Publisher",
                "writer",
                "Produces governed deliverables and artifacts",
                ["writing", "artifacts"],
            ),
        )
        for agent_id, name, role, description, capabilities in agents:
            self.connection.execute(
                """INSERT OR IGNORE INTO agents(
                     agent_id,workspace_id,name,role,description,model,capabilities_json,
                     tools_json,skills_json,system_prompt,metadata_json,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    agent_id,
                    "ws_default",
                    name,
                    role,
                    description,
                    "deterministic/offline",
                    json.dumps(capabilities),
                    "[]",
                    "[]",
                    "",
                    "{}",
                    "ready",
                    timestamp,
                    timestamp,
                ),
            )
        self.connection.execute(
            """INSERT OR IGNORE INTO evaluations VALUES(?,?,?,?,?,?,?,?)""",
            (
                "eval_trace_contract",
                "Trace Contract regression gate",
                "passed",
                1.0,
                "offline-v1",
                "offline-v1",
                json.dumps({"violations": [], "protocol_score": 100, "content_match": True}),
                timestamp,
            ),
        )
        self.connection.commit()

    def _migrate(self) -> None:
        columns = {
            row["name"] for row in self.connection.execute("PRAGMA table_info(agents)").fetchall()
        }
        additions = {
            "tools_json": "TEXT NOT NULL DEFAULT '[]'",
            "skills_json": "TEXT NOT NULL DEFAULT '[]'",
            "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
        }
        for name, definition in additions.items():
            if name not in columns:
                self.connection.execute(f"ALTER TABLE agents ADD COLUMN {name} {definition}")
        self.connection.commit()

    def execute(self, sql: str, values: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self.lock:
            cursor = self.connection.execute(sql, values)
            self.connection.commit()
            return cursor

    def query(self, sql: str, values: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(row) for row in self.connection.execute(sql, values).fetchall()]

    def list_workspaces(self) -> list[dict[str, Any]]:
        return self.query("SELECT * FROM workspaces ORDER BY created_at,name")

    def create_workspace(self, name: str, description: str, tenant_id: str) -> dict[str, Any]:
        workspace_id = identifier("ws")
        created_at = now()
        slug = slugify(name)
        suffix = 1
        while self.query(
            "SELECT 1 FROM workspaces WHERE tenant_id=? AND slug=?", (tenant_id, slug)
        ):
            suffix += 1
            slug = f"{slug[:58]}-{suffix}"
        self.execute(
            "INSERT INTO workspaces VALUES(?,?,?,?,?,?)",
            (workspace_id, tenant_id, name, slug, description, created_at),
        )
        self.event("workspace.created", {"name": name}, workspace_id=workspace_id)
        return self.query("SELECT * FROM workspaces WHERE workspace_id=?", (workspace_id,))[0]

    def list_agents(self, workspace_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM agents"
        values: tuple[Any, ...] = ()
        if workspace_id:
            sql += " WHERE workspace_id=?"
            values = (workspace_id,)
        rows = self.query(sql + " ORDER BY created_at,name", values)
        for row in rows:
            row["capabilities"] = json.loads(row.pop("capabilities_json"))
            row["tools"] = json.loads(row.pop("tools_json"))
            row["skills"] = json.loads(row.pop("skills_json"))
            row["metadata"] = json.loads(row.pop("metadata_json"))
        return rows

    def create_agent(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.query(
            "SELECT 1 FROM workspaces WHERE workspace_id=?", (payload["workspace_id"],)
        ):
            raise ValueError("Workspace not found")
        agent_id = identifier("agent")
        timestamp = now()
        self.execute(
            """INSERT INTO agents(
                 agent_id,workspace_id,name,role,description,model,capabilities_json,
                 tools_json,skills_json,system_prompt,metadata_json,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                agent_id,
                payload["workspace_id"],
                payload["name"],
                payload["role"],
                payload["description"],
                payload["model"],
                json.dumps(payload["capabilities"]),
                json.dumps(payload["tools"]),
                json.dumps(payload["skills"]),
                payload["system_prompt"],
                json.dumps(payload["metadata"]),
                "ready",
                timestamp,
                timestamp,
            ),
        )
        self.event(
            "agent.created",
            {"agent_id": agent_id, "name": payload["name"], "role": payload["role"]},
            workspace_id=payload["workspace_id"],
        )
        return next(
            item
            for item in self.list_agents(payload["workspace_id"])
            if item["agent_id"] == agent_id
        )

    def link_run(self, run_id: str, workspace_id: str, agent_id: str, objective: str) -> None:
        self.execute(
            "INSERT OR REPLACE INTO run_links VALUES(?,?,?,?,?)",
            (run_id, workspace_id, agent_id, objective, now()),
        )

    def run_links(self) -> dict[str, dict[str, Any]]:
        return {row["run_id"]: row for row in self.query("SELECT * FROM run_links")}

    def event(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        workspace_id: str | None = None,
        run_id: str | None = None,
        workflow_id: str | None = None,
    ) -> int:
        event_id = identifier("evt")
        cursor = self.execute(
            """INSERT INTO control_events(
                 event_id,kind,workspace_id,run_id,workflow_id,payload_json,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (event_id, kind, workspace_id, run_id, workflow_id, json.dumps(payload), now()),
        )
        return int(cursor.lastrowid)

    def events(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.query(
            "SELECT * FROM control_events ORDER BY seq DESC LIMIT ?", (min(max(limit, 1), 500),)
        )
        for row in rows:
            row["payload"] = json.loads(row.pop("payload_json"))
        return rows

    def evaluations(self) -> list[dict[str, Any]]:
        rows = self.query("SELECT * FROM evaluations ORDER BY created_at DESC")
        for row in rows:
            row["report"] = json.loads(row.pop("report_json"))
        return rows

    def idempotent_get(self, scope: str, key: str | None) -> dict[str, Any] | None:
        if not key:
            return None
        rows = self.query(
            "SELECT response_json FROM idempotency_keys WHERE scope=? AND key=?", (scope, key)
        )
        return json.loads(rows[0]["response_json"]) if rows else None

    def idempotent_put(self, scope: str, key: str | None, response: dict[str, Any]) -> None:
        if key:
            self.execute(
                "INSERT OR IGNORE INTO idempotency_keys VALUES(?,?,?,?)",
                (scope, key, json.dumps(response), now()),
            )
