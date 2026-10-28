"""Prompt assembly, structured LLM execution, numeric/prose guard retries and Stage-3 re-prompting."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import ValidationError

from metrics_ai import model_router
from metrics_ai.collector import state_identifiers
from metrics_ai.decision_gate import DecisionGate
from metrics_ai.model_router import parse_json
from metrics_ai.renderers import render_star, render_xyz
from metrics_ai.schemas import DENSITY_RANGES, StarResponse, XyzResponse

MAX_NUMERIC_RETRIES = 3
MAX_REVIEW_RETRIES = 2
XYZ_OVERHEAD = len("Accomplished  as measured by , by .")

ROLE_LENS = {
    "aiml": "an AI/ML engineer (data and model pipeline efficiency, inference/runtime cost)",
    "backend": "a backend engineer (storage, database access patterns, latency, resource usage)",
    "mlops": "an MLOps/platform engineer (benchmarking, profiling, reproducible telemetry, tooling)",
    "tpm": "a technical program manager (measurable delivery outcome, risk reduction, verification)",
}

SYSTEM_PROMPT = """You convert measured engineering telemetry into resume/interview artifacts.
HARD RULES:
1. Use ONLY numbers listed in ALLOWED_NUMBERS, written exactly as listed (same decimals). Never round,
   never compute new percentages or ratios, never add counts, versions, dates or years.
2. Write numbers without thousands separators. A speedup is written like `4.2x`.
3. Every library, database, algorithm or mechanism you mention must appear in AST_DIFF or FILES.
4. Write fluent executive English: parallel structure, no stacked prepositions, no run-ons.
5. Respond with a single JSON object and nothing else."""

XYZ_SHAPE = '{"x_accomplishment": str, "y_measurement": str, "z_mechanism": str, "cited_numbers": [number]}'
STAR_SHAPE = '{"situation": str, "task": str, "action": str, "result": str, "cited_numbers": [number]}'


class SynthesisError(RuntimeError):
    pass


@dataclass
class SynthesisResult:
    provider: str
    model: str
    output_text: str
    structured: dict[str, Any]
    faithful_prob: float
    natural_prob: float
    gate_mode: str
    critique: str
    attempts: int
    errors: list[str] = field(default_factory=list)


def fmt_number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}"


def _xyz_task(density: str) -> str:
    lo, hi = DENSITY_RANGES[density]
    return (
        "Write one Google XYZ resume bullet as three parts; Python assembles them as\n"
        "  'Accomplished {x_accomplishment} as measured by {y_measurement}, by {z_mechanism}.'\n"
        "- x_accomplishment: a NOUN PHRASE (e.g. 'a 95.3% reduction in ingest latency'); do not start with "
        "a verb or with 'Accomplished'.\n"
        "- y_measurement: what was measured and how (do not start with 'as measured by').\n"
        "- z_mechanism: starts with a lowercase gerund verb (e.g. 'batching ...'); never start with 'by' or "
        "'doing'. Write each MECHANISMS item you use as its own parallel gerund clause, joined with 'and'.\n"
        "- Name at least one exact identifier from IDENTIFIERS.\n"
        f"- The assembled sentence MUST be {lo}-{hi} characters, so the three parts together need "
        f"{lo - XYZ_OVERHEAD}-{hi - XYZ_OVERHEAD} characters.\n"
        f"Return JSON shaped {XYZ_SHAPE}.")


def build_messages(state: dict[str, Any], allowed: set[float], fmt: str, role: str,
                   density: str) -> list[dict[str, str]]:
    diff = state["ast_structural_diff"]
    if fmt == "xyz":
        task = _xyz_task(density)
    else:
        task = ("Write a STAR behavioral interview answer grounded in the exact file names, AST diff and "
                "benchmark deltas. Describe each MECHANISMS item as a separate step in ACTION. "
                f"Return JSON shaped {STAR_SHAPE}.")
    payload = {
        "ROLE_LENS": ROLE_LENS[role],
        "ALLOWED_NUMBERS": [fmt_number(n) for n in sorted(allowed)],
        "MECHANISMS": diff.get("mechanisms", []),
        "IDENTIFIERS": sorted(state_identifiers(state)),
        "FILES": state["files_modified"],
        "COMMAND": state.get("command"),
        "AST_DIFF": diff,
        "COMPLEXITY": state["complexity_delta"],
        "TELEMETRY": state["telemetry_delta"],
        "NOTE": "Negative *_delta_pct means the value went down (lower latency/memory is better).",
    }
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Write for {ROLE_LENS[role]}.\n{task}\n\n"
                                        f"{json.dumps(payload, indent=1)}"}]


def ledger_hash(state: dict[str, Any], output_text: str) -> str:
    blob = json.dumps(state, sort_keys=True, default=str) + "\n" + output_text
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def validation_context(state: dict[str, Any], allowed: set[float], fmt: str, density: str) -> dict[str, Any]:
    return {"allowed_numbers": allowed, "density": density if fmt == "xyz" else None,
            "identifiers": state_identifiers(state),
            "mechanism_count": len(state["ast_structural_diff"].get("mechanisms", []))}


def synthesize(cfg: dict[str, Any], gate: DecisionGate, state: dict[str, Any], allowed: set[float],
               fmt: str, role: str, density: str, chat: Callable[..., tuple[str, str, str, int]] | None = None,
               log: Callable[[str], None] = lambda _m: None) -> SynthesisResult:
    """Generate one artifact: numeric/prose guard (3 retries), Stage-3 review (2 retries), model failover."""
    chat = chat or model_router.chat
    schema = XyzResponse if fmt == "xyz" else StarResponse
    context = validation_context(state, allowed, fmt, density)
    jev = cfg.get("jev", {})
    f_min, n_min = float(jev.get("faithful_threshold", 0.80)), float(jev.get("natural_threshold", 0.80))
    chain_len = max(1, len(model_router.model_chain(cfg)))
    errors: list[str] = []
    attempts = 0
    index = 0
    while index < chain_len:
        messages = build_messages(state, allowed, fmt, role, density)
        guard_retries = review_retries = 0
        while True:
            attempts += 1
            provider, model, text, index = chat(cfg, messages, start_index=index)
            messages.append({"role": "assistant", "content": text})
            try:
                obj = schema.model_validate(parse_json(text), context=context)
            except (ValidationError, json.JSONDecodeError) as exc:
                guard_retries += 1
                errors.append(f"{model}: {str(exc).splitlines()[0][:200]}")
                log(f"[validation] {model} attempt rejected: {str(exc)[:400]}")
                if guard_retries > MAX_NUMERIC_RETRIES:
                    break
                messages.append({"role": "user", "content": f"Your JSON failed validation:\n{exc}\n"
                                 "Fix every listed problem. Use ONLY ALLOWED_NUMBERS verbatim; JSON only."})
                continue
            z = obj.z_mechanism if fmt == "xyz" else obj.action
            candidate = obj.combined_xyz_bullet if fmt == "xyz" else render_star(obj)
            review = gate.review(state, z, candidate)
            if review.faithful_prob < f_min or review.natural_prob < n_min:
                review_retries += 1
                errors.append(f"{model}: review faithful={review.faithful_prob:.2f} natural={review.natural_prob:.2f}")
                log(f"[review:{review.gate_mode}] {model} faithful={review.faithful_prob:.2f} "
                    f"natural={review.natural_prob:.2f}: {review.critique[:300]}")
                if review_retries > MAX_REVIEW_RETRIES:
                    break
                messages.append({"role": "user", "content": (
                    f"Rejected by the {review.gate_mode} review gate (faithful={review.faithful_prob:.2f}, "
                    f"natural={review.natural_prob:.2f}; both must be >= {f_min:.2f}/{n_min:.2f}). "
                    f"Critique: {review.critique or 'n/a'}\nRewrite: claim only mechanisms in AST_DIFF, use "
                    "parallel gerund clauses joined with 'and', fluent prose. JSON only.")})
                continue
            output = render_xyz(obj) if fmt == "xyz" else render_star(obj)
            return SynthesisResult(provider, model, output, obj.model_dump(), review.faithful_prob,
                                   review.natural_prob, review.gate_mode, review.critique, attempts, errors)
        index += 1
        log(f"[failover] exhausted retries on {model}; trying next fallback model")
    raise SynthesisError("No model produced a validated, faithful artifact: " + " | ".join(errors[-6:]))
