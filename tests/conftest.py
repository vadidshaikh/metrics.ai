from __future__ import annotations

import textwrap
from pathlib import Path

import git
import pytest

BASELINE_SRC = textwrap.dedent('''
    import sqlite3


    def ingest(conn, rows):
        for row in rows:
            for value in row:
                conn.execute("INSERT INTO t VALUES (?)", (value,))
                conn.commit()


    def parse(path):
        return open(path).read()
''')

TARGET_SRC = textwrap.dedent('''
    import sqlite3
    from functools import lru_cache


    def ingest(conn, rows):
        flat = [(value,) for row in rows for value in row]
        with conn:
            conn.executemany("INSERT INTO t VALUES (?)", flat)


    @lru_cache(maxsize=None)
    def parse(path):
        return open(path).read()
''')


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    from metrics_ai.config import DEFAULT_CONFIG

    for var in ("GROQ_API_KEY", "HF_TOKEN", "JEV_API_KEY", "TYPESAFE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    # Never let a test reach a real local Ollama: port 9 (discard) refuses connections immediately.
    monkeypatch.setitem(DEFAULT_CONFIG["providers"], "ollama_url", "http://127.0.0.1:9")


@pytest.fixture
def repo(tmp_path: Path) -> git.Repo:
    """A two-commit repo: sequential baseline -> batched/cached target."""
    r = git.Repo.init(tmp_path)
    with r.config_writer() as cw:
        cw.set_value("user", "name", "Test Author")
        cw.set_value("user", "email", "test@example.com")
    (tmp_path / "ingest.py").write_text(BASELINE_SRC, encoding="utf-8")
    r.index.add(["ingest.py"])
    r.index.commit("perf(baseline): sequential ingest")
    (tmp_path / "ingest.py").write_text(TARGET_SRC, encoding="utf-8")
    r.index.add(["ingest.py"])
    r.index.commit("perf: batch executemany + lru_cache")
    yield r
    r.close()


def make_state(latency=(1000.0, 250.0), memory=(50.0, 40.0), signals=("batched_operations_added",),
               extra_diff=None):
    from metrics_ai.collector import compute_deltas

    runs_b = [{"mean_latency_ms": latency[0], "peak_memory_mb": memory[0], "exit_code": 0}]
    runs_t = [{"mean_latency_ms": latency[1], "peak_memory_mb": memory[1], "exit_code": 0}]
    diff = {"signals": list(signals), "batching_calls_added": ["executemany"],
            "mechanisms": ["batched database writes into one transaction via executemany()"]} if signals else {}
    diff.update(extra_diff or {})
    return {
        "baseline_sha": "a" * 40, "target_sha": "b" * 40, "command": "python bench.py",
        "files_modified": ["src/storage.py"], "ast_structural_diff": diff,
        "complexity_delta": {"cyclomatic_before": 3.0, "cyclomatic_after": 2.0, "loc_delta": -4,
                             "complexity_delta_pct": -33.3},
        "telemetry_delta": compute_deltas(runs_b, runs_t, 3.0, 2.0),
    }
