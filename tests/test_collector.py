from __future__ import annotations

import os
import sys

import pytest

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


class FakeProc:
    """Deterministic stand-in for psutil.Popen: fixed RSS trace, exits after N polls."""

    def __init__(self, rss_mb, exit_code=0, children_mb=()):
        self.rss_mb, self.exit_code, self.polls = list(rss_mb), exit_code, 0
        self.kids = [FakeChild(mb) for mb in children_mb]

    def children(self, recursive=True):
        return self.kids

    def memory_info(self):
        return type("Mem", (), {"rss": int(self.rss_mb[min(self.polls, len(self.rss_mb) - 1)] * profiler.MB)})()

    def wait(self, timeout=None):
        self.polls += 1
        if self.polls < len(self.rss_mb):
            raise profiler.psutil.TimeoutExpired(timeout)
        return self.exit_code


class FakeChild:
    def __init__(self, mb):
        self.mb = mb

    def memory_info(self):
        return type("Mem", (), {"rss": int(self.mb * profiler.MB)})()


def _fake_profiler(monkeypatch, procs, clock):
    procs, ticks, seen = list(procs), iter(clock), {}

    def popen(args, **kwargs):
        seen.update(kwargs)
        return procs.pop(0)
    monkeypatch.setattr(profiler.psutil, "Popen", popen)
    monkeypatch.setattr(profiler.time, "perf_counter", lambda: next(ticks))
    return seen


def test_profile_command_is_exact_with_mocked_psutil(monkeypatch, tmp_path):
    (tmp_path / "src").mkdir()
    seen = _fake_profiler(monkeypatch, [FakeProc([10, 30, 20], children_mb=[2]), FakeProc([5, 40])],
                          clock=[0.0, 0.100, 1.0, 1.300])
    run = profiler.profile_command("python bench.py", runs=2, cwd=tmp_path)
    raw = run["raw_benchmark_json"]
    assert raw["per_run_ms"] == pytest.approx([100.0, 300.0]) and run["mean_latency_ms"] == pytest.approx(200.0)
    assert raw["per_run_mb"] == pytest.approx([32.0, 40.0]) and run["peak_memory_mb"] == pytest.approx(40.0)
    assert raw["benchmark_iterations"] == 2 and run["exit_code"] == 0
    # the profiled repo's own src/ is put first on the child's PYTHONPATH
    assert seen["env"]["PYTHONPATH"].split(os.pathsep)[0] == str(tmp_path / "src")


def test_profile_command_reports_first_failing_exit_code(monkeypatch):
    _fake_profiler(monkeypatch, [FakeProc([1]), FakeProc([1], exit_code=3)], clock=[0.0, 0.01, 1.0, 1.01])
    assert profiler.profile_command("python x.py", runs=2)["exit_code"] == 3


def test_profile_command_real_process_smoke():
    # Loose bounds only: real timings and RSS depend on machine load.
    run = profiler.profile_command(f'"{sys.executable}" -c "raise SystemExit(0)"', runs=1)
    assert run["exit_code"] == 0 and run["mean_latency_ms"] > 0 and run["peak_memory_mb"] >= 0
    assert run["raw_benchmark_json"]["benchmark_iterations"] == 1


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


def test_iterations_replace_samples_and_are_citable_only_above_one():
    raw3 = {"per_run_ms": [10.0, 11.0, 12.0], "per_run_mb": [5.0, 5.0, 5.0], "benchmark_iterations": 3}
    legacy = '{"runs": [{"latency_ms": 1}, {"latency_ms": 2}, {"latency_ms": 3}]}'
    runs3 = [{"mean_latency_ms": 11.0, "peak_memory_mb": 5.0, "exit_code": 0, "raw_benchmark_json": raw3}]
    d = collector.compute_deltas(runs3, [{"mean_latency_ms": 5.0, "exit_code": 0, "raw_benchmark_json": legacy}],
                                 2.0, 1.0)
    assert "samples" not in d["baseline"] and d["baseline"]["benchmark_iterations"] == 3
    assert d["target"]["benchmark_iterations"] == 3  # derived from a legacy `runs` list
    state = make_state()
    assert 3.0 in collector.allowed_numbers(state)
    for side in ("baseline", "target"):
        state["telemetry_delta"][side]["benchmark_iterations"] = 1
    # With a single iteration, "1" must not become citable (no other value in this state equals 1).
    assert 1.0 not in collector.allowed_numbers({**state, "complexity_delta": {"loc_delta": 0}})


def test_metric_manifest_lists_unmeasured_throughput():
    state = make_state()
    assert state["unmeasured_metrics"] == ["throughput_items_sec"]
    assert {"mean_latency_ms", "peak_memory_mb", "cyclomatic_complexity", "speedup_multiplier_x"} == \
        set(state["measured_metrics"])
