"""SQLite3 connection manager, schema and query layer for `.metrics/ledger.db`."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS commits (
    sha                 TEXT PRIMARY KEY,
    parent_sha          TEXT,
    author              TEXT,
    timestamp           INTEGER,
    message             TEXT,
    files_modified_json TEXT,
    ast_diff_json       TEXT,
    cyclomatic_before   REAL,
    cyclomatic_after    REAL,
    loc_delta           INTEGER
);
CREATE TABLE IF NOT EXISTS telemetry_runs (
    run_id               TEXT PRIMARY KEY,
    commit_sha           TEXT NOT NULL REFERENCES commits(sha),
    command              TEXT NOT NULL,
    mean_latency_ms      REAL,
    peak_memory_mb       REAL,
    throughput_items_sec REAL,
    exit_code            INTEGER,
    raw_benchmark_json   TEXT,
    created_at           INTEGER
);
CREATE TABLE IF NOT EXISTS synthesized_impacts (
    event_id          TEXT PRIMARY KEY,
    baseline_sha      TEXT,
    target_sha        TEXT,
    role              TEXT,
    format_type       TEXT,
    density           TEXT,
    jev_worth_prob    REAL,
    jev_impact_score  REAL,
    jev_role_prob     REAL,
    jev_faithful_prob REAL,
    provider_used     TEXT,
    model_used        TEXT,
    output_text       TEXT,
    structured_json   TEXT,
    ledger_hash       TEXT,
    created_at        INTEGER
);
CREATE INDEX IF NOT EXISTS idx_runs_commit ON telemetry_runs(commit_sha, command);
"""

RUN_COLUMNS = (
    "run_id", "commit_sha", "command", "mean_latency_ms", "peak_memory_mb",
    "throughput_items_sec", "exit_code", "raw_benchmark_json", "created_at",
)
IMPACT_COLUMNS = (
    "event_id", "baseline_sha", "target_sha", "role", "format_type", "density",
    "jev_worth_prob", "jev_impact_score", "jev_role_prob", "jev_faithful_prob",
    "provider_used", "model_used", "output_text", "structured_json", "ledger_hash", "created_at",
)


class Ledger:
    """Thin query layer over the local SQLite ledger."""

    def __init__(self, path: Path | str):
        self.path = Path(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    # -- commits ---------------------------------------------------------
    def upsert_commit(self, record: dict[str, Any]) -> None:
        row = dict(record)
        for key in ("files_modified_json", "ast_diff_json"):
            if not isinstance(row.get(key), str):
                row[key] = json.dumps(row.get(key) or {}, sort_keys=True)
        cols = ("sha", "parent_sha", "author", "timestamp", "message", "files_modified_json",
                "ast_diff_json", "cyclomatic_before", "cyclomatic_after", "loc_delta")
        values = [row.get(c) for c in cols]
        updates = ", ".join(f"{c}=COALESCE(excluded.{c}, {c})" for c in cols[1:])
        with self.connect() as conn:
            conn.execute(
                f"INSERT INTO commits ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
                f"ON CONFLICT(sha) DO UPDATE SET {updates}",
                values,
            )

    def get_commit(self, sha: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM commits WHERE sha = ?", (sha,)).fetchone()
        return dict(row) if row else None

    # -- telemetry runs --------------------------------------------------
    @staticmethod
    def _run_row(run: dict[str, Any]) -> tuple[Any, ...]:
        row = dict(run)
        row.setdefault("run_id", uuid.uuid4().hex)
        row.setdefault("created_at", int(time.time()))
        if not isinstance(row.get("raw_benchmark_json"), str):
            row["raw_benchmark_json"] = json.dumps(row.get("raw_benchmark_json") or {})
        return tuple(row.get(c) for c in RUN_COLUMNS)

    def insert_run(self, run: dict[str, Any]) -> str:
        row = self._run_row(run)
        with self.connect() as conn:
            conn.execute(
                f"INSERT INTO telemetry_runs ({', '.join(RUN_COLUMNS)}) "
                f"VALUES ({', '.join('?' * len(RUN_COLUMNS))})",
                row,
            )
        return row[0]

    def insert_runs(self, runs: list[dict[str, Any]]) -> list[str]:
        """Batch insert: one connection, one transaction, one `executemany()`."""
        rows = [self._run_row(run) for run in runs]
        with self.connect() as conn:
            conn.executemany(
                f"INSERT INTO telemetry_runs ({', '.join(RUN_COLUMNS)}) "
                f"VALUES ({', '.join('?' * len(RUN_COLUMNS))})",
                rows,
            )
        return [row[0] for row in rows]

    def runs_for(self, commit_sha: str, command: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM telemetry_runs WHERE commit_sha = ?"
        params: list[Any] = [commit_sha]
        if command is not None:
            sql += " AND command = ?"
            params.append(command)
        with self.connect() as conn:
            rows = conn.execute(sql + " ORDER BY created_at, rowid", params).fetchall()
        return [dict(r) for r in rows]

    # -- synthesized impacts --------------------------------------------
    def insert_impact(self, impact: dict[str, Any]) -> str:
        row = dict(impact)
        row.setdefault("event_id", uuid.uuid4().hex)
        row.setdefault("created_at", int(time.time()))
        if not isinstance(row.get("structured_json"), str):
            row["structured_json"] = json.dumps(row.get("structured_json") or {}, sort_keys=True)
        with self.connect() as conn:
            conn.execute(
                f"INSERT INTO synthesized_impacts ({', '.join(IMPACT_COLUMNS)}) "
                f"VALUES ({', '.join('?' * len(IMPACT_COLUMNS))})",
                [row.get(c) for c in IMPACT_COLUMNS],
            )
        return row["event_id"]

    def dump(self) -> dict[str, list[dict[str, Any]]]:
        with self.connect() as conn:
            return {
                table: [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                for table in ("commits", "telemetry_runs", "synthesized_impacts")
            }
