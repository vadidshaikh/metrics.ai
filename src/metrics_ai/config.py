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

# Environment variables always win over values persisted in config.toml (first match wins).
SECRET_ENV = {
    "groq_api_key": ("GROQ_API_KEY",),
    "hf_token": ("HF_TOKEN",),
    "jev_api_key": ("JEV_API_KEY",),            # OpenJev (codiv.ai) or any SystemOne-compatible server
    "typesafe_api_key": ("TYPESAFE_API_KEY",),  # commercial TypeSafe Jev
}

OPENJEV_ENDPOINT, OPENJEV_MODEL = "https://api.codiv.ai/v1/systemone", "openjev-latest"
TYPESAFE_ENDPOINT, TYPESAFE_MODEL = "https://api.typesafe.ai/v1/systemone", "jev-1.13.0"

DEFAULT_CONFIG: dict[str, Any] = {
    "jev": {
        "endpoint": OPENJEV_ENDPOINT,
        "model": OPENJEV_MODEL,
        "typesafe_endpoint": TYPESAFE_ENDPOINT,
        "typesafe_model": TYPESAFE_MODEL,
        "timeout_s": 30.0,
        "worthy_threshold": 0.60,
        "faithful_threshold": 0.80,
        "metric_threshold": 0.80,
        "natural_threshold": 0.80,
    },
    "providers": {
        "prefer": "",
        "ollama_url": "http://localhost:11434",
    },
    "secrets": {"groq_api_key": "", "hf_token": "", "jev_api_key": "", "typesafe_api_key": ""},
    "router": {
        "active_provider": "",
        "active_model": "",
        "fallback_models": [],
        "discovered_at": 0,
    },
}


# Old defaults persisted by earlier versions; upgraded transparently on load.
LEGACY_DEFAULTS = {
    # The generic slot now defaults to OpenJev; TypeSafe has its own typesafe_* settings.
    ("jev", "endpoint"): (("https://jevmodel.net/v1/systemone", TYPESAFE_ENDPOINT), OPENJEV_ENDPOINT),
    ("jev", "model"): (("typesafe/jev-1.13.0", TYPESAFE_MODEL), OPENJEV_MODEL),
    ("jev", "faithful_threshold"): ((0.75,), 0.80),
    ("jev", "timeout_s"): ((8.0,), 30.0),
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
        cfg = _merge(DEFAULT_CONFIG, tomllib.load(fh))
    for (section, key), (olds, new) in LEGACY_DEFAULTS.items():
        if cfg.get(section, {}).get(key) in olds:
            cfg[section][key] = new
    return cfg


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


def _env_secret(name: str) -> str:
    return next((os.environ[v].strip() for v in SECRET_ENV[name] if os.environ.get(v, "").strip()), "")


def get_secret(cfg: dict[str, Any], name: str) -> str:
    env_value = _env_secret(name)
    if env_value:
        return env_value
    return str(cfg.get("secrets", {}).get(name, "") or "").strip()


def jev_target(cfg: dict[str, Any]) -> tuple[str, str, str, str]:
    """(endpoint, model, api_key, label) for the Tier-1 SystemOne gate.

    A TypeSafe key routes to TypeSafe Jev; otherwise the configured (default OpenJev) endpoint is used.
    An empty api_key means Tier 1 is unavailable.
    """
    jev = cfg.get("jev", {})
    typesafe_key = get_secret(cfg, "typesafe_api_key")
    if typesafe_key:
        return (jev.get("typesafe_endpoint", TYPESAFE_ENDPOINT), jev.get("typesafe_model", TYPESAFE_MODEL),
                typesafe_key, "typesafe")
    endpoint = jev.get("endpoint", OPENJEV_ENDPOINT)
    label = "openjev" if "codiv.ai" in endpoint else "systemone"
    return endpoint, jev.get("model", OPENJEV_MODEL), get_secret(cfg, "jev_api_key"), label


def shadowed_secrets(cfg: dict[str, Any]) -> list[str]:
    """Env vars that override a *different* key stored in config.toml."""
    stored = cfg.get("secrets", {})
    shadowed = []
    for name, envs in SECRET_ENV.items():
        active = next((v for v in envs if os.environ.get(v, "").strip()), None)
        if active and stored.get(name) and os.environ[active].strip() != stored[name]:
            shadowed.append(active)
    return shadowed


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
