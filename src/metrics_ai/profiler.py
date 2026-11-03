"""psutil process-tree profiler and pytest-benchmark ingestion."""

from __future__ import annotations

import json
import os
import shlex
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import psutil

POLL_INTERVAL_S = 0.010
MB = 1024 * 1024


def _argv(cmd: str | list[str]) -> list[str]:
    argv = list(cmd) if isinstance(cmd, list) else shlex.split(cmd, posix=os.name != "nt")
    if argv and argv[0].strip("\"'") in {"python", "python3", "py"}:
        # Profile with the interpreter metrics.ai runs in, so project deps resolve.
        argv[0] = sys.executable
    return [a.strip('"') if os.name == "nt" else a for a in argv]


def _tree_rss(proc: psutil.Process) -> int:
    total = 0
    try:
        procs = [proc, *proc.children(recursive=True)]
    except psutil.Error:
        return 0
    for p in procs:
        try:
            total += p.memory_info().rss
        except psutil.Error:
            continue
    return total


def child_env(cwd: Path | None) -> dict[str, str] | None:
    """Put the profiled repo's own `src/` first on PYTHONPATH (src layout), so a checked-out commit is
    measured with its own code even when metrics.ai itself is installed elsewhere."""
    src = (cwd or Path.cwd()) / "src"
    if not src.is_dir():
        return None
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(src), env.get("PYTHONPATH", "")) if p)
    return env


def profile_once(cmd: str | list[str], cwd: Path | None = None, shell: bool = False) -> dict[str, Any]:
    """Run `cmd` once, sampling the whole process tree's RSS every 10 ms."""
    args: Any = cmd if shell else _argv(cmd)
    start = time.perf_counter()
    proc = psutil.Popen(args, cwd=cwd, shell=shell, env=child_env(cwd))
    peak = 0
    samples = 0
    while True:
        peak = max(peak, _tree_rss(proc))
        samples += 1
        try:
            exit_code = proc.wait(timeout=POLL_INTERVAL_S)
            break
        except psutil.TimeoutExpired:
            continue
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    return {"latency_ms": elapsed_ms, "peak_rss_mb": peak / MB, "exit_code": exit_code,
            "rss_samples": samples}


def profile_command(cmd: str, runs: int = 3, cwd: Path | None = None, shell: bool = False) -> dict[str, Any]:
    """Profile `cmd` `runs` times; returns a telemetry_runs-shaped record."""
    results = [profile_once(cmd, cwd=cwd, shell=shell) for _ in range(max(1, runs))]
    latencies = [r["latency_ms"] for r in results]
    exit_code = next((r["exit_code"] for r in results if r["exit_code"] != 0), 0)
    return {
        "command": cmd,
        "mean_latency_ms": statistics.fmean(latencies),
        "peak_memory_mb": max(r["peak_rss_mb"] for r in results),
        "throughput_items_sec": None,
        "exit_code": exit_code,
        "raw_benchmark_json": {
            "mode": "wrap",
            "poll_interval_ms": POLL_INTERVAL_S * 1000,
            "per_run_ms": latencies,
            "per_run_mb": [r["peak_rss_mb"] for r in results],
            "benchmark_iterations": len(latencies),
            "stdev_latency_ms": statistics.stdev(latencies) if len(latencies) > 1 else 0.0,
            "runs": results,
        },
    }


def parse_benchmark_json(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Map a pytest-benchmark JSON artifact to per-benchmark run records."""
    records = []
    for bench in data.get("benchmarks", []):
        stats = bench.get("stats", {})
        per_run_ms = [float(v) * 1000.0 for v in stats.get("data") or []]  # present with --benchmark-save-data
        records.append({
            "command": f"pytest::{bench.get('fullname') or bench.get('name')}",
            "mean_latency_ms": float(stats.get("mean", 0.0)) * 1000.0,
            "throughput_items_sec": float(stats["ops"]) if stats.get("ops") is not None else None,
            "raw_benchmark_json": {
                "mode": "pytest-benchmark",
                "per_run_ms": per_run_ms,
                "per_run_mb": [],  # pytest-benchmark reports one process-wide peak, set below
                "benchmark_iterations": len(per_run_ms) or int(stats.get("rounds") or 0),
                "stddev_ms": float(stats.get("stddev", 0.0)) * 1000.0,
                "rounds": stats.get("rounds"),
                "min_ms": float(stats.get("min", 0.0)) * 1000.0,
                "max_ms": float(stats.get("max", 0.0)) * 1000.0,
            },
        })
    return records


def run_pytest_benchmark(args: list[str], cwd: Path | None = None) -> tuple[list[dict[str, Any]], int]:
    """Execute pytest with `--benchmark-json` under the profiler and parse the artifact."""
    with tempfile.TemporaryDirectory() as tmp:
        artifact = Path(tmp) / "benchmark.json"
        argv = [sys.executable, "-m", "pytest", f"--benchmark-json={artifact}", *args]
        prof = profile_once(argv, cwd=cwd)
        data = json.loads(artifact.read_text(encoding="utf-8")) if artifact.exists() else {}
    records = parse_benchmark_json(data)
    for rec in records:
        rec["peak_memory_mb"] = prof["peak_rss_mb"]
        rec["exit_code"] = prof["exit_code"]
    return records, prof["exit_code"]
