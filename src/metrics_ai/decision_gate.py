"""3-tier System-1 decision gate.

Tier 1 `jev_api`: TypeSafe AI SystemOne (`{model, state, questions}` with `{type, instructions, criteria}`).
Tier 2 `llm_judge`: the same questions answered by the locked LLM at temperature 0 (keyless).
Tier 3 `offline_heuristic`: deterministic heuristics, only with `--offline-no-llm` or no reachable model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from metrics_ai import offline_gate
from metrics_ai.gate_questions import (REVIEW_QUESTIONS, ROLES, TRIAGE_QUESTIONS, normalize_distribution,
                                       normalize_tier)
from metrics_ai.llm_judge import JudgeError, LlmJudge

GATE_JEV, GATE_JUDGE, GATE_OFFLINE = "jev_api", "llm_judge", "offline_heuristic"
GATE_RANK = {GATE_OFFLINE: 0, GATE_JUDGE: 1, GATE_JEV: 2}


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
    natural_prob: float
    gate_mode: str
    critique: str = ""


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
    raise ValueError(f"Jev response is missing answer {qid!r}")


def _prob(ans: dict[str, Any]) -> float:
    value = ans.get("probability", ans.get("p_true", ans.get("score")))
    if value is None:
        raise ValueError("Jev answer has no probability")
    return float(value)


def triage_state(state: dict[str, Any]) -> dict[str, Any]:
    return {k: state[k] for k in ("ast_structural_diff", "complexity_delta", "telemetry_delta")}


def review_state(state: dict[str, Any], candidate: str) -> dict[str, Any]:
    return {"ast_structural_diff": state["ast_structural_diff"], "files_modified": state["files_modified"],
            "candidate_bullet": candidate}


def parse_triage(data: dict[str, Any], notes: list[str] | None = None) -> TriageResult:
    tier_ans = _answer(data, "impact_tier")
    tier, score = normalize_tier(tier_ans.get("score", tier_ans.get("value", tier_ans.get("expected"))))
    dist = normalize_distribution(_answer(data, "best_fit_role").get("distribution", {}))
    return TriageResult(_prob(_answer(data, "is_resume_worthy")), tier, score, dist, GATE_JEV, notes or [])


class DecisionGate:
    def __init__(self, cfg: dict[str, Any], api_key: str = "", judge: LlmJudge | None = None,
                 transport: httpx.BaseTransport | None = None, offline: bool = False):
        jev = cfg.get("jev", {})
        self.endpoint = jev.get("endpoint", "https://api.typesafe.ai/v1/systemone")
        self.model = jev.get("model", "jev-1.13.0")
        self.timeout = float(jev.get("timeout_s", 8.0))
        self.api_key = "" if offline else api_key
        self.judge = None if offline else judge
        self.transport = transport

    def _post(self, state: dict[str, Any], questions: dict[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        payload = {"model": self.model, "state": state, "questions": questions}
        with httpx.Client(timeout=self.timeout, transport=self.transport) as client:
            resp = client.post(self.endpoint, json=payload, headers=headers)
            resp.raise_for_status()
            return resp.json()

    def request_body(self, stage: str, state: dict[str, Any], candidate: str = "") -> dict[str, Any]:
        """The exact SystemOne body for `stage` ('triage' | 'review'), e.g. to paste into the Jev playground."""
        if stage == "triage":
            return {"model": self.model, "state": triage_state(state), "questions": TRIAGE_QUESTIONS}
        return {"model": self.model, "state": review_state(state, candidate), "questions": REVIEW_QUESTIONS}

    def triage(self, state: dict[str, Any], jev_response: dict[str, Any] | None = None) -> TriageResult:
        """`jev_response`: a SystemOne reply obtained out-of-band (e.g. the free Jev playground)."""
        compact = triage_state(state)
        notes: list[str] = []
        if jev_response is not None:
            try:
                return parse_triage(jev_response, ["Tier-1 Jev response supplied manually (playground)."])
            except (ValueError, KeyError, TypeError) as exc:
                notes.append(f"Supplied Jev response unusable ({type(exc).__name__}: {str(exc)[:120]}).")
        if self.api_key:
            try:
                return parse_triage(self._post(compact, TRIAGE_QUESTIONS))
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                notes.append(f"Jev API unavailable ({type(exc).__name__}: {str(exc)[:120]}).")
        if self.judge:
            try:
                verdict = self.judge.triage(compact, TRIAGE_QUESTIONS)
                tier, score = normalize_tier(verdict.impact_tier)
                notes.append(f"No usable Jev key; Tier-2 LLM judge ({self.judge.last_model}, temperature 0).")
                return TriageResult(round(verdict.is_resume_worthy, 3), tier, score,
                                    normalize_distribution(verdict.role_distribution), GATE_JUDGE, notes,
                                    verdict.critique)
            except JudgeError as exc:
                notes.append(f"LLM judge unavailable ({str(exc)[:120]}).")
        notes.append("Tier-3 offline deterministic heuristic gate used.")
        return TriageResult(**offline_gate.triage(state), gate_mode=GATE_OFFLINE, notes=notes)

    def review(self, state: dict[str, Any], z_clause: str, candidate: str) -> ReviewResult:
        """Stage 3: `is_mechanically_faithful` + `is_prose_natural` for one candidate."""
        evidence = review_state(state, candidate)
        if self.api_key:
            try:
                data = self._post(evidence, REVIEW_QUESTIONS)
                return ReviewResult(_prob(_answer(data, "is_mechanically_faithful")),
                                    _prob(_answer(data, "is_prose_natural")), GATE_JEV)
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                pass
        if self.judge:
            try:
                verdict = self.judge.review(evidence, REVIEW_QUESTIONS)
                return ReviewResult(round(verdict.is_mechanically_faithful, 3),
                                    round(verdict.is_prose_natural, 3), GATE_JUDGE, verdict.critique)
            except JudgeError:
                pass
        faithful = offline_gate.faithfulness(evidence, z_clause)
        natural, critique = offline_gate.naturalness(candidate)
        return ReviewResult(faithful, natural, GATE_OFFLINE, critique)
