"""3-tier System-1 decision gate.

Tier 1 `jev_api`: a SystemOne endpoint, either OpenJev (codiv.ai) or TypeSafe Jev, using
`{model, state, questions}` with `{type, instructions, criteria}`.
Tier 2 `llm_judge`: the same questions answered by the locked LLM at temperature 0.
Tier 3 `offline_heuristic`: deterministic heuristics, strictly only with `--offline-no-llm`.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from metrics_ai import offline_gate
from metrics_ai.config import OPENJEV_ENDPOINT, OPENJEV_MODEL
from metrics_ai.gate_questions import (REVIEW_CRITIQUES, REVIEW_QUESTIONS, ROLES, TRIAGE_QUESTIONS,
                                       normalize_distribution, normalize_tier)
from metrics_ai.llm_client import BACKOFF_S
from metrics_ai.llm_judge import JudgeError, LlmJudge

GATE_JEV, GATE_JUDGE, GATE_OFFLINE = "jev_api", "llm_judge", "offline_heuristic"
GATE_RANK = {GATE_OFFLINE: 0, GATE_JUDGE: 1, GATE_JEV: 2}
TRANSIENT = {429, 502, 503, 529}  # 529 = OpenJev "server overloaded"


class GateUnavailable(RuntimeError):
    """No SystemOne endpoint or LLM judge answered, and `--offline-no-llm` was not requested."""


@dataclass
class TriageResult:
    worth_prob: float
    impact_tier: int
    impact_score: float
    role_distribution: dict[str, float]
    gate_mode: str
    notes: list[str] = field(default_factory=list)
    critique: str = ""


@dataclass
class ReviewResult:
    faithful_prob: float
    metric_prob: float
    natural_prob: float
    gate_mode: str
    critique: str = ""
    cached: bool = False
    judge_model: str = ""  # who answered Stage 3 (never the generator in llm_judge mode)

    def failures(self, thresholds: dict[str, float]) -> list[str]:
        scores = {"is_mechanically_faithful": self.faithful_prob, "is_metric_accurate": self.metric_prob,
                  "is_prose_natural": self.natural_prob}
        return [q for q, p in scores.items() if p < thresholds.get(q, 0.80)]


def route_roles(distribution: dict[str, float], role: str) -> list[str]:
    if role == "all":
        return list(ROLES)
    if role != "auto":
        return [role]
    picked = [r for r, p in sorted(distribution.items(), key=lambda kv: -kv[1]) if p >= 0.25]
    return picked or [max(distribution, key=distribution.get)]


def weakest(*modes: str) -> str:
    return min(modes, key=GATE_RANK.__getitem__)


def _answer(data: dict[str, Any], qid: str) -> dict[str, Any]:
    """Locate one typed answer in a SystemOne response (dict-keyed or list-shaped)."""
    for container in (data.get("answers"), data.get("results"), data.get("data"), data):
        if isinstance(container, dict) and isinstance(container.get(qid), dict):
            return container[qid]
        if isinstance(container, list):
            for item in container:
                if isinstance(item, dict) and qid in (item.get("id"), item.get("question_id")):
                    return item
    raise ValueError(f"SystemOne response is missing answer {qid!r}")


def _prob(ans: dict[str, Any]) -> float:
    # TypeSafe-style `probability`, OpenJev-style `noul` (P(yes)), or generic fallbacks.
    value = next((ans[k] for k in ("probability", "noul", "p_true", "score") if ans.get(k) is not None), None)
    if value is None:
        raise ValueError("SystemOne answer has no probability")
    return float(value)


def triage_state(state: dict[str, Any]) -> dict[str, Any]:
    return {k: state[k] for k in ("ast_structural_diff", "complexity_delta", "telemetry_delta")}


def review_state(state: dict[str, Any], candidate: str, role: str = "") -> dict[str, Any]:
    """Stage-3 evidence: code mechanisms *and* metric semantics, so metric mislabels are judgeable."""
    return {"ast_structural_diff": state["ast_structural_diff"], "files_modified": state["files_modified"],
            "telemetry_delta": state["telemetry_delta"], "measured_metrics": state.get("measured_metrics", []),
            "unmeasured_metrics": state.get("unmeasured_metrics", []), "target_role": role,
            "candidate_bullet": candidate}


def cache_key(state: dict[str, Any], candidate: str) -> str:
    canonical = json.dumps(state, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256((canonical + candidate).encode("utf-8")).hexdigest()


def parse_triage(data: dict[str, Any], notes: list[str] | None = None) -> TriageResult:
    tier_ans = _answer(data, "impact_tier")
    tier, score = normalize_tier(tier_ans.get("score", tier_ans.get("value", tier_ans.get("expected"))))
    role_ans = _answer(data, "best_fit_role")
    dist = normalize_distribution(role_ans.get("distribution") or role_ans.get("probabilities") or {})
    return TriageResult(_prob(_answer(data, "is_resume_worthy")), tier, score, dist, GATE_JEV, notes or [])


def diagnostic_critique(result: ReviewResult, thresholds: dict[str, float]) -> str:
    return " ".join(REVIEW_CRITIQUES[q] for q in result.failures(thresholds))


class DecisionGate:
    def __init__(self, cfg: dict[str, Any], api_key: str = "", judge: LlmJudge | None = None,
                 transport: httpx.BaseTransport | None = None, offline: bool = False,
                 endpoint: str | None = None, model: str | None = None):
        jev = cfg.get("jev", {})
        self.endpoint = endpoint or jev.get("endpoint", OPENJEV_ENDPOINT)
        self.model = model or jev.get("model", OPENJEV_MODEL)
        self.timeout = float(jev.get("timeout_s", 30.0))
        self.api_key = "" if offline else api_key
        self.judge = None if offline else judge
        self.offline = offline
        self.transport = transport
        self.thresholds = {"is_mechanically_faithful": float(jev.get("faithful_threshold", 0.80)),
                           "is_metric_accurate": float(jev.get("metric_threshold", 0.80)),
                           "is_prose_natural": float(jev.get("natural_threshold", 0.80))}
        self._review_cache: dict[str, ReviewResult] = {}

    def _post(self, state: dict[str, Any], questions: dict[str, Any]) -> dict[str, Any]:
        """POST to SystemOne, backing off 1.5 s then 3.0 s on rate-limit/overload (429/502/503/529)."""
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        payload = {"model": self.model, "state": state, "questions": questions}
        with httpx.Client(timeout=self.timeout, transport=self.transport) as client:
            resp = client.post(self.endpoint, json=payload, headers=headers)
            for wait in BACKOFF_S:
                if resp.status_code not in TRANSIENT:
                    break
                time.sleep(wait)
                resp = client.post(self.endpoint, json=payload, headers=headers)
            resp.raise_for_status()
            return resp.json()

    def request_body(self, stage: str, state: dict[str, Any], candidate: str = "", role: str = "") -> dict[str, Any]:
        """The exact SystemOne body for `stage` ('triage' | 'review'), e.g. for a manual playground run."""
        if stage == "triage":
            return {"model": self.model, "state": triage_state(state), "questions": TRIAGE_QUESTIONS}
        return {"model": self.model, "state": review_state(state, candidate, role), "questions": REVIEW_QUESTIONS}

    def triage(self, state: dict[str, Any], jev_response: dict[str, Any] | None = None) -> TriageResult:
        """`jev_response`: a SystemOne reply obtained out-of-band (e.g. a playground run)."""
        compact = triage_state(state)
        notes: list[str] = []
        if self.offline:
            notes.append("--offline-no-llm: Tier-3 deterministic heuristic gate.")
            return TriageResult(**offline_gate.triage(state), gate_mode=GATE_OFFLINE, notes=notes)
        if jev_response is not None:
            try:
                return parse_triage(jev_response, ["Tier-1 SystemOne response supplied manually."])
            except (ValueError, KeyError, TypeError) as exc:
                notes.append(f"Supplied SystemOne response unusable ({type(exc).__name__}: {str(exc)[:120]}).")
        if self.api_key:
            try:
                return parse_triage(self._post(compact, TRIAGE_QUESTIONS), [f"Tier-1 {self.model} @ {self.endpoint}."])
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                notes.append(f"SystemOne API unavailable ({type(exc).__name__}: {str(exc)[:120]}).")
        if self.judge:
            try:
                verdict = self.judge.triage(compact, TRIAGE_QUESTIONS)
                tier, score = normalize_tier(verdict.impact_tier)
                notes.append(f"Tier-2 LLM judge ({self.judge.last_model}, temperature 0).")
                return TriageResult(round(verdict.is_resume_worthy, 3), tier, score,
                                    normalize_distribution(verdict.role_distribution), GATE_JUDGE, notes,
                                    verdict.critique)
            except JudgeError as exc:
                notes.append(f"LLM judge unavailable ({str(exc)[:120]}).")
        raise GateUnavailable(" ".join(notes) or "No SystemOne key and no LLM judge configured. "
                              "Run `metrics auth`, or pass --offline-no-llm for the Tier-3 heuristic gate.")

    def review(self, state: dict[str, Any], z_clause: str, candidate: str, role: str = "",
               generator_model: str = "") -> ReviewResult:
        """Stage 3: faithfulness, metric accuracy and fluency, cached per (state, candidate, generator).

        In `llm_judge` mode the verdict comes from a verified model other than `generator_model`.
        """
        evidence = review_state(state, candidate, role)
        key = cache_key(evidence, candidate + "\x00" + generator_model)
        if key in self._review_cache:
            hit = self._review_cache[key]
            return ReviewResult(hit.faithful_prob, hit.metric_prob, hit.natural_prob, hit.gate_mode,
                                hit.critique, cached=True, judge_model=hit.judge_model)
        result = self._review_uncached(evidence, z_clause, candidate, generator_model)
        if result.failures(self.thresholds) and not result.critique:
            result.critique = diagnostic_critique(result, self.thresholds)
        self._review_cache[key] = result
        return result

    def _review_uncached(self, evidence: dict[str, Any], z_clause: str, candidate: str,
                         generator_model: str = "") -> ReviewResult:
        if self.offline:
            return self._offline_review(evidence, z_clause, candidate)
        errors = []
        if self.api_key:
            try:
                data = self._post(evidence, REVIEW_QUESTIONS)
                return ReviewResult(*(_prob(_answer(data, q)) for q in REVIEW_QUESTIONS), GATE_JEV,
                                    judge_model=self.model)
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                errors.append(f"SystemOne: {type(exc).__name__}")
        if self.judge:
            try:
                verdict = self.judge.review(evidence, REVIEW_QUESTIONS, generator_model=generator_model)
                return ReviewResult(round(verdict.is_mechanically_faithful, 3), round(verdict.is_metric_accurate, 3),
                                    round(verdict.is_prose_natural, 3), GATE_JUDGE, verdict.critique,
                                    judge_model=self.judge.last_model)
            except JudgeError as exc:
                errors.append(f"judge: {str(exc)[:80]}")
        raise GateUnavailable("Stage-3 review unavailable (" + "; ".join(errors or ["no tier configured"]) + ")")

    @staticmethod
    def _offline_review(evidence: dict[str, Any], z_clause: str, candidate: str) -> ReviewResult:
        faithful = offline_gate.faithfulness(evidence, z_clause)
        metric = offline_gate.metric_accuracy(candidate, evidence.get("unmeasured_metrics", []))
        natural, critique = offline_gate.naturalness(candidate)
        return ReviewResult(faithful, metric, natural, GATE_OFFLINE, critique)
