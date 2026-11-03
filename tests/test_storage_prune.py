from __future__ import annotations

import sqlite3

import pytest

from metrics_ai.storage import Ledger


def _ledger(tmp_path) -> Ledger:
    ledger = Ledger(tmp_path / ".metrics" / "ledger.db")
    ledger.init_schema()
    for sha in ("A", "B", "orphan"):
        ledger.upsert_commit({"sha": sha, "message": sha})
    t = 100
    for sha in ("A", "B"):
        for i in range(3):  # three runs of the same command per commit; the newest must survive
            ledger.insert_run({"run_id": f"{sha}-{i}", "commit_sha": sha, "command": "bench",
                               "mean_latency_ms": float(i), "created_at": t + i})
    ledger.insert_run({"run_id": "B-other", "commit_sha": "B", "command": "other", "created_at": t})
    for i, role in enumerate(["backend", "backend", "aiml"]):
        ledger.insert_impact({"event_id": f"imp-{i}", "baseline_sha": "A", "target_sha": "B", "role": role,
                              "format_type": "xyz", "density": "standard", "gate_mode": "llm_judge",
                              "output_text": str(i), "created_at": t + i})
    return ledger


def test_keep_latest_drops_superseded_rows_and_orphan_commits(tmp_path):
    ledger = _ledger(tmp_path)
    deleted = ledger.prune_ledger("keep_latest")
    assert deleted == {"telemetry_runs": 4, "synthesized_impacts": 1, "commits": 1}
    assert sorted(r["run_id"] for r in ledger.dump()["telemetry_runs"]) == ["A-2", "B-2", "B-other"]
    impacts = ledger.dump()["synthesized_impacts"]
    assert sorted(i["event_id"] for i in impacts) == ["imp-1", "imp-2"]
    assert sorted(c["sha"] for c in ledger.dump()["commits"]) == ["A", "B"]
    assert ledger.prune_ledger("keep_latest") == {"telemetry_runs": 0, "synthesized_impacts": 0, "commits": 0}


def test_keep_latest_breaks_created_at_ties_by_insertion_order(tmp_path):
    ledger = Ledger(tmp_path / "l.db")
    ledger.init_schema()
    ledger.upsert_commit({"sha": "A"})
    for rid in ("first", "second"):
        ledger.insert_run({"run_id": rid, "commit_sha": "A", "command": "c", "created_at": 5})
    ledger.prune_ledger("keep_latest")
    assert [r["run_id"] for r in ledger.runs_for("A")] == ["second"]


def test_prune_all_truncates_every_table(tmp_path):
    ledger = _ledger(tmp_path)
    deleted = ledger.prune_ledger("all")
    assert deleted == {"synthesized_impacts": 3, "telemetry_runs": 7, "commits": 3}
    assert ledger.counts() == {"commits": 0, "telemetry_runs": 0, "synthesized_impacts": 0}


def test_prune_rejects_unknown_mode(tmp_path):
    with pytest.raises(ValueError):
        _ledger(tmp_path).prune_ledger("everything")


def test_gate_mode_column_migrates_legacy_ledger(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE synthesized_impacts (event_id TEXT PRIMARY KEY, role TEXT, created_at INTEGER)")
    conn.commit()
    conn.close()
    ledger = Ledger(path)
    ledger.init_schema()
    with ledger.connect() as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(synthesized_impacts)")}
    assert {"gate_mode", "natural_prob"} <= cols
