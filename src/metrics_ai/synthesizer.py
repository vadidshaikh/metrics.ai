"""Prompt assembly, structured LLM execution, numeric guard retries and faithfulness re-prompting."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import ValidationError

from metrics_ai import model_router
from metrics_ai.decision_gate import DecisionGate
from metrics_ai.renderers import render_star, render_xyz
from metrics_ai.schemas import DENSITY_RANGES, StarResponse, XyzResponse

MAX_NUMERIC_RETRIES = 3
MAX_FAITHFUL_RETRIES = 2

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
4. Respond with a single JSON object and nothing else."""

XYZ_SHAPE = ('{"x_accomplishment": str, "y_measurement": str, "z_mechanism": str, '
             '"combined_xyz_bullet": str, "cited_numbers": [number]}')
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
    faithful_source: str
    attempts: int
    errors: list[str] = field(default_factory=list)


def fmt_number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}"


def build_messages(state: dict[str, Any], allowed: set[float], fmt: str, role: str,
                   density: str) -> list[dict[str, str]]:
    lo, hi = DENSITY_RANGES[density]
    if fmt == "xyz":
        task = ("Write one Google XYZ resume bullet: 'Accomplished [X] as measured by [Y], by doing [Z]'. "
                f"combined_xyz_bullet MUST be {lo}-{hi} characters long. Return JSON shaped {XYZ_SHAPE}.")
    else:
        task = ("Write a STAR behavioral interview answer grounded in the exact file names, AST diff and "
                f"benchmark deltas. Return JSON shaped {STAR_SHAPE}.")
    payload = {
        "ROLE_LENS": ROLE_LENS[role],
        "ALLOWED_NUMBERS": [fmt_number(n) for n in sorted(allowed)],
        "FILES": state["files_modified"],
        "COMMAND": state.get("command"),
        "AST_DIFF": state["ast_structural_diff"],
        "COMPLEXITY": state["complexity_delta"],
        "TELEMETRY": state["telemetry_delta"],
        "NOTE": "Negative *_delta_pct means the value went down (lower latency/memory is better).",
    }
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Write for {ROLE_LENS[role]}.\n{task}\n\n"
                                        f"{json.dumps(payload, indent=1)}"}]


def parse_json(text: str) -> dict[str, Any]:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"```(?:json)?", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise json.JSONDecodeError("no JSON object found", text, 0)
    return json.loads(text[start:end + 1])


def ledger_hash(state: dict[str, Any], output_text: str) -> str:
    blob = json.dumps(state, sort_keys=True, default=str) + "\n" + output_text
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def synthesize(cfg: dict[str, Any], gate: DecisionGate, state: dict[str, Any], allowed: set[float],
               fmt: str, role: str, density: str, chat: Callable[..., tuple[str, str, str, int]] | None = None,
               log: Callable[[str], None] = lambda _m: None) -> SynthesisResult:
    """Generate one artifact; numeric-guard retries (3), faithfulness re-prompts (2), model failover."""
    chat = chat or model_router.chat
    schema = XyzResponse if fmt == "xyz" else StarResponse
    context = {"allowed_numbers": allowed, "density": density if fmt == "xyz" else None}
    threshold = float(cfg.get("jev", {}).get("faithful_threshold", 0.75))
    chain_len = max(1, len(model_router.model_chain(cfg)))
    errors: list[str] = []
    attempts = 0
    index = 0
    while index < chain_len:
        messages = build_messages(state, allowed, fmt, role, density)
        numeric_retries = faithful_retries = 0
        while True:
            attempts += 1
            provider, model, text, index = chat(cfg, messages, start_index=index)
            messages.append({"role": "assistant", "content": text})
            try:
                obj = schema.model_validate(parse_json(text), context=context)
            except (ValidationError, json.JSONDecodeError) as exc:
                numeric_retries += 1
                errors.append(f"{model}: {str(exc).splitlines()[0][:200]}")
                log(f"[validation] {model} attempt rejected: {str(exc)[:300]}")
                if numeric_retries > MAX_NUMERIC_RETRIES:
                    break
                messages.append({"role": "user", "content": f"Your JSON failed validation:\n{exc}\n"
                                 "Fix it. Use ONLY ALLOWED_NUMBERS verbatim and return JSON only."})
                continue
            z = obj.z_mechanism if fmt == "xyz" else obj.action
            full = obj.combined_xyz_bullet if fmt == "xyz" else render_star(obj)
            faithful, source = gate.faithfulness(state, z, full)
            if faithful < threshold:
                faithful_retries += 1
                errors.append(f"{model}: faithfulness {faithful:.2f} < {threshold}")
                log(f"[faithfulness] {model} scored {faithful:.2f} ({source}); re-prompting")
                if faithful_retries > MAX_FAITHFUL_RETRIES:
                    break
                messages.append({"role": "user", "content": "STRICTER: your mechanism clause claims "
                                 "libraries/algorithms not present in AST_DIFF or FILES. Rewrite naming only "
                                 "identifiers that literally appear in AST_DIFF/FILES. JSON only."})
                continue
            output = render_xyz(obj) if fmt == "xyz" else render_star(obj)
            return SynthesisResult(provider, model, output, obj.model_dump(), faithful, source,
                                   attempts, errors)
        index += 1
        log(f"[failover] exhausted retries on {model}; trying next fallback model")
    raise SynthesisError("No model produced a validated, faithful artifact: " + " | ".join(errors[-6:]))
