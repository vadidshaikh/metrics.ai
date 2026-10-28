"""Rich tables and panels used by the CLI."""

from __future__ import annotations

from typing import Any

from rich.panel import Panel
from rich.table import Table

from metrics_ai.decision_gate import TriageResult


def _f(value: Any, digits: int = 1, unit: str = "") -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}{unit}" if isinstance(value, float) else f"{value}{unit}"


def telemetry_table(sha: str, run: dict[str, Any]) -> Table:
    table = Table(title=f"Telemetry captured for {sha[:10]}", show_lines=False)
    for col in ("Command", "Mean latency", "Peak RSS", "Throughput", "Exit"):
        table.add_column(col)
    table.add_row(run["command"], _f(run["mean_latency_ms"], 1, " ms"), _f(run["peak_memory_mb"], 1, " MB"),
                  _f(run.get("throughput_items_sec"), 1, " ops/s"), str(run["exit_code"]))
    return table


def delta_table(state: dict[str, Any]) -> Table:
    d = state["telemetry_delta"]
    table = Table(title=f"Delta {str(state['baseline_sha'])[:7]} → {state['target_sha'][:7]}")
    for col in ("Metric", "Baseline", "Target", "Delta"):
        table.add_column(col)
    for label, key, pct in (("Mean latency (ms)", "mean_latency_ms", "latency_delta_pct"),
                            ("Peak memory (MB)", "peak_memory_mb", "memory_delta_pct"),
                            ("Throughput (items/s)", "throughput_items_sec", "throughput_delta_pct")):
        table.add_row(label, _f(d["baseline"][key]), _f(d["target"][key]), _f(d[pct], 1, "%"))
    cx = state["complexity_delta"]
    table.add_row("Avg cyclomatic", _f(cx["cyclomatic_before"]), _f(cx["cyclomatic_after"]),
                  _f(d["complexity_delta_pct"], 1, "%"))
    table.add_row("Speedup", "", _f(d["speedup_multiplier_x"], 1, "x"), "")
    return table


def triage_table(result: TriageResult, threshold: float) -> Table:
    table = Table(title=f"Stage-1 decision gate (gate_mode: {result.gate_mode})")
    table.add_column("Question")
    table.add_column("Answer")
    verdict = "PASS" if result.worth_prob >= threshold else "BELOW THRESHOLD"
    table.add_row("is_resume_worthy (noul)", f"p={result.worth_prob:.3f}  [{verdict} @ {threshold:.2f}]")
    table.add_row("impact_tier (score)", f"{result.impact_tier} / 5  (normalized score {result.impact_score:.2f})")
    dist = ", ".join(f"{r}={p:.3f}" for r, p in sorted(result.role_distribution.items(), key=lambda kv: -kv[1]))
    table.add_row("best_fit_role (choice)", dist)
    return table


def discovery_table(report: dict[str, Any]) -> Table:
    table = Table(title=f"Model discovery ({report['total_discovered']} eligible models)")
    for col in ("Rank", "Provider", "Model", "Score", "Probe", "Status"):
        table.add_column(col)
    probed = {(c["provider"], c["model"]): c for c in report["probed"]}
    winner = report.get("winner") or {}
    shown = report["candidates"][:12]
    shown += [c for c in report["candidates"][12:] if (c["provider"], c["model"]) in probed]
    for cand in shown:
        i = report["candidates"].index(cand) + 1
        p = probed.get((cand["provider"], cand["model"]), cand)
        if (p["provider"], p["model"]) == (winner.get("provider"), winner.get("model")):
            status = "[bold green]LOCKED[/]"
        elif p.get("probe_ok"):
            status = "fallback"
        elif p.get("probe_ok") is False:
            status = f"[red]failed {p.get('note', '')}[/]"
        else:
            status = "not probed"
        table.add_row(str(i), p["provider"], p["model"], str(p["score"]),
                      _f(p.get("probe_ms"), 1, " ms"), status)
    return table


def artifact_panel(title: str, body: str, subtitle: str) -> Panel:
    return Panel(body, title=title, subtitle=subtitle, expand=False)


def prune_table(deleted: dict[str, int], remaining: dict[str, int]) -> Table:
    table = Table(title="Ledger prune (VACUUM complete)")
    for col in ("Table", "Deleted", "Remaining"):
        table.add_column(col)
    for name in ("commits", "telemetry_runs", "synthesized_impacts"):
        table.add_row(name, str(deleted.get(name, 0)), str(remaining.get(name, 0)))
    return table
