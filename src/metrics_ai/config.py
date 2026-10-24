"""`.metrics/config.toml` management, API key storage and `.gitignore` safety."""

from __future__ import annotations

import copy
import os
import sys
from pathlib import Path
from typing import Any

import tomli_w

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

METRICS_DIR = ".metrics"
CONFIG_FILE = "config.toml"
LEDGER_FILE = "ledger.db"
GITIGNORE_ENTRY = ".metrics/"

# Environment variables always win over values persisted in config.toml.
SECRET_ENV = {
    "groq_api_key": "GROQ_API_KEY",
    "hf_token": "HF_TOKEN",
    "jev_api_key": "JEV_API_KEY",
}

DEFAULT_CONFIG: dict[str, Any] = {
    "jev": {
        "endpoint": "https://jevmodel.net/v1/systemone",
        "model": "typesafe/jev-1.13.0",
        "timeout_s": 8.0,
        "worthy_threshold": 0.60,
        "faithful_threshold": 0.75,
    },
    "providers": {
        "prefer": "",
        "ollama_url": "http://localhost:11434",
    },
    "secrets": {"groq_api_key": "", "hf_token": "", "jev_api_key": ""},
    "router": {
        "active_provider": "",
        "active_model": "",
        "fallback_models": [],
        "discovered_at": 0,
    },
}


def find_root(start: Path | None = None) -> Path:
    """Walk upwards to the nearest dir holding `.metrics/` or `.git/`."""
    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / METRICS_DIR).is_dir() or (candidate / ".git").exists():
            return candidate
    return here


def metrics_dir(root: Path) -> Path:
    return root / METRICS_DIR


def ledger_path(root: Path) -> Path:
    return metrics_dir(root) / LEDGER_FILE


def config_path(root: Path) -> Path:
    return metrics_dir(root) / CONFIG_FILE


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(root: Path) -> dict[str, Any]:
    path = config_path(root)
    if not path.exists():
        return copy.deepcopy(DEFAULT_CONFIG)
    with path.open("rb") as fh:
        return _merge(DEFAULT_CONFIG, tomllib.load(fh))


def save_config(root: Path, cfg: dict[str, Any]) -> Path:
    path = config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        tomli_w.dump(cfg, fh)
    return path


def is_metrics_ignored(root: Path) -> bool:
    gitignore = root / ".gitignore"
    if not gitignore.exists():
        return False
    entries = {line.strip() for line in gitignore.read_text(encoding="utf-8").splitlines()}
    return bool(entries & {".metrics", ".metrics/", "/.metrics", "/.metrics/"})


def ensure_gitignore(root: Path) -> bool:
    """Append `.metrics/` to `.gitignore`. Returns True when a line was added."""
    if is_metrics_ignored(root):
        return False
    gitignore = root / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    gitignore.write_text(f"{existing}{prefix}{GITIGNORE_ENTRY}\n", encoding="utf-8")
    return True


def get_secret(cfg: dict[str, Any], name: str) -> str:
    env_value = os.environ.get(SECRET_ENV[name], "").strip()
    if env_value:
        return env_value
    return str(cfg.get("secrets", {}).get(name, "") or "").strip()


def shadowed_secrets(cfg: dict[str, Any]) -> list[str]:
    """Env vars that override a *different* key stored in config.toml."""
    stored = cfg.get("secrets", {})
    return [env for name, env in SECRET_ENV.items()
            if os.environ.get(env, "").strip() and stored.get(name)
            and os.environ[env].strip() != stored[name]]


def store_secrets(root: Path, cfg: dict[str, Any], **secrets: str | None) -> dict[str, Any]:
    """Persist API keys, refusing unless `.metrics/` is git-ignored first."""
    provided = {k: v for k, v in secrets.items() if v}
    if not provided:
        return cfg
    ensure_gitignore(root)
    if not is_metrics_ignored(root):  # pragma: no cover - defensive
        raise RuntimeError("Refusing to store secrets: .metrics/ is not git-ignored.")
    for key, value in provided.items():
        if key not in SECRET_ENV:
            raise KeyError(f"Unknown secret {key!r}")
        cfg.setdefault("secrets", {})[key] = value
    save_config(root, cfg)
    return cfg
