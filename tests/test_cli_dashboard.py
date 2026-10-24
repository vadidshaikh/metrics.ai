from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
from typer.testing import CliRunner

from metrics_ai.cli import app
from metrics_ai.dashboard.server import create_app
from metrics_ai.storage import Ledger

runner = CliRunner()


def _get(asgi_app, path: str) -> httpx.Response:
    async def go() -> httpx.Response:
        transport = httpx.ASGITransport(app=asgi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path)
    return asyncio.run(go())


CMD = f'"{sys.executable}" -c "pass"'


def _wrap_both(repo, monkeypatch):
    root = Path(repo.working_dir)
    monkeypatch.chdir(root)
    assert runner.invoke(app, ["init", "--hook"]).exit_code == 0
    target = repo.head.commit.hexsha
    repo.git.checkout("HEAD~1")
    assert runner.invoke(app, ["wrap", CMD, "--runs", "1"]).exit_code == 0
    repo.git.checkout(target)
    assert runner.invoke(app, ["wrap", CMD, "--runs", "1"]).exit_code == 0
    return root


def test_init_creates_ledger_config_gitignore_and_hook(repo, monkeypatch):
    root = Path(repo.working_dir)
    monkeypatch.chdir(root)
    result = runner.invoke(app, ["init", "--hook"])
    assert result.exit_code == 0, result.output
    assert (root / ".metrics" / "ledger.db").exists() and (root / ".metrics" / "config.toml").exists()
    assert ".metrics/" in (root / ".gitignore").read_text(encoding="utf-8")
    assert "metrics capture" in (root / ".git" / "hooks" / "post-commit").read_text(encoding="utf-8")


def test_commands_require_init(repo, monkeypatch):
    monkeypatch.chdir(repo.working_dir)
    assert runner.invoke(app, ["wrap", CMD]).exit_code == 1


def test_wrap_then_generate_pr_md(repo, monkeypatch):
    root = _wrap_both(repo, monkeypatch)
    result = runner.invoke(app, ["generate", "--format", "pr-md"])
    assert result.exit_code == 0, result.output
    assert "| Mean latency (ms) |" in result.output and "#### AST summary" in result.output
    dump = Ledger(root / ".metrics" / "ledger.db").dump()
    assert len(dump["telemetry_runs"]) == 2
    assert dump["synthesized_impacts"][0]["provider_used"] == "deterministic"


def test_generate_halts_below_gate_and_logs(repo, monkeypatch):
    root = Path(repo.working_dir)
    # A comment-only commit has no structural change, so the gate must halt whatever the timing noise.
    with (root / "ingest.py").open("a", encoding="utf-8") as fh:
        fh.write("# docs: clarify ingest\n")
    repo.index.add(["ingest.py"])
    repo.index.commit("docs: comment only")
    _wrap_both(repo, monkeypatch)
    result = runner.invoke(app, ["generate", "--format", "xyz"])
    assert result.exit_code == 0, result.output
    assert "Synthesis halted" in result.output
    impact = Ledger(root / ".metrics" / "ledger.db").dump()["synthesized_impacts"][0]
    assert "halted" in json.loads(impact["structured_json"])


def test_generate_rejects_bad_format(repo, monkeypatch):
    _wrap_both(repo, monkeypatch)
    assert runner.invoke(app, ["generate", "--format", "nope"]).exit_code != 0


def test_capture_records_head_ast(repo, monkeypatch):
    root = Path(repo.working_dir)
    monkeypatch.chdir(root)
    runner.invoke(app, ["init"])
    assert runner.invoke(app, ["capture"]).exit_code == 0
    commit = Ledger(root / ".metrics" / "ledger.db").get_commit(repo.head.commit.hexsha)
    assert "batched_operations_added" in json.loads(commit["ast_diff_json"])["signals"]


def test_dashboard_serves_ledger_and_html(repo, monkeypatch):
    root = _wrap_both(repo, monkeypatch)
    dashboard = create_app(root / ".metrics" / "ledger.db")
    data = _get(dashboard, "/api/ledger").json()
    assert len(data["telemetry_runs"]) == 2
    assert isinstance(data["telemetry_runs"][0]["raw_benchmark_json"], dict)
    html = _get(dashboard, "/").text
    assert "new Chart(" in html and "new CanvasJS.Chart(" in html


def test_dashboard_missing_ledger_returns_empty(tmp_path):
    data = _get(create_app(tmp_path / "none.db"), "/api/ledger").json()
    assert data == {"commits": [], "telemetry_runs": [], "synthesized_impacts": []}
