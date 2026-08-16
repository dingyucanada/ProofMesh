from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from .timeutil import utc_now


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class EvidenceLedger:
    """Append-only hash-chained evidence ledger backed by SQLite."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT,
                    run_id TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    prev_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL UNIQUE
                )
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(events)").fetchall()}
            if "event_id" not in columns:
                conn.execute("ALTER TABLE events ADD COLUMN event_id TEXT")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_events_event_id ON events(event_id) WHERE event_id IS NOT NULL"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, seq)")

    def append(
        self,
        run_id: str,
        agent: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if event_id is not None:
                existing = conn.execute("SELECT * FROM events WHERE event_id = ?", (event_id,)).fetchone()
                if existing is not None:
                    conn.commit()
                    return self._row_to_event(existing)
            previous = conn.execute(
                "SELECT event_hash FROM events WHERE run_id = ? ORDER BY seq DESC LIMIT 1", (run_id,)
            ).fetchone()
            prev_hash = previous["event_hash"] if previous else "GENESIS"
            envelope = {
                "run_id": run_id,
                "ts": utc_now(),
                "agent": agent,
                "event_type": event_type,
                "payload": payload,
                "prev_hash": prev_hash,
            }
            event_hash = hashlib.sha256(canonical(envelope).encode("utf-8")).hexdigest()
            cursor = conn.execute(
                """INSERT INTO events(event_id, run_id, ts, agent, event_type, payload_json, prev_hash, event_hash)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_id,
                    run_id,
                    envelope["ts"],
                    agent,
                    event_type,
                    canonical(payload),
                    prev_hash,
                    event_hash,
                ),
            )
            conn.commit()
            return {"seq": cursor.lastrowid, "event_id": event_id, **envelope, "event_hash": event_hash}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def events(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM events WHERE run_id = ? ORDER BY seq", (run_id,)).fetchall()
        return [self._row_to_event(row) for row in rows]

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "seq": row["seq"],
            "event_id": row["event_id"] if "event_id" in row.keys() else None,
            "run_id": row["run_id"],
            "ts": row["ts"],
            "agent": row["agent"],
            "event_type": row["event_type"],
            "payload": json.loads(row["payload_json"]),
            "prev_hash": row["prev_hash"],
            "event_hash": row["event_hash"],
        }

    def verify(self, run_id: str) -> tuple[bool, str]:
        previous = "GENESIS"
        for event in self.events(run_id):
            envelope = {
                "run_id": event["run_id"],
                "ts": event["ts"],
                "agent": event["agent"],
                "event_type": event["event_type"],
                "payload": event["payload"],
                "prev_hash": event["prev_hash"],
            }
            calculated = hashlib.sha256(canonical(envelope).encode("utf-8")).hexdigest()
            if event["prev_hash"] != previous or event["event_hash"] != calculated:
                return False, event["event_hash"]
            previous = event["event_hash"]
        return True, previous
