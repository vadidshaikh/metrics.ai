"""Role differentiation: per-role lenses, data-driven lens requirements, and the cross-role clone check."""

from __future__ import annotations

import difflib
import re
from typing import Any, Callable

# Within one run, a role's [X] and [Z] must each stay below this similarity to every other role's.
CLONE_LIMITS = {"x": 0.70, "z": 0.70, "text": 0.80}

# Each lens names a *facet* to lead with; the numbers always come from the measured state.
ROLE_LENS = {
    "aiml": "an AI/ML engineer. Lead with the data-processing side: caching or memoizing repeated parsing / "
            "feature extraction and removing redundant per-record work. [X] names the parsing/profiling "
            "pipeline component that got faster.",
    "backend": "a backend engineer. Lead with persistence architecture: batched/bulk database writes in one "
               "transaction replacing per-row writes, and hoisting loop-invariant calls. [X] names the "
               "storage/write path that got faster.",
    "mlops": "an MLOps/platform engineer. Lead with the profiled benchmark runtime: the speedup multiplier "
             "together with the peak-memory (RSS) change and the complexity trend. [X] names the benchmark.",
    "tpm": "a technical program manager. Lead with the end-to-end outcome: the latency reduction percentage "
           "with its before/after values, framed as delivery impact and maintainability. [X] names the "
           "end-to-end workflow. Never claim an SLA, target or deadline was defined or met; none is in the data.",
}

SLA_CLAIM_RE = re.compile(r"\b(meet|meets|meeting|met|hit|hitting|achiev\w*)\s+(an?\s+|the\s+)?(sla|slo|target)",
                          re.IGNORECASE)


def fmt_number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}"


def clone_key(structured: dict[str, Any]) -> dict[str, str]:
    """The parts that must differ between roles. XYZ: [X] and [Z], each compared on its own ([Y] repeats
    the same measured before/after values for every role, so it is excluded). STAR: the whole answer."""
    if "x_accomplishment" in structured:
        return {"x": structured["x_accomplishment"], "z": structured["z_mechanism"]}
    return {"text": " ".join(str(structured.get(k, "")) for k in ("situation", "task", "action", "result"))}


def _ratio(a: str, b: str) -> float:
    """Symmetric similarity: SequenceMatcher.ratio() depends on argument order, so take the larger."""
    a, b = a.lower(), b.lower()
    return max(difflib.SequenceMatcher(None, a, b).ratio(), difflib.SequenceMatcher(None, b, a).ratio())


def too_similar(parts: dict[str, str], others: dict[str, dict[str, str]]) -> tuple[str, str, float] | None:
    """(other_role, part, similarity) for the worst part at or above its limit, else None."""
    worst = None
    for role, other in others.items():
        for part, text in parts.items():
            if part in other:
                ratio = _ratio(text, other[part])
                if ratio >= CLONE_LIMITS[part] and (worst is None or ratio > worst[2]):
                    worst = (role, part, ratio)
    return worst


def _first_clause(z: str) -> str:
    return re.split(r",|\band\b|\bwhile\b", z, maxsplit=1)[0].lower()


def _has_number(text: str, value: str) -> bool:
    return bool(re.search(rf"(?<![\d.]){re.escape(value)}(?!\d)", text))


# Component vocabulary each lens must name in [X] (role-generic words, never data values).
X_COMPONENTS = {
    "aiml": ("pars", "profil", "ast", "feature", "extraction", "cach", "memo"),
    "backend": ("storage", "write", "persist", "ledger", "database", "sqlite", "transaction", "insert"),
}


NUMBER_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_.])(\d+(?:\.\d+)?)(?!\.?\d)")


def _units_ok(combined: str, values: list[float], unit: str) -> bool:
    """Every cited number equal to a measured value of this kind carries its unit ('3837.7 ms', '216.0 ms')."""
    for match in NUMBER_TOKEN_RE.finditer(combined):
        if any(abs(float(match.group(1)) - v) < 1e-9 for v in values):
            if not re.match(rf"\s?{unit}\b", combined[match.end():], re.IGNORECASE):
                return False
    return True


def lens_requirements(role: str, state: dict[str, Any]) -> list[tuple[str, Callable[[str, str, str], bool]]]:
    """Data-driven role contract as (instruction, check(x, z, combined)) pairs; values come from the state."""
    diff, delta = state["ast_structural_diff"], state["telemetry_delta"]
    reqs: list[tuple[str, Callable[[str, str, str], bool]]] = []
    latency = [float(delta[s]["mean_latency_ms"]) for s in ("baseline", "target")
               if delta.get(s, {}).get("mean_latency_ms") is not None]
    memory = [float(delta[s]["peak_memory_mb"]) for s in ("baseline", "target")
              if delta.get(s, {}).get("peak_memory_mb") is not None]
    if latency:
        reqs.append((f"write latency values with 'ms' (e.g. '{fmt_number(latency[0])} ms')",
                     lambda x, z, c: _units_ok(c, latency, "ms")))
    if memory:
        reqs.append(("write memory values with 'MB'", lambda x, z, c: _units_ok(c, memory, "MB")))
    speedup = f"{fmt_number(delta['speedup_multiplier_x'])}x" if delta.get("speedup_multiplier_x") is not None else ""
    if role in X_COMPONENTS:
        words = X_COMPONENTS[role]
        reqs.append((f"[X] names the {role} component (a word like: {', '.join(words[:4])})",
                     lambda x, z, c: any(w in x.lower() for w in words)))
    if speedup and role in ("aiml", "backend", "tpm"):
        reqs.append((f"[X] does not use the '{speedup}' multiplier (reserved for the mlops bullet)",
                     lambda x, z, c: speedup not in x.lower()))
    if role == "aiml":
        caching = sorted(set(diff.get("caching_decorators_added", [])) | {
            d.split("::")[-1].split("@")[0].split(".")[-1] for d in diff.get("decorators_added", [])})
        if caching:
            reqs.append((f"[Z]'s FIRST clause is the caching change, naming {' or '.join(caching)}",
                         lambda x, z, c: any(n.lower() in _first_clause(z) for n in caching)))
    elif role == "backend":
        batching = diff.get("batching_calls_added", [])
        if batching:
            reqs.append((f"[Z]'s FIRST clause is the batched-write change, naming {' or '.join(batching)}",
                         lambda x, z, c: any(b.lower() in _first_clause(z) for b in batching)))
    elif role == "mlops":
        if delta.get("speedup_multiplier_x") is not None:
            speedup = f"{fmt_number(delta['speedup_multiplier_x'])}x"
            reqs.append((f"[X] contains the measured '{speedup}' speedup", lambda x, z, c: speedup in x.lower()))
        if delta.get("memory_delta_pct") is not None:
            pct, mb = fmt_number(abs(delta["memory_delta_pct"])), fmt_number(delta["target"]["peak_memory_mb"])
            reqs.append((f"cite the peak-memory change as '{pct}%' or '{mb} MB' (e.g. in [Y]: '... with peak RSS "
                         f"down {pct}%')", lambda x, z, c: _has_number(c, pct) or _has_number(c, mb)))
    elif role == "tpm":
        if delta.get("latency_delta_pct") is not None:
            pct = f"{fmt_number(abs(delta['latency_delta_pct']))}%"
            reqs.append((f"[X] contains the end-to-end '{pct}' latency reduction", lambda x, z, c: pct in x))
        reqs.append(("never claim an SLA/target was met", lambda x, z, c: not SLA_CLAIM_RE.search(c)))
    return reqs


def lens_violations(role: str, x: str, z: str, combined: str, state: dict[str, Any]) -> list[str]:
    return [f"{role} lens: {text}" for text, ok in lens_requirements(role, state) if not ok(x, z, combined)]
