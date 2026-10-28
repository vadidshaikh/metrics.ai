from __future__ import annotations

import copy
import json

import httpx

from metrics_ai import offline_gate
from metrics_ai.config import DEFAULT_CONFIG
from metrics_ai.decision_gate import (GATE_JEV, GATE_JUDGE, GATE_OFFLINE, DecisionGate, route_roles,
                                      weakest)
from metrics_ai.gate_questions import IMPACT_LEVELS, ROLE_CRITERIA, normalize_tier
from metrics_ai.llm_judge import LlmJudge
from metrics_ai.model_router import ProviderUnavailable
from tests.conftest import make_state


def _jev_transport(answers, seen=None, status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append({"url": str(request.url), "auth": request.headers.get("authorization"),
                         "body": json.loads(request.content)})
        return httpx.Response(status, json=answers)
    return httpx.MockTransport(handler)


class FakeJudgeChat:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, cfg, messages, **kwargs):
        self.calls.append({"messages": copy.deepcopy(messages), **kwargs})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return "groq", "judge-model", reply, 0


TRIAGE_REPLY = json.dumps({"is_resume_worthy": 0.92, "impact_tier": 3.6, "critique": "solid",
                           "role_distribution": {"aiml": 0.1, "backend": 0.5, "mlops": 0.3, "tpm": 0.1}})


# ---- Tier 1: canonical SystemOne contract -------------------------------------------------
def test_tier1_payload_uses_canonical_systemone_schema():
    seen = []
    answers = {"answers": {"is_resume_worthy": {"probability": 0.91}, "impact_tier": {"score": 3.2},
                           "best_fit_role": {"distribution": {"aiml": 0.05, "backend": 0.7, "mlops": 0.2,
                                                              "tpm": 0.05}}}}
    gate = DecisionGate(DEFAULT_CONFIG, api_key="k", transport=_jev_transport(answers, seen))
    result = gate.triage(make_state())
    body = seen[0]["body"]
    assert seen[0]["url"] == "https://api.typesafe.ai/v1/systemone" and seen[0]["auth"] == "Bearer k"
    assert set(body) == {"model", "state", "questions"} and body["model"] == "jev-1.13.0"
    assert set(body["state"]) == {"ast_structural_diff", "complexity_delta", "telemetry_delta"}
    questions = body["questions"]
    assert isinstance(questions, dict) and set(questions) == {"is_resume_worthy", "impact_tier", "best_fit_role"}
    for q in questions.values():
        assert set(q) == {"type", "instructions", "criteria"}  # never a "question" or "options" key
    assert [questions[k]["type"] for k in ("is_resume_worthy", "impact_tier", "best_fit_role")] == \
        ["noul", "score", "choice"]
    assert questions["impact_tier"]["criteria"] == IMPACT_LEVELS and len(IMPACT_LEVELS) == 5
    assert questions["best_fit_role"]["criteria"] == ROLE_CRITERIA
    # 0-indexed score 3.2 -> 4.2 on the 1..5 display scale
    assert (result.gate_mode, result.worth_prob, result.impact_tier, result.impact_score) == (GATE_JEV, 0.91, 4, 4.2)
    assert result.role_distribution["backend"] == 0.7


def test_tier1_review_sends_both_noul_questions():
    seen = []
    answers = {"is_mechanically_faithful": {"probability": 0.88}, "is_prose_natural": {"probability": 0.9}}
    review = DecisionGate(DEFAULT_CONFIG, "k", transport=_jev_transport(answers, seen)).review(
        make_state(), "z", "candidate text")
    assert (review.faithful_prob, review.natural_prob, review.gate_mode) == (0.88, 0.9, GATE_JEV)
    body = seen[0]["body"]
    assert set(body["state"]) == {"ast_structural_diff", "files_modified", "candidate_bullet"}
    assert {q["type"] for q in body["questions"].values()} == {"noul"}
    assert "by doing implementing" in body["questions"]["is_prose_natural"]["instructions"]


def test_tier1_failure_falls_to_tier2_judge():
    chat = FakeJudgeChat([TRIAGE_REPLY])
    gate = DecisionGate(DEFAULT_CONFIG, "k", judge=LlmJudge(DEFAULT_CONFIG, chat=chat),
                        transport=_jev_transport({}, status=503))
    result = gate.triage(make_state())
    assert result.gate_mode == GATE_JUDGE and "Jev API unavailable" in result.notes[0]


def test_normalize_tier_clamps_and_shifts():
    assert normalize_tier(0) == (1, 1.0) and normalize_tier(4) == (5, 5.0)
    assert normalize_tier(9) == (5, 5.0) and normalize_tier(2.4) == (3, 3.4)


# ---- Tier 2: keyless structured LLM judge -------------------------------------------------
def test_tier2_judge_triage_at_temperature_zero():
    chat = FakeJudgeChat([TRIAGE_REPLY])
    result = DecisionGate(DEFAULT_CONFIG, judge=LlmJudge(DEFAULT_CONFIG, chat=chat)).triage(make_state())
    assert result.gate_mode == GATE_JUDGE and result.worth_prob == 0.92
    assert (result.impact_tier, result.impact_score) == (5, 4.6) and result.critique == "solid"
    assert chat.calls[0]["temperature"] == 0.0
    sent = json.loads(chat.calls[0]["messages"][1]["content"])
    assert sent["QUESTIONS"]["best_fit_role"]["criteria"] == ROLE_CRITERIA


def test_tier2_judge_retries_invalid_reply_then_accepts():
    bad = json.dumps({"is_resume_worthy": 1.7, "impact_tier": 2, "role_distribution": {}})
    chat = FakeJudgeChat(["not json", bad, TRIAGE_REPLY])
    result = DecisionGate(DEFAULT_CONFIG, judge=LlmJudge(DEFAULT_CONFIG, chat=chat)).triage(make_state())
    assert result.gate_mode == GATE_JUDGE and len(chat.calls) == 3
    assert "Invalid reply" in chat.calls[1]["messages"][-1]["content"]


def test_tier2_judge_review_returns_critique():
    reply = json.dumps({"is_mechanically_faithful": 0.95, "is_prose_natural": 0.3,
                        "critique": "contains 'by doing implementing'"})
    gate = DecisionGate(DEFAULT_CONFIG, judge=LlmJudge(DEFAULT_CONFIG, chat=FakeJudgeChat([reply])))
    review = gate.review(make_state(), "z", "Accomplished x, by doing implementing y.")
    assert (review.gate_mode, review.natural_prob) == (GATE_JUDGE, 0.3) and "by doing" in review.critique


def test_tier2_unreachable_provider_falls_to_tier3():
    chat = FakeJudgeChat([ProviderUnavailable("down")])
    result = DecisionGate(DEFAULT_CONFIG, judge=LlmJudge(DEFAULT_CONFIG, chat=chat)).triage(make_state())
    assert result.gate_mode == GATE_OFFLINE and any("LLM judge unavailable" in n for n in result.notes)


# ---- Tier 3: deterministic heuristic ------------------------------------------------------
def test_offline_flag_forces_tier3_even_with_key_and_judge():
    chat = FakeJudgeChat([TRIAGE_REPLY])
    gate = DecisionGate(DEFAULT_CONFIG, "k", judge=LlmJudge(DEFAULT_CONFIG, chat=chat), offline=True)
    assert gate.triage(make_state()).gate_mode == GATE_OFFLINE and chat.calls == []


def test_offline_triage_major_gain_is_worthy_and_tier5():
    result = DecisionGate(DEFAULT_CONFIG).triage(make_state(latency=(1000.0, 200.0)))
    assert result.gate_mode == GATE_OFFLINE and result.worth_prob >= 0.60 and result.impact_tier == 5


def test_offline_triage_rejects_no_code_change_and_regression():
    assert offline_gate.triage(make_state(signals=()))["worth_prob"] < 0.60
    regression = offline_gate.triage(make_state(latency=(100.0, 150.0)))
    assert regression["worth_prob"] < 0.60 and regression["impact_tier"] == 1


def test_offline_review_flags_invented_mechanisms_and_bad_prose():
    gate = DecisionGate(DEFAULT_CONFIG)
    good = gate.review(make_state(), "batching inserts via executemany", "Accomplished x, by batching y.")
    assert good.faithful_prob >= 0.80 and good.natural_prob >= 0.80
    invented = gate.review(make_state(), "moving storage to Redis and Kafka", "Accomplished x, by moving.")
    assert invented.faithful_prob < 0.80
    clunky = gate.review(make_state(), "executemany", "Accomplished x, by doing implementing executemany.")
    assert clunky.natural_prob < 0.80


def test_offline_faithfulness_ignores_candidate_text_as_evidence():
    # "redis" appearing only in the candidate itself must not count as evidence for the claim.
    review = DecisionGate(DEFAULT_CONFIG).review(make_state(), "caching rows in redis", "by caching in redis")
    assert review.faithful_prob < 0.80


def test_route_roles_and_weakest_mode():
    assert route_roles({"aiml": 0.1, "backend": 0.5, "mlops": 0.3, "tpm": 0.1}, "auto") == ["backend", "mlops"]
    assert route_roles({"aiml": 0.2, "backend": 0.2, "mlops": 0.2, "tpm": 0.21}, "auto") == ["tpm"]
    assert route_roles({}, "all") == ["aiml", "backend", "mlops", "tpm"] and route_roles({}, "tpm") == ["tpm"]
    assert weakest(GATE_JEV, GATE_JUDGE) == GATE_JUDGE and weakest(GATE_JUDGE, GATE_OFFLINE) == GATE_OFFLINE


# ---- manual Jev playground round-trip -----------------------------------------------------
def test_request_body_matches_what_the_api_client_sends():
    seen = []
    answers = {"is_resume_worthy": {"probability": 0.8}, "impact_tier": {"score": 2.0},
               "best_fit_role": {"distribution": {"backend": 1.0}}}
    gate = DecisionGate(DEFAULT_CONFIG, "k", transport=_jev_transport(answers, seen))
    state = make_state()
    gate.triage(state)
    assert gate.request_body("triage", state) == seen[0]["body"]
    review = gate.request_body("review", state, "bullet")
    assert review["state"]["candidate_bullet"] == "bullet" and set(review["questions"]) == \
        {"is_mechanically_faithful", "is_prose_natural"}


def test_manual_playground_response_drives_tier1_without_a_key():
    reply = {"is_resume_worthy": {"probability": 0.97}, "impact_tier": {"score": 3.9},
             "best_fit_role": {"distribution": {"aiml": 0.1, "backend": 0.6, "mlops": 0.3, "tpm": 0.0}}}
    result = DecisionGate(DEFAULT_CONFIG).triage(make_state(), jev_response=reply)
    assert (result.gate_mode, result.worth_prob, result.impact_tier) == (GATE_JEV, 0.97, 5)
    assert "playground" in result.notes[0]


def test_unusable_manual_response_falls_through_with_note():
    result = DecisionGate(DEFAULT_CONFIG).triage(make_state(), jev_response={"oops": 1})
    assert result.gate_mode == GATE_OFFLINE and "unusable" in result.notes[0]
