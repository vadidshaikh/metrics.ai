"""Deterministic text renderers: XYZ tree, STAR blocks and the PR Markdown delta table."""

from __future__ import annotations

from typing import Any

from metrics_ai.schemas import StarResponse, XyzResponse


def render_xyz(obj: XyzResponse) -> str:
    return (f"{obj.combined_xyz_bullet}\n"
            f"├─ [X] {obj.x_accomplishment}\n"
            f"├─ [Y] {obj.y_measurement}\n"
            f"└─ [Z] {obj.z_mechanism}")


def render_star(obj: StarResponse) -> str:
    return (f"[SITUATION] {obj.situation}\n[TASK] {obj.task}\n"
            f"[ACTION] {obj.action}\n[RESULT] {obj.result}")


def _fmt(value: float | None, unit: str = "") -> str:
    return "n/a" if value is None else f"{value:.1f}{unit}"


def _status(delta: float | None, lower_is_better: bool = True) -> str:
    if delta is None:
        return "➖ n/a"
    if abs(delta) < 1.0:
        return "➖ neutral"
    improved = delta < 0 if lower_is_better else delta > 0
    return "✅ improved" if improved else "❌ regressed"


def _signed(delta: float | None) -> str:
    return "n/a" if delta is None else f"{delta:+.1f}%"


def render_pr_md(state: dict[str, Any]) -> str:
    d = state["telemetry_delta"]
    b, t = d["baseline"], d["target"]
    cx = state["complexity_delta"]
    base7 = (state["baseline_sha"] or "")[:7]
    tgt7 = state["target_sha"][:7]
    rows = [
        ("Mean latency (ms)", b["mean_latency_ms"], t["mean_latency_ms"], d["latency_delta_pct"], True),
        ("Peak memory RSS (MB)", b["peak_memory_mb"], t["peak_memory_mb"], d["memory_delta_pct"], True),
        ("Throughput (items/s)", b["throughput_items_sec"], t["throughput_items_sec"],
         d["throughput_delta_pct"], False),
        ("Avg cyclomatic complexity", cx["cyclomatic_before"], cx["cyclomatic_after"],
         d["complexity_delta_pct"], True),
    ]
    lines = [
        f"### metrics.ai telemetry delta: `{base7}` → `{tgt7}`",
        "",
        f"Command: `{state.get('command') or 'n/a'}`",
        "",
        f"| Metric | Baseline `{base7}` | Target `{tgt7}` | Delta (%) | Status |",
        "| :--- | ---: | ---: | ---: | :--- |",
    ]
    for label, before, after, delta, lower in rows:
        lines.append(f"| {label} | {_fmt(before)} | {_fmt(after)} | {_signed(delta)} | {_status(delta, lower)} |")
    if d.get("speedup_multiplier_x") is not None:
        lines.append(f"| Speedup | 1.0x | {d['speedup_multiplier_x']:.1f}x | — | "
                     f"{'🚀' if d['speedup_multiplier_x'] > 1 else '➖'} |")
    lines += ["", "#### AST summary", ""]
    lines += [f"- {item}" for item in ast_summary(state)]
    return "\n".join(lines)


def ast_summary(state: dict[str, Any]) -> list[str]:
    diff = state["ast_structural_diff"]
    cx = state["complexity_delta"]
    items = []
    if diff.get("signals"):
        items.append("Structural signals: " + ", ".join(f"`{s}`" for s in diff["signals"]))
    for key, label in (("batching_calls_added", "Batched calls added"),
                       ("caching_decorators_added", "Caching decorators added"),
                       ("concurrency_calls_added", "Concurrency primitives added"),
                       ("calls_hoisted_out_of_loops", "Calls removed from loop bodies"),
                       ("added_functions", "Functions added"),
                       ("removed_functions", "Functions removed")):
        values = diff.get(key) or []
        if values:
            shown = ", ".join(f"`{v}`" for v in values[:8])
            items.append(f"{label}: {shown}" + (f" (+{len(values) - 8} more)" if len(values) > 8 else ""))
    for key, label in (("loops", "Loop constructs"), ("nested_loops", "Nested loops")):
        if key in diff:
            items.append(f"{label}: {diff[key]['before']} → {diff[key]['after']}")
    items.append(f"Net LOC delta: {cx['loc_delta']:+d}; files modified: "
                 + ", ".join(f"`{f}`" for f in state["files_modified"][:10]))
    return items
