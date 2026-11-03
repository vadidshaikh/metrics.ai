from __future__ import annotations

import sqlite3

import pytest

from metrics_ai import config
from metrics_ai.storage import Ledger


def test_schema_and_round_trip(tmp_path):
    ledger = Ledger(tmp_path / ".metrics" / "ledger.db")
    ledger.init_schema()
    ledger.upsert_commit({"sha": "abc", "parent_sha": "p", "message": "m", "ast_diff_json": {"signals": []},
                          "files_modified_json": ["a.py"], "cyclomatic_before": 2.0, "cyclomatic_after": 1.5,
                          "loc_delta": -3})
    ledger.upsert_commit({"sha": "abc", "message": "updated"})  # partial update keeps other columns
    commit = ledger.get_commit("abc")
    assert commit["message"] == "updated" and commit["loc_delta"] == -3
    run_id = ledger.insert_run({"commit_sha": "abc", "command": "c", "mean_latency_ms": 1.5,
                                "peak_memory_mb": 2.0, "exit_code": 0, "raw_benchmark_json": {"runs": []}})
    assert ledger.runs_for("abc", "c")[0]["run_id"] == run_id
    assert ledger.runs_for("abc", "other") == []
    ledger.insert_impact({"baseline_sha": "p", "target_sha": "abc", "output_text": "x", "structured_json": {}})
    dump = ledger.dump()
    assert [len(dump[t]) for t in ("commits", "telemetry_runs", "synthesized_impacts")] == [1, 1, 1]


def test_foreign_key_enforced(tmp_path):
    ledger = Ledger(tmp_path / "l.db")
    ledger.init_schema()
    with pytest.raises(sqlite3.IntegrityError):
        ledger.insert_run({"commit_sha": "missing", "command": "c"})


def test_config_defaults_save_and_merge(tmp_path):
    cfg = config.load_config(tmp_path)
    assert (cfg["jev"]["endpoint"], cfg["jev"]["model"]) == ("https://api.codiv.ai/v1/systemone", "openjev-latest")
    assert (cfg["jev"]["typesafe_endpoint"], cfg["jev"]["typesafe_model"]) == \
        ("https://api.typesafe.ai/v1/systemone", "jev-1.13.0")
    cfg["router"]["active_model"] = "m"
    config.save_config(tmp_path, cfg)
    assert config.load_config(tmp_path)["router"]["active_model"] == "m"
    assert config.load_config(tmp_path)["jev"]["worthy_threshold"] == 0.60


def test_gitignore_safety(tmp_path):
    (tmp_path / ".gitignore").write_text("node_modules", encoding="utf-8")
    assert config.ensure_gitignore(tmp_path) is True
    assert config.ensure_gitignore(tmp_path) is False
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == "node_modules\n.metrics/\n"


def test_store_secrets_ignores_first_and_env_wins(tmp_path, monkeypatch):
    cfg = config.store_secrets(tmp_path, config.load_config(tmp_path), hf_token="file-token", groq_api_key=None)
    assert config.is_metrics_ignored(tmp_path)
    assert config.get_secret(config.load_config(tmp_path), "hf_token") == "file-token"
    assert config.shadowed_secrets(cfg) == []
    monkeypatch.setenv("HF_TOKEN", "env-token")
    assert config.get_secret(cfg, "hf_token") == "env-token"
    assert config.shadowed_secrets(cfg) == ["HF_TOKEN"]


def test_find_root_walks_up(tmp_path):
    (tmp_path / ".metrics").mkdir()
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert config.find_root(nested) == tmp_path.resolve()


@pytest.mark.parametrize("old_endpoint,old_model", [
    ("https://jevmodel.net/v1/systemone", "typesafe/jev-1.13.0"),     # first release default
    ("https://api.typesafe.ai/v1/systemone", "jev-1.13.0"),           # previous persisted default
])
def test_legacy_jev_defaults_are_upgraded_on_load(tmp_path, old_endpoint, old_model):
    cfg = config.load_config(tmp_path)
    cfg["jev"].update(endpoint=old_endpoint, model=old_model, faithful_threshold=0.75)
    config.save_config(tmp_path, cfg)
    loaded = config.load_config(tmp_path)["jev"]
    assert (loaded["endpoint"], loaded["model"], loaded["faithful_threshold"]) == \
        ("https://api.codiv.ai/v1/systemone", "openjev-latest", 0.80)


def test_jev_target_routes_typesafe_key_to_typesafe_else_openjev(monkeypatch, tmp_path):
    cfg = config.load_config(tmp_path)
    assert config.jev_target(cfg) == ("https://api.codiv.ai/v1/systemone", "openjev-latest", "", "openjev")
    monkeypatch.setenv("JEV_API_KEY", "oj")
    assert config.jev_target(cfg)[2:] == ("oj", "openjev")
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts")
    assert config.jev_target(cfg) == ("https://api.typesafe.ai/v1/systemone", "jev-1.13.0", "ts", "typesafe")
