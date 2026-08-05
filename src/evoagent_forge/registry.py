from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import SkillManifest

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS releases(
  name TEXT NOT NULL, version TEXT NOT NULL, digest TEXT NOT NULL, description TEXT NOT NULL,
  manifest_json TEXT NOT NULL, signature_json TEXT, published_at TEXT NOT NULL,
  PRIMARY KEY(name, version), UNIQUE(digest)
);
CREATE VIRTUAL TABLE IF NOT EXISTS release_fts USING fts5(name, description, content='releases', content_rowid='rowid');
CREATE TRIGGER IF NOT EXISTS releases_ai AFTER INSERT ON releases BEGIN
  INSERT INTO release_fts(rowid,name,description) VALUES(new.rowid,new.name,new.description);
END;
CREATE TABLE IF NOT EXISTS evaluations(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, version TEXT NOT NULL, suite TEXT NOT NULL,
  passed INTEGER NOT NULL, score REAL NOT NULL, report_json TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


class Registry:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "blobs").mkdir(exist_ok=True)
        self.connection = sqlite3.connect(self.root / "registry.sqlite", check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)

    def close(self) -> None:
        self.connection.close()

    def publish(
        self, manifest: SkillManifest, artifact: Path, signature: Path | None = None
    ) -> dict[str, Any]:
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        destination = self.root / "blobs" / f"sha256-{digest}.evoskill"
        if not destination.exists():
            shutil.copyfile(artifact, destination)
        signature_json = signature.read_text(encoding="utf-8") if signature else None
        published_at = datetime.now(UTC).isoformat()
        try:
            self.connection.execute(
                """INSERT INTO releases(name,version,digest,description,manifest_json,signature_json,published_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (
                    manifest.name,
                    manifest.version,
                    digest,
                    manifest.description,
                    json.dumps(manifest.as_dict()),
                    signature_json,
                    published_at,
                ),
            )
            self.connection.commit()
        except sqlite3.IntegrityError as exc:
            raise ValueError("Name/version or artifact digest is already published") from exc
        return {
            "name": manifest.name,
            "version": manifest.version,
            "digest": digest,
            "published_at": published_at,
        }

    def search(self, query: str = "", limit: int = 50) -> list[dict[str, Any]]:
        if query.strip():
            terms = " OR ".join(f'"{term}"' for term in query.replace('"', " ").split()[:10])
            rows = self.connection.execute(
                """SELECT r.* FROM release_fts f JOIN releases r ON r.rowid=f.rowid
                   WHERE release_fts MATCH ? ORDER BY bm25(release_fts) LIMIT ?""",
                (terms, limit),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM releases ORDER BY published_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_decode(row) for row in rows]

    def get(self, name: str, version: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM releases WHERE name=? AND version=?", (name, version)
        ).fetchone()
        return _decode(row) if row else None


def _decode(row: sqlite3.Row) -> dict[str, Any]:
    value = dict(row)
    value["manifest"] = json.loads(value.pop("manifest_json"))
    if value.get("signature_json"):
        value["signature"] = json.loads(value.pop("signature_json"))
    else:
        value.pop("signature_json", None)
        value["signature"] = None
    return value
