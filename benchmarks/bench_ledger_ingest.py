"""Dogfood benchmark: ingest 500 telemetry records and AST-profile a real source file.

Baseline (intentionally unoptimized): every record re-parses the sample file's AST and
is written with its own SQLite connection + commit.
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

from metrics_ai.ast_diff import profile_source
from metrics_ai.storage import Ledger

RECORDS = 500
SAMPLE = Path(__file__).resolve().parents[1] / "src" / "metrics_ai" / "collector.py"


def run(db_path: Path) -> int:
    ledger = Ledger(db_path)
    ledger.init_schema()
    ledger.upsert_commit({"sha": "bench", "message": "benchmark fixture"})
    source = SAMPLE.read_text(encoding="utf-8")
    for i in range(RECORDS):
        profile = profile_source(source)  # re-parses the full AST for every record
        ledger.insert_run({
            "commit_sha": "bench",
            "command": f"bench::{i}",
            "mean_latency_ms": float(len(profile.functions)),
            "peak_memory_mb": float(profile.loops),
            "exit_code": 0,
            "raw_benchmark_json": {"calls": sum(profile.calls.values())},
        })
    return len(ledger.runs_for("bench"))


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        start = time.perf_counter()
        count = run(Path(tmp) / "bench.db")
        elapsed = (time.perf_counter() - start) * 1000.0
    print(f"ingested {count} records in {elapsed:.1f} ms")
    return 0 if count == RECORDS else 1


if __name__ == "__main__":
    sys.exit(main())
