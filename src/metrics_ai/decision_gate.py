"""TypeSafe AI Jev ("System One") triage + faithfulness client with an offline fallback gate."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from metrics_ai import offline_gate

ROLES = ["aiml", "backend", "mlops", "tpm"]

WORTHY_QUESTION = {
    "id": "is_resume_worthy",
    "type": "noul",
    "question": "Does this commit represent a genuine algorithmic, systems, or code-quality improvement?",
    "criteria": {
        "true": "Substantive AST-level optimization (batching, caching, loop elimination, concurrency, "
                "modularization) backed by measurable positive telemetry or complexity reduction.",
        "false": "Typo fixes, comment/doc edits, dead-code deletion without functional replacement, "
                 "or any change whose telemetry shows a performance regression.",
    },
}
TIER_QUESTION = {
    "id": "impact_tier",
    "type": "score",
    "question": "Rate the engineering significance of this change.",
    "rubric": {
        "1": "Negligible: <5% delta.",
        "2": "Minor cleanup: 5-15% delta.",
        "3": "Solid module optimization: 15-35% delta.",
        "4": "Major bottleneck elimination: 35-75% delta.",
        "5": "Architectural/algorithmic breakthrough: >75% latency reduction or >200% throughput gain.",
    },
}
ROLE_QUESTION = {
    "id": "best_fit_role",
    "type": "choice",
    "question": "Which engineering role does this change best evidence?",
    "options": ROLES,
    "criteria": {
        "aiml": "Model training/inference, tensors, embeddings, numerical or ML pipeline code.",
        "backend": "Databases, SQL, APIs, caching, request handling, storage and service latency.",
        "mlops": "Benchmarks, CI/CD, profiling, deployment, observability and pipeline tooling.",
        "tpm": "Cross-cutting delivery, docs, configuration, process and measurable program outcomes.",
    },
}


@dataclass
class TriageResult:
    worth_prob: float
    impact_tier: int
    impact_score: float
    role_distribution: dict[str, float]
    source: str
    notes: list[str] = field(default_factory=list)


def route_roles(distribution: dict[str, float], role: str) -> list[str]:
    if role == "all":
        return list(ROLES)
    if role != "auto":
        return [role]
    picked = [r for r, p in sorted(distribution.items(), key=lambda kv: -kv[1]) if p >= 0.25]
    return picked or [max(distribution, key=distribution.get)]


def _answer(data: dict[str, Any], qid: str) -> dict[str, Any]:
    """Locate one typed answer in a Jev response (dict-keyed or list-shaped)."""
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


class DecisionGate:
    """Calls Jev when a key is configured; otherwise (or on failure) uses the offline gate."""

    def __init__(self, cfg: dict[str, Any], api_key: str = "", transport: httpx.BaseTransport | None = None):
        jev = cfg.get("jev", {})
        self.endpoint = jev.get("endpoint", "https://jevmodel.net/v1/systemone")
        self.model = jev.get("model", "typesafe/jev-1.13.0")
        self.timeout = float(jev.get("timeout_s", 8.0))
        self.api_key = api_key
        self.transport = transport

    @property
    def online(self) -> bool:
        return bool(self.api_key)

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        with httpx.Client(timeout=self.timeout, transport=self.transport) as client:
            resp = client.post(self.endpoint, json={"model": self.model, **payload}, headers=headers)
            resp.raise_for_status()
            return resp.json()

    def triage(self, state: dict[str, Any]) -> TriageResult:
        compact = {k: state[k] for k in ("ast_structural_diff", "complexity_delta", "telemetry_delta")}
        if self.online:
            try:
                data = self._post({"input": json.dumps(compact, sort_keys=True),
                                   "questions": [WORTHY_QUESTION, TIER_QUESTION, ROLE_QUESTION]})
                tier_ans = _answer(data, "impact_tier")
                raw_tier = tier_ans.get("value", tier_ans.get("label", tier_ans.get("score")))
                tier = int(round(float(raw_tier)))
                role_ans = _answer(data, "best_fit_role")
                dist = {r: float(role_ans.get("distribution", {}).get(r, 0.0)) for r in ROLES}
                return TriageResult(_prob(_answer(data, "is_resume_worthy")), tier,
                                    float(tier_ans.get("expected", tier)), dist, "jev")
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                result = offline_gate.triage(state)
                result_notes = [f"Jev unavailable ({type(exc).__name__}: {exc}); used offline gate."]
                return TriageResult(**{**result, "source": "offline", "notes": result_notes})
        return TriageResult(**{**offline_gate.triage(state), "source": "offline",
                               "notes": ["JEV_API_KEY not set; used offline deterministic gate."]})

    def faithfulness(self, state: dict[str, Any], z_clause: str, candidate: str) -> tuple[float, str]:
        evidence = {"ast_structural_diff": state["ast_structural_diff"],
                    "files_modified": state["files_modified"]}
        if self.online:
            question = {
                "id": "is_mechanically_faithful", "type": "noul",
                "question": "Does every library, database, algorithm and architectural mechanism claimed "
                            "in the [Z] clause appear in the AST diff or modified files?",
                "criteria": {"true": "All claimed mechanisms are present in the evidence.",
                             "false": "At least one claimed mechanism is absent from the evidence."},
            }
            try:
                data = self._post({"input": json.dumps({**evidence, "z_clause": z_clause,
                                                        "candidate": candidate}, sort_keys=True),
                                   "questions": [question]})
                return _prob(_answer(data, "is_mechanically_faithful")), "jev"
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                pass
        return offline_gate.faithfulness(evidence, z_clause), "offline"
