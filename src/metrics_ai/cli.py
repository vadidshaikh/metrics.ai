"""Typer CLI: `metrics init | auth | wrap | pytest | generate | ui`."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from metrics_ai import collector, config, model_router, profiler
from metrics_ai.cli_render import (artifact_panel, delta_table, discovery_table, telemetry_table,
                                   triage_table)
from metrics_ai.decision_gate import DecisionGate, route_roles
from metrics_ai.renderers import render_pr_md
from metrics_ai.storage import Ledger
from metrics_ai.synthesizer import SynthesisError, ledger_hash, synthesize

# Force UTF-8 so tree glyphs/emoji render when output is piped on Windows code pages.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure") and (_stream.encoding or "").lower() != "utf-8":
        _stream.reconfigure(encoding="utf-8", errors="replace")

app = typer.Typer(help="metrics.ai — local-first telemetry and empirical impact synthesis.",
                  no_args_is_help=True, add_completion=False)
console = Console(highlight=False)

HOOK_SCRIPT = "#!/bin/sh\n# metrics.ai: record AST/complexity for every new commit (backgrounded).\n" \
              "metrics capture >/dev/null 2>&1 &\n"


def _ctx() -> tuple[Path, dict, Ledger]:
    root = config.find_root()
    ledger = Ledger(config.ledger_path(root))
    if not config.metrics_dir(root).is_dir():
        console.print("[red]No .metrics/ directory found. Run `metrics init` first.[/]")
        raise typer.Exit(1)
    ledger.init_schema()
    return root, config.load_config(root), ledger


@app.command()
def init(hook: bool = typer.Option(False, "--hook", help="Install a fast .git/hooks/post-commit script.")) -> None:
    """Initialise .metrics/, the SQLite ledger, config.toml and .gitignore safety."""
    root = config.find_root()
    config.metrics_dir(root).mkdir(exist_ok=True)
    Ledger(config.ledger_path(root)).init_schema()
    if not config.config_path(root).exists():
        config.save_config(root, config.load_config(root))
    added = config.ensure_gitignore(root)
    console.print(f"[green]✓[/] Ledger ready at {config.ledger_path(root)}")
    console.print(f"[green]✓[/] Config at {config.config_path(root)}")
    console.print("[green]✓[/] Added .metrics/ to .gitignore" if added else "[green]✓[/] .metrics/ already git-ignored")
    if hook:
        hook_path = root / ".git" / "hooks" / "post-commit"
        if not (root / ".git").is_dir():
            console.print("[yellow]![/] Not a git repository; skipped post-commit hook.")
        else:
            hook_path.write_text(HOOK_SCRIPT, encoding="utf-8", newline="\n")
            hook_path.chmod(0o755)
            console.print(f"[green]✓[/] Installed {hook_path}")


@app.command()
def auth(groq_key: Optional[str] = typer.Option(None, "--groq-key"),
         hf_token: Optional[str] = typer.Option(None, "--hf-token"),
         jev_key: Optional[str] = typer.Option(None, "--jev-key"),
         prefer: Optional[str] = typer.Option(None, "--prefer", help="groq | hf | ollama")) -> None:
    """Store credentials, then discover, score, probe and lock the best model."""
    root, cfg, _ = _ctx()
    cfg = config.store_secrets(root, cfg, groq_api_key=groq_key, hf_token=hf_token, jev_api_key=jev_key)
    if prefer:
        if prefer not in {"groq", "hf", "ollama"}:
            raise typer.BadParameter("--prefer must be groq, hf or ollama")
        cfg["providers"]["prefer"] = prefer
    with console.status("Discovering models…"):
        report = model_router.discover(cfg)
    config.save_config(root, cfg)
    console.print(discovery_table(report))
    for note in report["notes"]:
        console.print(f"[yellow]![/] {note}")
    if report["winner"]:
        w = report["winner"]
        console.print(f"[bold green]Locked[/] {w['provider']}/{w['model']} "
                      f"(+{len(cfg['router']['fallback_models'])} fallbacks)")
    else:
        console.print("[red]No model passed the live probe.[/]")
        raise typer.Exit(1)
    jev = "configured" if config.get_secret(cfg, "jev_api_key") else "not set (offline gate will be used)"
    console.print(f"Jev API key: {jev}")


def _record_head(ledger: Ledger, root: Path) -> str:
    repo = collector.open_repo(root)
    stub = collector.head_stub(repo)
    ledger.upsert_commit(stub)
    return stub["sha"]


@app.command()
def wrap(cmd: str = typer.Argument(..., help="Command to profile, e.g. \"python bench.py\""),
         runs: int = typer.Option(3, "--runs", min=1, help="Repetitions; latency is the mean."),
         shell: bool = typer.Option(False, "--shell", help="Run through the system shell.")) -> None:
    """Profile an arbitrary command (10 ms psutil polling) and link telemetry to HEAD."""
    root, _, ledger = _ctx()
    sha = _record_head(ledger, root)
    with console.status(f"Profiling `{cmd}` × {runs}…"):
        run = profiler.profile_command(cmd, runs=runs, cwd=root, shell=shell)
    run["commit_sha"] = sha
    run_id = ledger.insert_run(run)
    console.print(telemetry_table(sha, run))
    console.print(f"run_id={run_id}")
    if run["exit_code"] != 0:
        raise typer.Exit(run["exit_code"])


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def pytest(ctx: typer.Context) -> None:
    """Run pytest-benchmark (extra ARGS are passed to pytest) and ingest results for HEAD."""
    root, _, ledger = _ctx()
    sha = _record_head(ledger, root)
    records, exit_code = profiler.run_pytest_benchmark(list(ctx.args), cwd=root)
    for rec in records:
        rec["commit_sha"] = sha
        console.print(telemetry_table(sha, rec))
    ledger.insert_runs(records)
    if not records:
        console.print("[yellow]No benchmarks found in pytest-benchmark output.[/]")
    raise typer.Exit(exit_code)


@app.command(hidden=True)
def capture() -> None:
    """Record AST/complexity for HEAD (used by the post-commit hook)."""
    root, _, ledger = _ctx()
    ledger.upsert_commit(collector.commit_record(collector.open_repo(root), "HEAD"))


def _pick_command(ledger: Ledger, base: str, tgt: str, command: Optional[str]) -> Optional[str]:
    if command:
        return command
    base_cmds = {r["command"] for r in ledger.runs_for(base)}
    shared = [r["command"] for r in reversed(ledger.runs_for(tgt)) if r["command"] in base_cmds]
    return shared[0] if shared else None


@app.command()
def generate(commit: str = typer.Option("HEAD", "--commit", help="Target revision."),
             baseline: Optional[str] = typer.Option(None, "--baseline", help="Baseline (default: parent)."),
             fmt: str = typer.Option("xyz", "--format", help="xyz | star | pr-md"),
             role: str = typer.Option("auto", "--role", help="auto | aiml | backend | mlops | tpm | all"),
             density: str = typer.Option("standard", "--density", help="ultra-compact | standard | detailed"),
             command: Optional[str] = typer.Option(None, "--command", help="Telemetry command to compare."),
             force: bool = typer.Option(False, "--force", help="Bypass the Jev worthiness gate.")) -> None:
    """Jev triage → auto-routed LLM synthesis + numeric guard → Jev faithfulness check."""
    if fmt not in {"xyz", "star", "pr-md"}:
        raise typer.BadParameter("--format must be xyz, star or pr-md")
    if role not in {"auto", "all", "aiml", "backend", "mlops", "tpm"}:
        raise typer.BadParameter("invalid --role")
    if density not in {"ultra-compact", "standard", "detailed"}:
        raise typer.BadParameter("invalid --density")
    root, cfg, ledger = _ctx()
    repo = collector.open_repo(root)
    tgt = collector.resolve_sha(repo, commit)
    base = collector.resolve_sha(repo, baseline or f"{tgt}~1")
    record = collector.commit_record(repo, tgt, base)
    ledger.upsert_commit(record)
    cmd = _pick_command(ledger, base, tgt, command)
    base_runs = ledger.runs_for(base, cmd)[-1:] if cmd else []
    tgt_runs = ledger.runs_for(tgt, cmd)[-1:] if cmd else []
    if not (base_runs and tgt_runs):
        console.print("[yellow]![/] No shared telemetry for both commits; using static AST/complexity only.")
    state = collector.build_state(record, base_runs, tgt_runs, cmd)
    allowed = collector.allowed_numbers(state)
    console.print(delta_table(state))
    gate = DecisionGate(cfg, config.get_secret(cfg, "jev_api_key"))
    triage = gate.triage(state)
    threshold = float(cfg["jev"]["worthy_threshold"])
    console.print(triage_table(triage, threshold))
    for note in triage.notes:
        console.print(f"[dim]{note}[/]")
    base_row = {"baseline_sha": base, "target_sha": tgt, "format_type": fmt, "density": density,
                "jev_worth_prob": triage.worth_prob, "jev_impact_score": triage.impact_score}

    if fmt == "pr-md":
        text = render_pr_md(state)
        ledger.insert_impact({**base_row, "role": "n/a", "provider_used": "deterministic",
                              "model_used": "renderers.render_pr_md", "output_text": text,
                              "structured_json": state, "ledger_hash": ledger_hash(state, text)})
        console.print(f"[dim]PR Markdown · deterministic renderer · hash={ledger_hash(state, text)[:12]}[/]")
        print(text)
        return
    if triage.worth_prob < threshold and not force:
        reason = (f"is_resume_worthy p={triage.worth_prob:.3f} < {threshold:.2f}: the commit lacks a "
                  "substantive structural change backed by measured gains. Use --force to override.")
        ledger.insert_impact({**base_row, "role": role, "output_text": "", "structured_json": {"halted": reason},
                              "ledger_hash": ledger_hash(state, "")})
        console.print(f"[bold yellow]Synthesis halted.[/] {reason}")
        return
    if not model_router.model_chain(cfg):
        with console.status("No model locked yet; running discovery…"):
            model_router.discover(cfg)
        config.save_config(root, cfg)
    for target_role in route_roles(triage.role_distribution, role):
        try:
            result = synthesize(cfg, gate, state, allowed, fmt, target_role, density,
                                log=lambda m: console.print(f"[dim]{m}[/]"))
        except (SynthesisError, model_router.ProviderUnavailable) as exc:
            console.print(f"[red]{target_role}: {exc}[/]")
            raise typer.Exit(1)
        digest = ledger_hash(state, result.output_text)
        ledger.insert_impact({**base_row, "role": target_role,
                              "jev_role_prob": triage.role_distribution.get(target_role),
                              "jev_faithful_prob": result.faithful_prob, "provider_used": result.provider,
                              "model_used": result.model, "output_text": result.output_text,
                              "structured_json": result.structured, "ledger_hash": digest})
        console.print(artifact_panel(
            f"{fmt.upper()} · {target_role}", result.output_text,
            f"{result.provider}/{result.model} · faithful={result.faithful_prob:.2f} ({result.faithful_source})"
            f" · attempts={result.attempts} · hash={digest[:12]}"))
        console.print(json.dumps({"role": target_role, "cited_numbers": result.structured["cited_numbers"]}))


@app.command()
def ui(port: int = typer.Option(8420, "--port"), host: str = typer.Option("127.0.0.1", "--host")) -> None:
    """Launch the local FastAPI dashboard."""
    import uvicorn

    from metrics_ai.dashboard.server import create_app

    root = config.find_root()
    console.print(f"Serving dashboard on http://{host}:{port}")
    uvicorn.run(create_app(config.ledger_path(root)), host=host, port=port, log_level="warning")


if __name__ == "__main__":  # pragma: no cover
    app()
