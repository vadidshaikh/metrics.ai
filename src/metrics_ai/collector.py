"""Git commit inspection, AST/complexity capture and pre-computed delta arithmetic.

All percentages and ratios are computed here in Python (never by Jev or an LLM),
rounded to 1 decimal place, and every number is registered in `allowed_numbers`.
"""

from __future__ import annotations

import statistics
from pathlib import Path
from typing import Any

import git
from radon.complexity import cc_visit
from radon.raw import analyze

from metrics_ai.ast_diff import diff_profiles, merge_diffs, profile_source, structural_signals


def open_repo(root: Path) -> git.Repo:
    return git.Repo(root, search_parent_directories=True)


def resolve_sha(repo: git.Repo, rev: str) -> str:
    return repo.commit(rev).hexsha


def _blob_source(commit: git.Commit, path: str) -> str | None:
    try:
        return commit.tree[path].data_stream.read().decode("utf-8", errors="replace")
    except KeyError:
        return None


def _complexities(source: str | None) -> list[float]:
    if not source:
        return []
    try:
        return [float(block.complexity) for block in cc_visit(source)]
    except SyntaxError:
        return []


def _sloc(source: str | None) -> int:
    if not source:
        return 0
    try:
        return analyze(source).sloc
    except SyntaxError:
        return len([ln for ln in source.splitlines() if ln.strip()])


def commit_record(repo: git.Repo, target: str, baseline: str | None = None) -> dict[str, Any]:
    """Build a `commits` row for `target`, diffed against `baseline` (default: its parent)."""
    tgt = repo.commit(target)
    base = repo.commit(baseline) if baseline else (tgt.parents[0] if tgt.parents else None)
    files = sorted({d.b_path or d.a_path for d in (base.diff(tgt) if base else [])}) if base else \
        sorted(str(b.path) for b in tgt.tree.traverse() if b.type == "blob")
    per_file: dict[str, dict[str, Any]] = {}
    cc_before: list[float] = []
    cc_after: list[float] = []
    loc_delta = 0
    for path in files:
        if not path.endswith(".py"):
            continue
        src_before = _blob_source(base, path) if base else None
        src_after = _blob_source(tgt, path)
        diff = diff_profiles(profile_source(src_before), profile_source(src_after))
        diff["signals"] = structural_signals(diff)
        per_file[path] = diff
        cc_before += _complexities(src_before)
        cc_after += _complexities(src_after)
        loc_delta += _sloc(src_after) - _sloc(src_before)
    total = merge_diffs({p: {k: v for k, v in d.items() if k != "signals"} for p, d in per_file.items()})
    ast_diff = {"files": per_file, "total": total, "signals": structural_signals(total)}
    return {
        "sha": tgt.hexsha,
        "parent_sha": base.hexsha if base else None,
        "author": f"{tgt.author.name} <{tgt.author.email}>",
        "timestamp": int(tgt.committed_date),
        "message": tgt.message.strip(),
        "files_modified_json": files,
        "ast_diff_json": ast_diff,
        "cyclomatic_before": round(statistics.fmean(cc_before), 2) if cc_before else 0.0,
        "cyclomatic_after": round(statistics.fmean(cc_after), 2) if cc_after else 0.0,
        "loc_delta": loc_delta,
    }


def head_stub(repo: git.Repo) -> dict[str, Any]:
    """Minimal commits row for HEAD so telemetry can be FK-linked before a full diff."""
    head = repo.head.commit
    return {"sha": head.hexsha, "parent_sha": head.parents[0].hexsha if head.parents else None,
            "author": f"{head.author.name} <{head.author.email}>", "timestamp": int(head.committed_date),
            "message": head.message.strip()}


def pct_delta(before: float | None, after: float | None) -> float | None:
    if before in (None, 0) or after is None:
        return None
    return round((after - before) / before * 100.0, 1)


def ratio(before: float | None, after: float | None) -> float | None:
    if before is None or after in (None, 0):
        return None
    return round(before / after, 1)


def _aggregate(runs: list[dict[str, Any]]) -> dict[str, float | None]:
    ok = [r for r in runs if r.get("exit_code", 0) == 0] or runs

    def mean(key: str) -> float | None:
        vals = [r[key] for r in ok if r.get(key) is not None]
        return statistics.fmean(vals) if vals else None

    return {"mean_latency_ms": mean("mean_latency_ms"), "peak_memory_mb": mean("peak_memory_mb"),
            "throughput_items_sec": mean("throughput_items_sec"), "samples": len(ok)}


def compute_deltas(base_runs: list[dict[str, Any]], tgt_runs: list[dict[str, Any]],
                   cc_before: float, cc_after: float) -> dict[str, Any]:
    b, t = _aggregate(base_runs), _aggregate(tgt_runs)
    r1 = lambda v: round(v, 1) if v is not None else None  # noqa: E731
    return {
        "baseline": {k: r1(v) for k, v in b.items() if k != "samples"} | {"samples": b["samples"]},
        "target": {k: r1(v) for k, v in t.items() if k != "samples"} | {"samples": t["samples"]},
        "latency_delta_pct": pct_delta(b["mean_latency_ms"], t["mean_latency_ms"]),
        "memory_delta_pct": pct_delta(b["peak_memory_mb"], t["peak_memory_mb"]),
        "throughput_delta_pct": pct_delta(b["throughput_items_sec"], t["throughput_items_sec"]),
        "speedup_multiplier_x": ratio(b["mean_latency_ms"], t["mean_latency_ms"]),
        "complexity_delta_pct": pct_delta(cc_before, cc_after),
    }


def allowed_numbers(state: dict[str, Any]) -> set[float]:
    """Every baseline value, target value and computed delta (signed and absolute)."""
    nums: set[float] = set()

    def add(v: Any) -> None:
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            nums.update({round(float(v), 1), round(abs(float(v)), 1)})

    delta = state["telemetry_delta"]
    for side in ("baseline", "target"):
        for value in delta[side].values():
            add(value)
    for key in ("latency_delta_pct", "memory_delta_pct", "throughput_delta_pct",
                "speedup_multiplier_x", "complexity_delta_pct"):
        add(delta.get(key))
    cx = state["complexity_delta"]
    for key in ("cyclomatic_before", "cyclomatic_after", "loc_delta"):
        add(cx.get(key))
    return nums


def build_state(commit: dict[str, Any], base_runs: list[dict[str, Any]],
                tgt_runs: list[dict[str, Any]], command: str | None) -> dict[str, Any]:
    """Compact telemetry state handed to Jev and the synthesizer."""
    cc_b, cc_a = commit.get("cyclomatic_before") or 0.0, commit.get("cyclomatic_after") or 0.0
    ast_diff = commit["ast_diff_json"]
    total = ast_diff.get("total", {})
    # Keep only what changed: non-empty lists and before/after counters that actually moved.
    compact_diff = {k: v for k, v in total.items()
                    if v != [] and not (isinstance(v, dict) and v.get("before") == v.get("after"))}
    state = {
        "baseline_sha": commit["parent_sha"],
        "target_sha": commit["sha"],
        "commit_message": commit.get("message", ""),
        "command": command,
        "files_modified": commit["files_modified_json"],
        "ast_structural_diff": {"signals": ast_diff.get("signals", []), **compact_diff},
        "complexity_delta": {"cyclomatic_before": round(cc_b, 1), "cyclomatic_after": round(cc_a, 1),
                             "loc_delta": commit.get("loc_delta", 0)},
        "telemetry_delta": compute_deltas(base_runs, tgt_runs, cc_b, cc_a),
    }
    state["complexity_delta"]["complexity_delta_pct"] = state["telemetry_delta"]["complexity_delta_pct"]
    return state
