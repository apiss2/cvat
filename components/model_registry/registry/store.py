# SPDX-License-Identifier: MIT
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Conflict(RuntimeError):
    pass


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS models(
                  id TEXT PRIMARY KEY, owner TEXT NOT NULL, active_revision TEXT,
                  deleted INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS revisions(
                  model_id TEXT NOT NULL, revision TEXT NOT NULL, manifest TEXT NOT NULL,
                  digest TEXT NOT NULL, path TEXT NOT NULL, created_at TEXT NOT NULL,
                  PRIMARY KEY(model_id, revision));
                CREATE TABLE IF NOT EXISTS operations(
                  id TEXT PRIMARY KEY, model_id TEXT NOT NULL, revision TEXT NOT NULL,
                  owner TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(
                  seq INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT NOT NULL,
                  model_id TEXT NOT NULL, revision TEXT NOT NULL, request_id TEXT NOT NULL,
                  actor TEXT NOT NULL, stage TEXT NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS events_model_seq ON events(model_id, seq);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def recover(self) -> int:
        with self.connect() as db:
            # A crash can occur after the revision commit but before operation status is saved.
            completed = db.execute("""UPDATE operations SET status='succeeded',
                detail='Revision was committed before the manager restarted', updated_at=?
                WHERE status IN ('queued','validating') AND EXISTS(
                    SELECT 1 FROM revisions r WHERE r.model_id=operations.model_id AND r.revision=operations.revision)
                """, (now(),)).rowcount
            interrupted = db.execute("UPDATE operations SET status='failed', detail='Manager restarted before commit; retry the upload', updated_at=? WHERE status IN ('queued','validating')", (now(),)).rowcount
            return completed + interrupted

    def model(self, model_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM models WHERE id=?", (model_id,)).fetchone()
        if row is None:
            raise KeyError(model_id)
        return dict(row)

    def models(self) -> list[dict]:
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM models ORDER BY created_at DESC")]

    def create_model(self, model_id: str, owner: str) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO models(id,owner,created_at) VALUES(?,?,?)", (model_id, owner, now()))

    def revision(self, model_id: str, revision: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM revisions WHERE model_id=? AND revision=?", (model_id, revision)).fetchone()
        if row is None:
            raise KeyError(revision)
        result = dict(row)
        result["manifest"] = json.loads(result["manifest"])
        return result

    def revisions(self, model_id: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM revisions WHERE model_id=? ORDER BY created_at DESC", (model_id,)).fetchall()
        return [{**dict(r), "manifest": json.loads(r["manifest"])} for r in rows]

    def commit_revision(self, model_id: str, revision: str, expected: str | None, manifest: dict, digest: str, path: str) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM models WHERE id=?", (model_id,)).fetchone()
            if row is None or row["deleted"] or row["active_revision"] != expected:
                raise Conflict("Model was deleted or changed while this revision was being tested; upload again")
            db.execute("INSERT INTO revisions VALUES(?,?,?,?,?,?)", (model_id, revision, json.dumps(manifest, ensure_ascii=False), digest, path, now()))
            db.execute("UPDATE models SET active_revision=? WHERE id=?", (revision, model_id))

    def activate_existing(self, model_id: str, revision: str, expected: str) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM revisions WHERE model_id=? AND revision=?", (model_id, revision)).fetchone():
                raise KeyError(revision)
            cur = db.execute("UPDATE models SET active_revision=? WHERE id=? AND active_revision=? AND deleted=0", (revision, model_id, expected))
            if cur.rowcount != 1:
                raise Conflict("Model changed; refresh before rolling back")

    def delete(self, model_id: str, expected: str | None) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM models WHERE id=?", (model_id,)).fetchone()
            if row is None or row["active_revision"] != expected:
                raise Conflict("Model changed; refresh before deletion")
            db.execute("UPDATE models SET deleted=1 WHERE id=?", (model_id,))

    def new_operation(self, op: str, model_id: str, revision: str, owner: str) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO operations VALUES(?,?,?,?,?,?,?,?)", (op, model_id, revision, owner, "queued", "", now(), now()))

    def operation(self, op: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM operations WHERE id=?", (op,)).fetchone()
        if row is None:
            raise KeyError(op)
        return dict(row)

    def update_operation(self, op: str, status: str, detail: str = "") -> None:
        with self.connect() as db:
            db.execute("UPDATE operations SET status=?,detail=?,updated_at=? WHERE id=?", (status, detail[:16000], now(), op))

    def operations(self, model_id: str) -> list[dict]:
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM operations WHERE model_id=? ORDER BY created_at DESC LIMIT 30", (model_id,))]

    def event(self, model_id: str, revision: str, request_id: str, actor: str, stage: str, level: str, message: str) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO events(time,model_id,revision,request_id,actor,stage,level,message) VALUES(?,?,?,?,?,?,?,?)", (now(), model_id, revision, request_id, actor, stage, level, message[:16000]))
            # Bounded global log retention. Requests never store image bytes by default.
            db.execute("DELETE FROM events WHERE seq <= (SELECT COALESCE(MAX(seq),0)-10000 FROM events)")

    def events(self, model_id: str, after: int = 0) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM (SELECT * FROM events WHERE model_id=? AND seq>? ORDER BY seq DESC LIMIT 200) ORDER BY seq", (model_id, after)).fetchall()
        return [dict(r) for r in rows]
