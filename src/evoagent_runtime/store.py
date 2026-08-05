from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .models import RunStatus, new_id, utc_now

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY,
  channel TEXT NOT NULL,
  peer_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL REFERENCES sessions(session_id),
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  name TEXT,
  tool_call_id TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES sessions(session_id),
  status TEXT NOT NULL,
  input_text TEXT NOT NULL,
  output_text TEXT,
  error TEXT,
  prompt_version INTEGER NOT NULL,
  usage_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT,
  session_id TEXT,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memories (
  memory_id TEXT UNIQUE NOT NULL,
  session_id TEXT NOT NULL,
  text TEXT NOT NULL,
  tags_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
  text, content='memories', content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
  INSERT INTO memory_fts(rowid, text) VALUES (new.rowid, new.text);
END;

CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
  INSERT INTO memory_fts(memory_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
END;

CREATE TABLE IF NOT EXISTS approvals (
  approval_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  tool_name TEXT NOT NULL,
  args_json TEXT NOT NULL,
  reason TEXT NOT NULL,
  status TEXT NOT NULL,
  actor TEXT,
  created_at TEXT NOT NULL,
  decided_at TEXT
);

CREATE TABLE IF NOT EXISTS prompts (
  version INTEGER PRIMARY KEY,
  prompt TEXT NOT NULL,
  state TEXT NOT NULL,
  score REAL,
  parent_version INTEGER,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
  feedback_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  rating INTEGER NOT NULL,
  comment TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evolution_candidates (
  candidate_id TEXT PRIMARY KEY,
  parent_version INTEGER NOT NULL,
  prompt TEXT NOT NULL,
  status TEXT NOT NULL,
  baseline_score REAL,
  candidate_score REAL,
  safety REAL,
  report_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
  job_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  interval_seconds INTEGER NOT NULL,
  message TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  next_run_at REAL NOT NULL,
  created_at TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._connection.executescript(SCHEMA)
            if not self._connection.execute("SELECT 1 FROM prompts").fetchone():
                self._connection.execute(
                    "INSERT INTO prompts(version,prompt,state,created_at) VALUES(1,?,?,?)",
                    (
                        (
                            "You are a careful local assistant. Use tools only when needed, "
                            "state uncertainty, and ask before consequential actions."
                        ),
                        "active",
                        utc_now(),
                    ),
                )
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def ensure_session(self, session_id: str, channel: str, peer_id: str) -> None:
        now = utc_now()
        with self._lock:
            self._connection.execute(
                """INSERT INTO sessions(session_id,channel,peer_id,created_at,updated_at)
                   VALUES(?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET updated_at=excluded.updated_at""",
                (session_id, channel, peer_id, now, now),
            )
            self._connection.commit()

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        name: str | None = None,
        tool_call_id: str | None = None,
    ) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT INTO messages(session_id,role,content,name,tool_call_id,created_at) VALUES(?,?,?,?,?,?)",
                (session_id, role, content, name, tool_call_id, utc_now()),
            )
            self._connection.commit()

    def history(self, session_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """SELECT role,content,name,tool_call_id,created_at FROM messages
                   WHERE session_id=? ORDER BY id DESC LIMIT ?""",
                (session_id, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def create_run(self, session_id: str, input_text: str, prompt_version: int) -> str:
        run_id = new_id("run")
        now = utc_now()
        with self._lock:
            self._connection.execute(
                """INSERT INTO runs(run_id,session_id,status,input_text,prompt_version,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (run_id, session_id, RunStatus.ACCEPTED, input_text, prompt_version, now, now),
            )
            self._connection.commit()
        return run_id

    def update_run(self, run_id: str, status: RunStatus, **values: Any) -> None:
        allowed = {"output_text", "error", "usage_json"}
        updates = {key: value for key, value in values.items() if key in allowed}
        updates["status"] = status.value
        updates["updated_at"] = utc_now()
        columns = ",".join(f"{key}=?" for key in updates)
        with self._lock:
            self._connection.execute(
                f"UPDATE runs SET {columns} WHERE run_id=?",
                (*updates.values(), run_id),
            )
            self._connection.commit()

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def event(
        self,
        kind: str,
        payload: dict[str, Any],
        run_id: str | None = None,
        session_id: str | None = None,
    ) -> int:
        with self._lock:
            cursor = self._connection.execute(
                "INSERT INTO events(run_id,session_id,kind,payload_json,created_at) VALUES(?,?,?,?,?)",
                (run_id, session_id, kind, json.dumps(payload), utc_now()),
            )
            self._connection.commit()
            return int(cursor.lastrowid)

    def events(self, after: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT ?", (after, limit)
            ).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload_json"])} for row in rows]

    def remember(self, session_id: str, text: str, tags: list[str] | None = None) -> str:
        memory_id = new_id("mem")
        with self._lock:
            self._connection.execute(
                "INSERT INTO memories(memory_id,session_id,text,tags_json,created_at) VALUES(?,?,?,?,?)",
                (memory_id, session_id, text, json.dumps(tags or []), utc_now()),
            )
            self._connection.commit()
        return memory_id

    def search_memory(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        terms = [part for part in query.replace('"', " ").split() if part]
        if not terms:
            return []
        match = " OR ".join(f'"{term}"' for term in terms[:12])
        with self._lock:
            rows = self._connection.execute(
                """SELECT m.memory_id,m.session_id,m.text,m.tags_json,m.created_at,bm25(memory_fts) AS rank
                   FROM memory_fts JOIN memories m ON m.rowid=memory_fts.rowid
                   WHERE memory_fts MATCH ? ORDER BY rank LIMIT ?""",
                (match, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def active_prompt(self) -> tuple[int, str]:
        with self._lock:
            row = self._connection.execute(
                "SELECT version,prompt FROM prompts WHERE state='active' ORDER BY version DESC LIMIT 1"
            ).fetchone()
        if row is None:
            raise RuntimeError("No active prompt")
        return int(row["version"]), str(row["prompt"])

    def prompt(self, version: int) -> str:
        with self._lock:
            row = self._connection.execute(
                "SELECT prompt FROM prompts WHERE version=?", (version,)
            ).fetchone()
        if row is None:
            raise ValueError(f"Unknown prompt version: {version}")
        return str(row["prompt"])

    def add_feedback(self, run_id: str, rating: int, comment: str) -> str:
        feedback_id = new_id("fb")
        with self._lock:
            self._connection.execute(
                "INSERT INTO feedback(feedback_id,run_id,rating,comment,created_at) VALUES(?,?,?,?,?)",
                (feedback_id, run_id, rating, comment, utc_now()),
            )
            self._connection.commit()
        return feedback_id

    def negative_feedback(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """SELECT f.*,r.input_text,r.output_text FROM feedback f JOIN runs r USING(run_id)
                   WHERE f.rating<0 ORDER BY f.created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_approval(
        self, run_id: str, session_id: str, tool_name: str, arguments: dict[str, Any], reason: str
    ) -> str:
        approval_id = new_id("approval")
        with self._lock:
            self._connection.execute(
                """INSERT INTO approvals(approval_id,run_id,session_id,tool_name,args_json,reason,status,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    approval_id,
                    run_id,
                    session_id,
                    tool_name,
                    json.dumps(arguments),
                    reason,
                    "pending",
                    utc_now(),
                ),
            )
            self._connection.commit()
        return approval_id

    def approval(self, approval_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM approvals WHERE approval_id=?", (approval_id,)
            ).fetchone()
        return dict(row) if row else None

    def decide_approval(self, approval_id: str, approved: bool, actor: str) -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE approvals SET status=?,actor=?,decided_at=? WHERE approval_id=? AND status='pending'",
                ("approved" if approved else "denied", actor, utc_now(), approval_id),
            )
            self._connection.commit()

    def pending_approvals(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM approvals WHERE status='pending' ORDER BY created_at"
            ).fetchall()
        return [dict(row) for row in rows]

    def execute(self, sql: str, parameters: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self._lock:
            cursor = self._connection.execute(sql, parameters)
            self._connection.commit()
            return cursor

    def query(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(sql, parameters).fetchall()
        return [dict(row) for row in rows]
