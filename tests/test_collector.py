from __future__ import annotations

import sys

from metrics_ai import collector, profiler
from metrics_ai.ast_diff import diff_profiles, identifiers, mechanisms, profile_source, structural_signals
from tests.conftest import BASELINE_SRC, TARGET_SRC, make_state


def test_profile_detects_nested_loops_and_calls_in_loops():
    p = profile_source(BASELINE_SRC)
    assert p.loops == 2 and p.nested_loops == 1 and p.max_loop_depth == 2
    assert p.calls_in_loops["commit"] == 1
    assert {"ingest", "parse"} <= p.functions


def test_diff_signals_batching_caching_and_loop_removal():
    diff = diff_profiles(profile_source(BASELINE_SRC), profile_source(TARGET_SRC))
    assert diff["batching_calls_added"] == ["executemany"]
    assert diff["caching_decorators_added"] == ["lru_cache"]
    assert "commit" in diff["calls_hoisted_out_of_loops"]
    signals = structural_signals(diff)
    assert {"batched_operations_added", "caching_decorator_added", "calls_hoisted_out_of_loops"} <= set(signals)


def test_profile_handles_syntax_errors_and_empty():
    assert profile_source("def broken(:\n").parse_error
    assert profile_source(None).functions == set()


def test_commit_record_from_real_repo(repo):
    record = collector.commit_record(repo, "HEAD")
    assert record["parent_sha"] == repo.commit("HEAD~1").hexsha
    assert record["files_modified_json"] == ["ingest.py"]
    assert "batched_operations_added" in record["ast_diff_json"]["signals"]
    assert record["ast_diff_json"]["total"]["caching_decorators_added"] == ["lru_cache"]
    assert record["cyclomatic_before"] > 0 and record["cyclomatic_after"] > 0
    assert isinstance(record["loc_delta"], int)


def test_commit_record_root_commit(repo):
    root = collector.commit_record(repo, "HEAD~1")
    assert root["parent_sha"] is None and root["files_modified_json"] == ["ingest.py"]


def test_pct_delta_and_ratio_rounding():
    assert collector.pct_delta(1234.0, 216.0) == -82.5
    assert collector.pct_delta(0, 5) is None and collector.pct_delta(None, 5) is None
    assert collector.ratio(1234.0, 216.0) == 5.7
    assert collector.ratio(10.0, 0) is None


def test_compute_deltas_and_allowed_numbers():
    state = make_state(latency=(1234.56, 216.04), memory=(48.26, 40.0))
    d = state["telemetry_delta"]
    assert d["baseline"]["mean_latency_ms"] == 1234.6
    assert d["latency_delta_pct"] == -82.5
    assert d["speedup_multiplier_x"] == 5.7
    allowed = collector.allowed_numbers(state)
    assert {1234.6, 216.0, 82.5, -82.5, 5.7, 48.3, 40.0, 3.0, 2.0, 4.0} <= allowed
    assert 83.0 not in allowed


def test_build_state_shapes_compact_diff(repo):
    record = collector.commit_record(repo, "HEAD")
    runs = [{"mean_latency_ms": 10.0, "peak_memory_mb": 5.0, "exit_code": 0}]
    state = collector.build_state(record, runs, runs, "cmd")
    assert state["telemetry_delta"]["latency_delta_pct"] == 0.0
    assert "files" not in state["ast_structural_diff"]
    assert state["ast_structural_diff"]["signals"]


def test_failed_runs_excluded_when_successes_exist():
    d = collector.compute_deltas(
        [{"mean_latency_ms": 100.0, "exit_code": 0}, {"mean_latency_ms": 9999.0, "exit_code": 1}],
        [{"mean_latency_ms": 50.0, "exit_code": 0}], 1.0, 1.0)
    assert d["latency_delta_pct"] == -50.0


def test_profile_command_measures_real_process():
    cmd = f'"{sys.executable}" -c "x = bytearray(30 * 1024 * 1024); import time; time.sleep(0.2)"'
    run = profiler.profile_command(cmd, runs=2)
    assert run["exit_code"] == 0
    assert run["mean_latency_ms"] >= 200.0
    assert run["peak_memory_mb"] >= 30.0
    assert len(run["raw_benchmark_json"]["runs"]) == 2


def test_profile_command_reports_exit_code():
    run = profiler.profile_command('python -c "raise SystemExit(3)"', runs=1)
    assert run["exit_code"] == 3


def test_parse_benchmark_json():
    data = {"benchmarks": [{"fullname": "tests/test_x.py::test_a",
                            "stats": {"mean": 0.002, "stddev": 0.0001, "ops": 500.0, "rounds": 10,
                                      "min": 0.0019, "max": 0.0025}}]}
    [rec] = profiler.parse_benchmark_json(data)
    assert rec["command"] == "pytest::tests/test_x.py::test_a"
    assert rec["mean_latency_ms"] == 2.0 and rec["throughput_items_sec"] == 500.0
    assert rec["raw_benchmark_json"]["stddev_ms"] == 0.1


def test_mechanisms_are_distinct_items():
    diff = diff_profiles(profile_source(BASELINE_SRC), profile_source(TARGET_SRC))
    items = mechanisms({**diff, "decorators_added": ["ingest.py::parse@lru_cache"]})
    assert items[0] == "batched database writes into one transaction via executemany()"
    assert "cached repeated parse() results in memory with @lru_cache" in items
    assert any(i.startswith("moved per-iteration") and "commit()" in i for i in items)
    assert len(items) == len(set(items)) >= 3


def test_build_state_exposes_mechanisms_and_identifiers(repo):
    record = collector.commit_record(repo, "HEAD")
    runs = [{"mean_latency_ms": 10.0, "peak_memory_mb": 5.0, "exit_code": 0}]
    state = collector.build_state(record, runs, runs, "cmd")
    assert any("executemany()" in m for m in state["ast_structural_diff"]["mechanisms"])
    idents = collector.state_identifiers(state)
    assert {"executemany", "lru_cache", "ingest.py"} <= idents
    assert identifiers({}, ["a/b/long_name.py"]) == {"long_name.py", "long_name"}
