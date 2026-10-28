"""Typer CLI: `metrics init | auth | wrap | pytest | generate | prune | ui`."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from metrics_ai import collector, config, model_router, pipeline, profiler
from metrics_ai.cli_render import discovery_table, prune_table, telemetry_table
from metrics_ai.decision_gate import DecisionGate
from metrics_ai.storage import Ledger

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
    for env in config.shadowed_secrets(cfg):
        console.print(f"[yellow]![/] ${env} is set and overrides the key stored in config.toml; "
                      f"unset it to use the stored key.")
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
    jev = ("configured (Tier-1 jev_api gate)" if config.get_secret(cfg, "jev_api_key")
           else "not set (Tier-2 llm_judge gate will use the locked model)")
    console.print(f"Jev / TypeSafe API key: {jev}")


def _record_head(ledger: Ledger, root: Path) -> str:
    with collector.open_repo(root) as repo:
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
    with collector.open_repo(root) as repo:
        ledger.upsert_commit(collector.commit_record(repo, "HEAD"))


@app.command()
def generate(commit: str = typer.Option("HEAD", "--commit", help="Target revision."),
             baseline: Optional[str] = typer.Option(None, "--baseline", help="Baseline (default: parent)."),
             fmt: str = typer.Option("xyz", "--format", help="xyz | star | pr-md"),
             role: str = typer.Option("auto", "--role", help="auto | aiml | backend | mlops | tpm | all"),
             density: str = typer.Option("standard", "--density", help="ultra-compact | standard | detailed"),
             command: Optional[str] = typer.Option(None, "--command", help="Telemetry command to compare."),
             force: bool = typer.Option(False, "--force", help="Bypass the worthiness gate."),
             offline_no_llm: bool = typer.Option(False, "--offline-no-llm",
                                                 help="Use only the Tier-3 deterministic gate."),
             jev_response: Optional[Path] = typer.Option(None, "--jev-response", exists=True, dir_okay=False,
                                                         help="SystemOne triage reply saved from the Jev "
                                                              "playground (see `metrics jev-request`).")) -> None:
    """Stage 1 gate → Stage 2 LLM synthesis + numeric/prose guard → Stage 3 faithfulness/fluency review."""
    if fmt not in {"xyz", "star", "pr-md"}:
        raise typer.BadParameter("--format must be xyz, star or pr-md")
    if role not in {"auto", "all", "aiml", "backend", "mlops", "tpm"}:
        raise typer.BadParameter("invalid --role")
    if density not in {"ultra-compact", "standard", "detailed"}:
        raise typer.BadParameter("invalid --density")
    root, cfg, ledger = _ctx()
    failed = pipeline.generate(root, cfg, ledger, console, commit=commit, baseline=baseline, fmt=fmt,
                               role=role, density=density, command=command, force=force,
                               offline=offline_no_llm, jev_response=_load_json(jev_response))
    if failed:
        console.print(f"[red]No verified artifact for: {', '.join(failed)}[/]")
        raise typer.Exit(1)


def _load_json(path: Optional[Path]) -> Optional[dict]:
    if path is None:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"{path} is not valid JSON: {exc}") from exc


@app.command("jev-request")
def jev_request(commit: str = typer.Option("HEAD", "--commit"),
                baseline: Optional[str] = typer.Option(None, "--baseline"),
                command: Optional[str] = typer.Option(None, "--command"),
                stage: str = typer.Option("triage", "--stage", help="triage | review"),
                candidate: str = typer.Option("", "--candidate", help="Bullet text for --stage review."),
                out: Optional[Path] = typer.Option(None, "--out", help="Also write the body to this file.")) -> None:
    """Print the exact SystemOne request body to run in the Jev playground (no API key needed)."""
    if stage not in {"triage", "review"}:
        raise typer.BadParameter("--stage must be triage or review")
    if stage == "review" and not candidate:
        raise typer.BadParameter("--stage review needs --candidate")
    root, cfg, ledger = _ctx()
    quiet = Console(stderr=True, highlight=False)
    _, _, state = pipeline.load_state(root, ledger, quiet, commit, baseline, command)
    body = DecisionGate(cfg).request_body(stage, state, candidate)
    text = json.dumps(body, indent=2, ensure_ascii=False)
    if out:
        out.write_text(text + "\n", encoding="utf-8")
        quiet.print(f"[dim]wrote {out}[/]")
    print(text)


@app.command()
def prune(all_rows: bool = typer.Option(False, "--all", help="Truncate every ledger table."),
          keep_latest: bool = typer.Option(False, "--keep-latest",
                                           help="Keep only the newest run/synthesis per key.")) -> None:
    """Remove stale or superseded ledger rows, then VACUUM."""
    if all_rows == keep_latest:
        raise typer.BadParameter("pass exactly one of --all or --keep-latest")
    _, _, ledger = _ctx()
    deleted = ledger.prune_ledger("all" if all_rows else "keep_latest")
    console.print(prune_table(deleted, ledger.counts()))


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
