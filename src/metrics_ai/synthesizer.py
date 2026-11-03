"""Role-differentiated prompt assembly, structured LLM execution, guard retries and Stage-3 re-prompting."""

from __future__ import annotations

import difflib
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import ValidationError

from metrics_ai import model_router
from metrics_ai.collector import state_identifiers
from metrics_ai.decision_gate import DecisionGate, ReviewResult
from metrics_ai.model_router import parse_json
from metrics_ai.prose import unmeasured_terms
from metrics_ai.renderers import render_star, render_xyz
from metrics_ai.schemas import DENSITY_RANGES, StarResponse, XyzResponse

MAX_GUARD_RETRIES = 3
MAX_REVIEW_RETRIES = 2
CLONE_SIMILARITY = 0.80  # a role's bullet may not be this similar to another role's bullet in the same run
XYZ_OVERHEAD = len("Accomplished  as measured by , by .")

# Each lens names a *facet* to lead with; the numbers always come from the measured state.
ROLE_LENS = {
    "aiml": "an AI/ML engineer. Lead with the data-processing side: caching or memoizing repeated parsing / "
            "feature extraction and removing redundant per-record work in the pipeline.",
    "backend": "a backend engineer. Lead with persistence architecture: batched/bulk database writes in one "
               "transaction replacing per-row writes, and hoisting loop-invariant calls.",
    "mlops": "an MLOps/platform engineer. Lead with the profiled benchmark runtime: the speedup multiplier "
             "together with the peak-memory (RSS) change and the complexity trend.",
    "tpm": "a technical program manager. Lead with the end-to-end outcome: the latency reduction percentage "
           "with its before/after values, framed as delivery/SLA impact and maintainability.",
}

SYSTEM_PROMPT = """You convert measured engineering telemetry into resume/interview artifacts.
HARD RULES:
1. Use ONLY numbers listed in ALLOWED_NUMBERS, written exactly as listed (same decimals). Never round,
   never compute new percentages or ratios, never add counts, versions, dates or years.
2. Write numbers without thousands separators. Multipliers (e.g. 4.2x) pair only with speedup /
   acceleration ("a 4.2x speedup"); percentages pair with reduction / drop. Never "4.2x reduction".
3. Every library, database, algorithm or mechanism you mention must appear in AST_DIFF or FILES.
4. Write fluent executive English: parallel structure, no stacked prepositions, no run-ons.
   State reductions as positive magnitudes ("a 95.7% reduction"), never "-95.7%".
   Mention only MEASURED_METRICS; never any of UNMEASURED_METRICS. Never draw a conclusion the numbers
   contradict (a positive loc_delta means the code grew).
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
    review: ReviewResult
    attempts: int
    errors: list[str] = field(default_factory=list)


def fmt_number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}"


def _xyz_task(density: str) -> str:
    lo, hi = DENSITY_RANGES[density]
    return (
        "Write one Google XYZ resume bullet as three parts; Python assembles them as\n"
        "  'Accomplished {x_accomplishment} as measured by {y_measurement}, by {z_mechanism}.'\n"
        "- x_accomplishment: a NOUN PHRASE naming the component/system and its impact (e.g. 'a 95.3% "
        "reduction in ingest latency' or 'a 4.2x benchmark speedup'); no leading verb, no 'Accomplished'.\n"
        "- y_measurement: a NOUN PHRASE giving the before-and-after measurement (e.g. 'mean latency dropping "
        "from A ms to B ms'); no finite verbs like 'fell'/'was'; do not start with 'as measured by'.\n"
        "- z_mechanism: starts with a lowercase gerund verb (e.g. 'batching ...'); never start with 'by' or "
        "'doing'. Write each MECHANISMS item you use as its own parallel gerund clause, joined with 'and'.\n"
        "- Name at least one exact identifier from IDENTIFIERS, written plainly (no '()' or '@'). Do not "
        "repeat the same number in X and Y.\n"
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
                "benchmark deltas. SITUATION and TASK describe only what COMMAND and TELEMETRY show (the measured "
                "workload, its baseline numbers and benchmark_iterations); do not invent services, users, teams, "
                "APIs, deadlines or downstream impact. Describe each MECHANISMS item as a separate step in ACTION. "
                f"Return JSON shaped {STAR_SHAPE}.")
    payload = {
        "ROLE_LENS": ROLE_LENS[role],
        "ALLOWED_NUMBERS": [fmt_number(n) for n in sorted(allowed)],
        "MEASURED_METRICS": state.get("measured_metrics", []),
        "UNMEASURED_METRICS": state.get("unmeasured_metrics", []),
        "MECHANISMS": diff.get("mechanisms", []),
        "IDENTIFIERS": sorted(state_identifiers(state)),
        "FILES": state["files_modified"],
        "COMMAND": state.get("command"),
        "AST_DIFF": diff,
        "COMPLEXITY": state["complexity_delta"],
        "TELEMETRY": state["telemetry_delta"],
        "NOTE": "Negative *_delta_pct means the value went down (lower latency/memory is better). "
                "benchmark_iterations is how many timed runs each mean aggregates.",
    }
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Write for {ROLE_LENS[role]}\n{task}\n\n{json.dumps(payload, indent=1)}"}]


def ledger_hash(state: dict[str, Any], output_text: str) -> str:
    blob = json.dumps(state, sort_keys=True, default=str) + "\n" + output_text
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def validation_context(state: dict[str, Any], allowed: set[float], fmt: str, density: str) -> dict[str, Any]:
    return {"allowed_numbers": allowed, "density": density if fmt == "xyz" else None,
            "identifiers": state_identifiers(state),
            "mechanisms": list(state["ast_structural_diff"].get("mechanisms", [])),
            "unmeasured": unmeasured_terms(state.get("unmeasured_metrics", []))}


def too_similar(candidate: str, others: dict[str, str]) -> tuple[str, float] | None:
    """The other role whose bullet this candidate nearly clones, if any."""
    best = max(((role, difflib.SequenceMatcher(None, candidate.lower(), text.lower()).ratio())
                for role, text in others.items()), key=lambda rt: rt[1], default=None)
    return best if best and best[1] >= CLONE_SIMILARITY else None


def review_feedback(review: ReviewResult) -> str:
    return (f"Rejected by the {review.gate_mode} Stage-3 review (faithful={review.faithful_prob:.2f}, "
            f"metric={review.metric_prob:.2f}, natural={review.natural_prob:.2f}; each must be >= 0.80).\n"
            f"Fix: {review.critique or 'rewrite more plainly'}\nRewrite. JSON only.")


def synthesize(cfg: dict[str, Any], gate: DecisionGate, state: dict[str, Any], allowed: set[float],
               fmt: str, role: str, density: str, chat: Callable[..., tuple[str, str, str, int]] | None = None,
               log: Callable[[str], None] = lambda _m: None, others: dict[str, str] | None = None) -> SynthesisResult:
    """One artifact: numeric/prose/clone guard (3 retries), Stage-3 review (2 retries), model failover.

    `others` maps role -> bullet already produced in this run, so roles never receive cloned bullets.
    """
    chat = chat or model_router.chat
    schema = XyzResponse if fmt == "xyz" else StarResponse
    context = validation_context(state, allowed, fmt, density)
    others = others or {}
    chain_len = max(1, len(model_router.model_chain(cfg)))
    errors: list[str] = []
    attempts = index = 0
    while index < chain_len:
        messages = build_messages(state, allowed, fmt, role, density)
        guard_retries = review_retries = 0
        while True:
            attempts += 1
            provider, model, text, index = chat(cfg, messages, start_index=index)
            messages.append({"role": "assistant", "content": text})
            try:
                obj = schema.model_validate(parse_json(text), context=context)
                candidate = obj.combined_xyz_bullet if fmt == "xyz" else render_star(obj)
                clone = too_similar(candidate, others)
                if clone:
                    raise ValueError(f"bullet is {clone[1]:.0%} similar to the {clone[0]} bullet "
                                     f"'{others[clone[0]]}'. Lead with your own ROLE_LENS facet instead.")
            except (ValidationError, ValueError) as exc:  # JSONDecodeError is a ValueError
                guard_retries += 1
                errors.append(f"{model}: {str(exc).splitlines()[0][:200]}")
                log(f"[validation] {model} attempt rejected: {str(exc)[:400]}")
                if guard_retries > MAX_GUARD_RETRIES:
                    break
                messages.append({"role": "user", "content": f"Your JSON failed validation:\n{exc}\n"
                                 "Fix every listed problem. Use ONLY ALLOWED_NUMBERS verbatim; JSON only."})
                continue
            z = obj.z_mechanism if fmt == "xyz" else obj.action
            review = gate.review(state, z, candidate, role)
            if review.failures(gate.thresholds):
                review_retries += 1
                errors.append(f"{model}: review f={review.faithful_prob:.2f} m={review.metric_prob:.2f} "
                              f"n={review.natural_prob:.2f}")
                log(f"[review:{review.gate_mode}{' cached' if review.cached else ''}] {model} "
                    f"faithful={review.faithful_prob:.2f} metric={review.metric_prob:.2f} "
                    f"natural={review.natural_prob:.2f}: {review.critique[:300]}")
                if review_retries > MAX_REVIEW_RETRIES:
                    break
                messages.append({"role": "user", "content": review_feedback(review)})
                continue
            output = render_xyz(obj) if fmt == "xyz" else render_star(obj)
            return SynthesisResult(provider, model, output, obj.model_dump(), review, attempts, errors)
        index += 1
        log(f"[failover] exhausted retries on {model}; trying next fallback model")
    raise SynthesisError("No model produced a validated, faithful artifact: " + " | ".join(errors[-6:]))
