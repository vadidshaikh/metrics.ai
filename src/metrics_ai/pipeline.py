"""`metrics generate` orchestration: Stage 1 gate → Stage 2 synthesis + guards → Stage 3 review."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from rich.console import Console

from metrics_ai import collector, config, model_router
from metrics_ai.cli_render import artifact_panel, delta_table, triage_table
from metrics_ai.decision_gate import DecisionGate, GateUnavailable, route_roles, weakest
from metrics_ai.llm_judge import LlmJudge
from metrics_ai.renderers import render_pr_md
from metrics_ai.storage import Ledger
from metrics_ai.synthesizer import SynthesisError, ledger_hash, synthesize


def pick_command(ledger: Ledger, base: str, tgt: str, command: Optional[str]) -> Optional[str]:
    if command:
        return command
    base_cmds = {r["command"] for r in ledger.runs_for(base)}
    shared = [r["command"] for r in reversed(ledger.runs_for(tgt)) if r["command"] in base_cmds]
    return shared[0] if shared else None


def ensure_model(root: Path, cfg: dict[str, Any], console: Console) -> bool:
    """Make sure a model is locked (running discovery if needed); False when none is reachable."""
    if not model_router.model_chain(cfg):
        with console.status("No model locked yet; running discovery…"):
            model_router.discover(cfg)
        config.save_config(root, cfg)
    return bool(model_router.model_chain(cfg))


def build_gate(root: Path, cfg: dict[str, Any], offline: bool, console: Console) -> DecisionGate:
    """Tier 1 when a SystemOne key resolves (TypeSafe key wins, else OpenJev); the locked LLM is always
    wired as the Tier-2 judge so an unreachable SystemOne endpoint degrades to `llm_judge`."""
    endpoint, model, api_key, _label = config.jev_target(cfg)
    judge = LlmJudge(cfg) if not offline and ensure_model(root, cfg, console) else None
    return DecisionGate(cfg, api_key, judge=judge, offline=offline, endpoint=endpoint, model=model)


def load_state(root: Path, ledger: Ledger, console: Console, commit: str, baseline: Optional[str],
               command: Optional[str]) -> tuple[str, str, dict[str, Any]]:
    """Diff the commits, pull the latest shared telemetry and build the compact state."""
    with collector.open_repo(root) as repo:
        tgt = collector.resolve_sha(repo, commit)
        base = collector.resolve_sha(repo, baseline or f"{tgt}~1")
        record = collector.commit_record(repo, tgt, base)
    ledger.upsert_commit(record)
    cmd = pick_command(ledger, base, tgt, command)
    base_runs = ledger.runs_for(base, cmd)[-1:] if cmd else []
    tgt_runs = ledger.runs_for(tgt, cmd)[-1:] if cmd else []
    if not (base_runs and tgt_runs):
        console.print("[yellow]![/] No shared telemetry for both commits; using static AST/complexity only.")
    return base, tgt, collector.build_state(record, base_runs, tgt_runs, cmd)


def generate(root: Path, cfg: dict[str, Any], ledger: Ledger, console: Console, *, commit: str,
             baseline: Optional[str], fmt: str, role: str, density: str, command: Optional[str],
             force: bool, offline: bool, jev_response: Optional[dict[str, Any]] = None) -> list[str]:
    """Run the full pipeline; returns the roles that failed to produce a verified artifact."""
    base, tgt, state = load_state(root, ledger, console, commit, baseline, command)
    allowed = collector.allowed_numbers(state)
    console.print(delta_table(state))

    gate = build_gate(root, cfg, offline, console)
    try:
        triage = gate.triage(state, jev_response=None if offline else jev_response)
    except GateUnavailable as exc:
        console.print(f"[red]Stage-1 gate unavailable:[/] {exc}")
        console.print("Run `metrics auth` (SystemOne key or an LLM provider), or pass --offline-no-llm.")
        return ["gate"]
    threshold = float(cfg["jev"]["worthy_threshold"])
    console.print(triage_table(triage, threshold))
    for note in triage.notes:
        console.print(f"[dim]{note}[/]")
    if triage.critique:
        console.print(f"[dim]judge critique: {triage.critique}[/]")
    base_row = {"baseline_sha": base, "target_sha": tgt, "format_type": fmt, "density": density,
                "gate_mode": triage.gate_mode, "jev_worth_prob": triage.worth_prob,
                "jev_impact_score": triage.impact_score}

    if fmt == "pr-md":
        text = render_pr_md(state)
        ledger.insert_impact({**base_row, "role": "n/a", "provider_used": "deterministic",
                              "model_used": "renderers.render_pr_md", "output_text": text,
                              "structured_json": state, "ledger_hash": ledger_hash(state, text)})
        console.print(f"[dim]PR Markdown · deterministic renderer · hash={ledger_hash(state, text)[:12]}[/]")
        print(text)
        return []
    if triage.worth_prob < threshold and not force:
        reason = (f"is_resume_worthy p={triage.worth_prob:.3f} < {threshold:.2f}: the commit lacks a "
                  "substantive structural change backed by measured gains. Use --force to override.")
        ledger.insert_impact({**base_row, "role": role, "output_text": "", "structured_json": {"halted": reason},
                              "ledger_hash": ledger_hash(state, "")})
        console.print(f"[bold yellow]Synthesis halted.[/] {reason}")
        return []
    if not ensure_model(root, cfg, console):
        console.print("[red]No reachable LLM provider; cannot synthesize. Run `metrics auth`.[/]")
        return route_roles(triage.role_distribution, role)

    failed: list[str] = []
    produced: dict[str, str] = {}  # role -> bullet, so later roles are not clones of earlier ones
    for target_role in route_roles(triage.role_distribution, role):
        try:
            result = synthesize(cfg, gate, state, allowed, fmt, target_role, density,
                                log=lambda m: console.print(f"[dim]{m}[/]"), others=produced)
        except (SynthesisError, model_router.ProviderUnavailable, GateUnavailable) as exc:
            console.print(f"[red]{target_role}: {exc}[/]")
            failed.append(target_role)
            continue
        review = result.review
        produced[target_role] = result.structured.get("combined_xyz_bullet") or result.output_text
        digest = ledger_hash(state, result.output_text)
        structured = {**result.structured, "gate": {
            "triage_mode": triage.gate_mode, "review_mode": review.gate_mode,
            "faithful_prob": review.faithful_prob, "metric_prob": review.metric_prob,
            "natural_prob": review.natural_prob, "critique": review.critique, "attempts": result.attempts}}
        ledger.insert_impact({**base_row, "role": target_role,
                              "gate_mode": weakest(triage.gate_mode, review.gate_mode),
                              "jev_role_prob": triage.role_distribution.get(target_role),
                              "jev_faithful_prob": review.faithful_prob, "natural_prob": review.natural_prob,
                              "provider_used": result.provider, "model_used": result.model,
                              "output_text": result.output_text, "structured_json": structured, "ledger_hash": digest})
        console.print(artifact_panel(
            f"{fmt.upper()} · {target_role}", result.output_text,
            f"{result.provider}/{result.model} · {review.gate_mode} faithful={review.faithful_prob:.2f} "
            f"metric={review.metric_prob:.2f} natural={review.natural_prob:.2f} · attempts={result.attempts} "
            f"· hash={digest[:12]}"))
        console.print(json.dumps({"role": target_role, "cited_numbers": result.structured["cited_numbers"]}))
    return failed
