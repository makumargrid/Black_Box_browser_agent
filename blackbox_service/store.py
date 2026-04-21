from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from blackbox_service.models import EventEnvelope, RunRecord, TabState


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(raw: str) -> datetime:
    return datetime.fromisoformat(raw)


class SQLiteEventStore:
    def __init__(self, db_path: str | Path = "blackbox_events.db") -> None:
        self._db_path = str(db_path)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                targets_json TEXT NOT NULL,
                options_json TEXT NOT NULL,
                active_tab_id TEXT,
                error TEXT
            );

            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                run_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                type TEXT NOT NULL,
                tab_id TEXT,
                step_id TEXT,
                payload_json TEXT NOT NULL,
                token_cost REAL
            );

            CREATE INDEX IF NOT EXISTS idx_events_run_id_id ON events(run_id, id);
            CREATE INDEX IF NOT EXISTS idx_events_run_id_type ON events(run_id, type);

            CREATE TABLE IF NOT EXISTS tabs (
                run_id TEXT NOT NULL,
                tab_id TEXT NOT NULL,
                url TEXT NOT NULL,
                title TEXT NOT NULL,
                parent_tab_id TEXT,
                correlation_id TEXT,
                is_active INTEGER NOT NULL,
                opened_at TEXT NOT NULL,
                PRIMARY KEY(run_id, tab_id)
            );
            """
        )
        self._conn.commit()

    def create_run(self, targets: list[str], options: dict[str, Any]) -> RunRecord:
        run = RunRecord(
            run_id=f"run-{uuid.uuid4().hex[:12]}",
            status="running",
            targets=targets,
            options=options,
        )
        self._conn.execute(
            """
            INSERT INTO runs (run_id, status, created_at, updated_at, targets_json, options_json, active_tab_id, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run.run_id,
                run.status,
                run.created_at.isoformat(),
                run.updated_at.isoformat(),
                json.dumps(run.targets),
                json.dumps(run.options),
                run.active_tab_id,
                run.error,
            ),
        )
        self._conn.commit()
        return run

    def get_run(self, run_id: str) -> RunRecord | None:
        row = self._conn.execute(
            "SELECT run_id, status, created_at, updated_at, targets_json, options_json, active_tab_id, error FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return RunRecord(
            run_id=row["run_id"],
            status=row["status"],
            created_at=_parse_dt(row["created_at"]),
            updated_at=_parse_dt(row["updated_at"]),
            targets=json.loads(row["targets_json"]),
            options=json.loads(row["options_json"]),
            active_tab_id=row["active_tab_id"],
            error=row["error"],
        )

    def set_run_status(self, run_id: str, status: str, error: str | None = None) -> None:
        self._conn.execute(
            "UPDATE runs SET status = ?, updated_at = ?, error = ? WHERE run_id = ?",
            (status, _utc_now_iso(), error, run_id),
        )
        self._conn.commit()

    def set_active_tab(self, run_id: str, tab_id: str | None) -> None:
        self._conn.execute(
            "UPDATE runs SET active_tab_id = ?, updated_at = ? WHERE run_id = ?",
            (tab_id, _utc_now_iso(), run_id),
        )
        self._conn.commit()

    def append_event(self, event: EventEnvelope) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO events
            (event_id, run_id, ts, type, tab_id, step_id, payload_json, token_cost)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.run_id,
                event.ts.isoformat(),
                event.type,
                event.tab_id,
                event.step_id,
                json.dumps(event.payload),
                event.token_cost,
            ),
        )
        self._conn.commit()

    def list_events(self, run_id: str, limit: int = 500) -> list[EventEnvelope]:
        rows = self._conn.execute(
            """
            SELECT event_id, run_id, ts, type, tab_id, step_id, payload_json, token_cost
            FROM events
            WHERE run_id = ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (run_id, limit),
        ).fetchall()
        return [
            EventEnvelope(
                event_id=row["event_id"],
                run_id=row["run_id"],
                ts=_parse_dt(row["ts"]),
                type=row["type"],
                tab_id=row["tab_id"],
                step_id=row["step_id"],
                payload=json.loads(row["payload_json"]),
                token_cost=row["token_cost"],
            )
            for row in rows
        ]

    def upsert_tab(self, tab: TabState) -> None:
        self._conn.execute(
            """
            INSERT INTO tabs (run_id, tab_id, url, title, parent_tab_id, correlation_id, is_active, opened_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, tab_id) DO UPDATE SET
                url = excluded.url,
                title = excluded.title,
                parent_tab_id = excluded.parent_tab_id,
                correlation_id = excluded.correlation_id,
                is_active = excluded.is_active
            """,
            (
                tab.run_id,
                tab.tab_id,
                tab.url,
                tab.title,
                tab.parent_tab_id,
                tab.correlation_id,
                1 if tab.is_active else 0,
                tab.opened_at.isoformat(),
            ),
        )
        self._conn.commit()

    def list_tabs(self, run_id: str) -> list[TabState]:
        rows = self._conn.execute(
            """
            SELECT run_id, tab_id, url, title, parent_tab_id, correlation_id, is_active, opened_at
            FROM tabs
            WHERE run_id = ?
            ORDER BY opened_at ASC
            """,
            (run_id,),
        ).fetchall()
        return [
            TabState(
                run_id=row["run_id"],
                tab_id=row["tab_id"],
                url=row["url"],
                title=row["title"],
                parent_tab_id=row["parent_tab_id"],
                correlation_id=row["correlation_id"],
                is_active=bool(row["is_active"]),
                opened_at=_parse_dt(row["opened_at"]),
            )
            for row in rows
        ]

