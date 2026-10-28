"""Deterministic local heuristic gate used when Jev is unconfigured or unreachable."""

from __future__ import annotations

import json
import re
from typing import Any

from metrics_ai.prose import prose_defects

ROLE_KEYWORDS = {
    "aiml": ["torch", "tensorflow", "numpy", "embedding", "inference", "tensor", "sklearn",
             "transformers", "llm", "train", "model"],
    "backend": ["sqlite", "sql", "executemany", "execute", "commit", "cursor", "database", "http",
                "api", "request", "server", "cache", "storage", "ledger", "fastapi", "query"],
    "mlops": ["benchmark", "bench", "profile", "pytest", "ci", "docker", "deploy", "workflow",
              "pipeline", "monitor", "telemetry", "metrics", "action"],
    "tpm": ["readme", "docs", ".md", "config", ".toml", ".yml", "changelog", "roadmap"],
}

# Claimed mechanism (regex over the Z clause) -> evidence substrings that must appear in the diff.
MECHANISMS = {
    r"executemany": ["executemany"],
    r"sqlite|sql\b|database": ["sqlite", "execute", "connect", "storage", "ledger"],
    r"lru_cache|memoi[sz]|cach(e|ed|ing)": ["lru_cache", "cache", "cached"],
    r"batch(ed|ing|es)?|bulk": ["executemany", "batch", "bulk"],
    r"async|asyncio|await": ["async", "await", "gather", "create_task"],
    r"thread(s|ed|ing)?|concurren": ["threadpoolexecutor", "thread", "gather"],
    r"multiprocess|process pool": ["processpoolexecutor", "pool"],
    r"redis": ["redis"], r"postgres": ["postgres", "psycopg"], r"mysql": ["mysql"],
    r"mongo": ["mongo"], r"kafka": ["kafka"], r"pandas": ["pandas"],
    r"numpy|vectori[sz]": ["numpy", "np."],
    r"transaction": ["executemany", "commit", "connect", "transaction", "executescript"],
    r"\bast\b|syntax tree": ["ast", "parse", "profile_source"],
    r"connection pool": ["pool"],
    r"generator": ["yield"],
}
NOISE_FLOOR_PCT = 5.0
IDENTIFIER = re.compile(r"`([^`]+)`|\b([A-Za-z]+_[A-Za-z0-9_.]+|[a-z]+[A-Z][A-Za-z]+)\b(?:\(\))?")


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _neg(value: Any) -> float | None:
    v = _num(value)
    return -v if v is not None else None


def triage(state: dict[str, Any]) -> dict[str, Any]:
    delta = state["telemetry_delta"]
    diff = state["ast_structural_diff"]
    signals = diff.get("signals", [])
    code_change = bool(signals) or any(k != "signals" for k in diff)
    latency_red = _neg(delta.get("latency_delta_pct"))
    throughput_gain = _num(delta.get("throughput_delta_pct"))
    gains = [g for g in (latency_red, _neg(delta.get("memory_delta_pct")), throughput_gain,
                         _neg(delta.get("complexity_delta_pct"))) if g is not None]
    best = max(gains, default=0.0)
    regression = (latency_red is not None and latency_red < -5.0) or \
        (throughput_gain is not None and throughput_gain < -5.0)

    if not code_change:
        worth = 0.08
    elif regression:
        worth = 0.15
    else:
        # Gains under the 5% noise floor earn nothing; ~15%+ plus structural signals is needed to pass.
        worth = 0.25 + 0.50 * min(max(best - NOISE_FLOOR_PCT, 0.0) / 30.0, 1.0) + 0.06 * min(len(signals), 3)
    worth = round(min(max(worth, 0.02), 0.97), 3)

    if regression:
        tier = 1
    elif (latency_red or 0) > 75 or (throughput_gain or 0) > 200:
        tier = 5
    else:
        tier = 4 if best >= 35 else 3 if best >= 15 else 2 if best >= 5 else 1
    return {"worth_prob": worth, "impact_tier": tier, "impact_score": float(tier),
            "role_distribution": role_distribution(state)}


def role_distribution(state: dict[str, Any]) -> dict[str, float]:
    corpus = json.dumps({"files": state.get("files_modified", []),
                         "diff": state.get("ast_structural_diff", {}),
                         "command": state.get("command") or ""}).lower()
    scores = {role: 0.5 + sum(corpus.count(k) for k in kws) ** 0.5 for role, kws in ROLE_KEYWORDS.items()}
    total = sum(scores.values())
    return {role: round(s / total, 3) for role, s in scores.items()}


def faithfulness(evidence: dict[str, Any], z_clause: str) -> float:
    """Fraction of mechanisms claimed in Z that are evidenced by the AST diff / files."""
    # The candidate itself is never evidence for its own claims.
    haystack = json.dumps({k: v for k, v in evidence.items() if k != "candidate_bullet"}).lower()
    z = z_clause.lower()
    claimed: list[list[str]] = [alts for pattern, alts in MECHANISMS.items() if re.search(pattern, z)]
    for match in IDENTIFIER.finditer(z_clause):
        ident = (match.group(1) or match.group(2) or "").strip().lower().rstrip("()")
        if ident:
            claimed.append([ident.rsplit("/", 1)[-1]])
    if not claimed:
        return 0.80
    found = sum(1 for alts in claimed if any(a in haystack for a in alts))
    return round(0.97 * found / len(claimed), 3)


def naturalness(candidate: str) -> tuple[float, str]:
    """Deterministic fluency proxy: the prose linter's collision patterns."""
    defects = prose_defects(candidate)
    if defects:
        return 0.2, f"awkward phrasing {defects}"
    return 0.9, ""
