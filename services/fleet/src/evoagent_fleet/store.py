from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS workflows(
 workflow_id TEXT PRIMARY KEY,name TEXT NOT NULL,status TEXT NOT NULL,metadata_json TEXT NOT NULL,
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS nodes(
 workflow_id TEXT NOT NULL REFERENCES workflows(workflow_id),node_id TEXT NOT NULL,objective TEXT NOT NULL,
 status TEXT NOT NULL,deps_json TEXT NOT NULL,capabilities_json TEXT NOT NULL,input_json TEXT NOT NULL,
 output_json TEXT,error TEXT,budget_json TEXT NOT NULL,approval_required INTEGER NOT NULL,
 approval_actor TEXT,attempts INTEGER NOT NULL DEFAULT 0,max_attempts INTEGER NOT NULL,
 leased_by TEXT,lease_token TEXT,lease_expires REAL,tokens_used INTEGER NOT NULL DEFAULT 0,
 cost_usd REAL NOT NULL DEFAULT 0,duration_seconds REAL NOT NULL DEFAULT 0,created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,PRIMARY KEY(workflow_id,node_id)
);
CREATE TABLE IF NOT EXISTS workers(
 worker_id TEXT PRIMARY KEY,capabilities_json TEXT NOT NULL,pool TEXT NOT NULL,max_concurrency INTEGER NOT NULL,
 metadata_json TEXT NOT NULL,status TEXT NOT NULL,last_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS artifacts(
 artifact_id TEXT PRIMARY KEY,workflow_id TEXT NOT NULL,node_id TEXT NOT NULL,name TEXT NOT NULL,
 digest TEXT NOT NULL,size INTEGER NOT NULL,path TEXT NOT NULL,created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events(
 seq INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT NOT NULL,workflow_id TEXT,node_id TEXT,
 payload_json TEXT NOT NULL,created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS route_metrics(
 pool TEXT PRIMARY KEY,runs INTEGER NOT NULL DEFAULT 0,successes INTEGER NOT NULL DEFAULT 0,
 quality_sum REAL NOT NULL DEFAULT 0,cost_sum REAL NOT NULL DEFAULT 0,latency_sum REAL NOT NULL DEFAULT 0,
 score REAL NOT NULL DEFAULT 0.5,updated_at TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(UTC).isoformat()


def identifier(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:20]}"


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.connection.executescript(SCHEMA)

    def close(self) -> None:
        self.connection.close()

    def execute(self, sql: str, values: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self.lock:
            cursor = self.connection.execute(sql, values)
            self.connection.commit()
            return cursor

    def query(self, sql: str, values: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.connection.execute(sql, values).fetchall()
        return [dict(row) for row in rows]

    def event(
        self,
        kind: str,
        payload: dict[str, Any],
        workflow_id: str | None = None,
        node_id: str | None = None,
    ) -> int:
        cursor = self.execute(
            "INSERT INTO events(kind,workflow_id,node_id,payload_json,created_at) VALUES(?,?,?,?,?)",
            (kind, workflow_id, node_id, json.dumps(payload), now()),
        )
        return int(cursor.lastrowid)

    def events(self, after: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.query("SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT ?", (after, limit))
        for row in rows:
            row["payload"] = json.loads(row.pop("payload_json"))
        return rows
