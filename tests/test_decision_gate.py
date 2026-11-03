from __future__ import annotations

import copy
import json

import httpx
import pytest

from metrics_ai import offline_gate
from metrics_ai.config import DEFAULT_CONFIG
from metrics_ai.decision_gate import (GATE_JEV, GATE_JUDGE, GATE_OFFLINE, DecisionGate, GateUnavailable,
                                      cache_key, review_state, route_roles, weakest)
from metrics_ai.gate_questions import IMPACT_LEVELS, REVIEW_QUESTIONS, ROLE_CRITERIA, normalize_tier
from metrics_ai.llm_judge import LlmJudge
from metrics_ai.model_router import ProviderUnavailable
from tests.conftest import make_state

TRIAGE_ANSWERS = {"answers": {"is_resume_worthy": {"probability": 0.91}, "impact_tier": {"score": 3.2},
                              "best_fit_role": {"distribution": {"aiml": 0.05, "backend": 0.7, "mlops": 0.2,
                                                                 "tpm": 0.05}}}}
REVIEW_ANSWERS = {"answers": {"is_mechanically_faithful": {"noul": 0.95}, "is_metric_accurate": {"noul": 0.9},
                              "is_prose_natural": {"noul": 0.85}}}


def _transport(answers, seen=None, status=200):
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
        self.calls.append({"messages": copy.deepcopy(messages), "cfg": copy.deepcopy(cfg), **kwargs})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        router = cfg.get("router", {})
        return router.get("active_provider") or "groq", router.get("active_model") or "judge-model", reply, 0


TRIAGE_REPLY = json.dumps({"is_resume_worthy": 0.92, "impact_tier": 3.6, "critique": "solid",
                           "role_distribution": {"aiml": 0.1, "backend": 0.5, "mlops": 0.3, "tpm": 0.1}})


def _judge(*replies):
    chat = FakeJudgeChat(replies)
    return LlmJudge(DEFAULT_CONFIG, chat=chat), chat


# ---- Tier 1: canonical SystemOne contract (OpenJev default) --------------------------------
def test_tier1_defaults_to_openjev_with_canonical_schema():
    seen = []
    result = DecisionGate(DEFAULT_CONFIG, "k", transport=_transport(TRIAGE_ANSWERS, seen)).triage(make_state())
    body = seen[0]["body"]
    assert seen[0]["url"] == "https://api.codiv.ai/v1/systemone" and seen[0]["auth"] == "Bearer k"
    assert set(body) == {"model", "state", "questions"} and body["model"] == "openjev-latest"
    assert set(body["state"]) == {"ast_structural_diff", "complexity_delta", "telemetry_delta"}
    for q in body["questions"].values():
        assert set(q) == {"type", "instructions", "criteria"}  # never "question" / "options"
    assert body["questions"]["impact_tier"]["criteria"] == IMPACT_LEVELS
    assert body["questions"]["best_fit_role"]["criteria"] == ROLE_CRITERIA
    assert (result.gate_mode, result.worth_prob, result.impact_tier, result.impact_score) == (GATE_JEV, 0.91, 4, 4.2)


def test_tier1_explicit_typesafe_endpoint():
    seen = []
    gate = DecisionGate(DEFAULT_CONFIG, "ts", transport=_transport(TRIAGE_ANSWERS, seen),
                        endpoint="https://api.typesafe.ai/v1/systemone", model="jev-1.13.0")
    gate.triage(make_state())
    assert seen[0]["url"] == "https://api.typesafe.ai/v1/systemone" and seen[0]["body"]["model"] == "jev-1.13.0"


def test_openjev_reply_shape_is_parsed():
    answers = {"answers": {"is_resume_worthy": {"noul": 0.99},
                           "impact_tier": {"score": 3.7, "legend": [], "probabilities": [0, 0, .05, .2, .75]},
                           "best_fit_role": {"choice": "backend", "probabilities": {"aiml": 0.1, "backend": 0.6,
                                                                                    "mlops": 0.3, "tpm": 0.0}}}}
    result = DecisionGate(DEFAULT_CONFIG, "k", transport=_transport(answers)).triage(make_state())
    assert (result.worth_prob, result.impact_tier, result.impact_score) == (0.99, 5, 4.7)
    assert result.role_distribution["backend"] == 0.6


def test_normalize_tier_clamps_and_shifts():
    assert normalize_tier(0) == (1, 1.0) and normalize_tier(4) == (5, 5.0)
    assert normalize_tier(9) == (5, 5.0) and normalize_tier(2.4) == (3, 3.4)


# ---- Stage 3: enriched state, three diagnostics, cache -------------------------------------
def test_stage3_state_carries_telemetry_metrics_and_role():
    seen = []
    state = make_state()
    review = DecisionGate(DEFAULT_CONFIG, "k", transport=_transport(REVIEW_ANSWERS, seen)).review(
        state, "z", "candidate text", "backend")
    body = seen[0]["body"]
    assert set(body["state"]) == {"ast_structural_diff", "files_modified", "telemetry_delta", "measured_metrics",
                                  "unmeasured_metrics", "target_role", "candidate_bullet"}
    assert body["state"]["unmeasured_metrics"] == ["throughput_items_sec"] and body["state"]["target_role"] == "backend"
    assert list(body["questions"]) == ["is_mechanically_faithful", "is_metric_accurate", "is_prose_natural"]
    assert {q["type"] for q in body["questions"].values()} == {"noul"}
    assert (review.faithful_prob, review.metric_prob, review.natural_prob, review.gate_mode) == (0.95, 0.9, 0.85, GATE_JEV)
    assert review.failures({}) == []


def test_stage3_low_score_yields_specific_diagnostic_critique():
    answers = {"answers": {"is_mechanically_faithful": {"noul": 0.97}, "is_metric_accurate": {"noul": 0.1},
                           "is_prose_natural": {"noul": 0.95}}}
    review = DecisionGate(DEFAULT_CONFIG, "k", transport=_transport(answers)).review(make_state(), "z", "c")
    assert review.failures({}) == ["is_metric_accurate"]
    assert review.critique.startswith("METRICS:") and "FLUENCY" not in review.critique


def test_stage3_cache_returns_identical_scores_without_a_second_call():
    seen = []
    gate = DecisionGate(DEFAULT_CONFIG, "k", transport=_transport(REVIEW_ANSWERS, seen))
    first = gate.review(make_state(), "z", "same bullet", "aiml")
    second = gate.review(make_state(), "z", "same bullet", "aiml")
    assert len(seen) == 1 and second.cached and not first.cached
    assert (first.faithful_prob, first.metric_prob, first.natural_prob) == \
        (second.faithful_prob, second.metric_prob, second.natural_prob)
    gate.review(make_state(), "z", "different bullet", "aiml")
    assert len(seen) == 2


def test_cache_key_is_canonical():
    a = review_state(make_state(), "x", "r")
    b = json.loads(json.dumps(a))  # same content, different object/order
    assert cache_key(a, "x") == cache_key(b, "x") != cache_key(a, "y")


# ---- Tier 2: keyless structured LLM judge -------------------------------------------------
def test_tier2_judge_triage_at_temperature_zero():
    judge, chat = _judge(TRIAGE_REPLY)
    result = DecisionGate(DEFAULT_CONFIG, judge=judge).triage(make_state())
    assert result.gate_mode == GATE_JUDGE and result.worth_prob == 0.92 and (result.impact_tier, result.impact_score) == (5, 4.6)
    assert chat.calls[0]["temperature"] == 0.0


def test_tier1_failure_falls_to_tier2_judge():
    judge, _ = _judge(TRIAGE_REPLY)
    gate = DecisionGate(DEFAULT_CONFIG, "k", judge=judge, transport=_transport({}, status=503))
    result = gate.triage(make_state())
    assert result.gate_mode == GATE_JUDGE and "SystemOne API unavailable" in result.notes[0]


def test_tier2_judge_retries_invalid_reply_then_accepts():
    bad = json.dumps({"is_resume_worthy": 1.7, "impact_tier": 2, "role_distribution": {}})
    judge, chat = _judge("not json", bad, TRIAGE_REPLY)
    assert DecisionGate(DEFAULT_CONFIG, judge=judge).triage(make_state()).gate_mode == GATE_JUDGE
    assert len(chat.calls) == 3 and "Invalid reply" in chat.calls[1]["messages"][-1]["content"]


def test_tier2_judge_review_answers_all_three_questions():
    reply = json.dumps({"is_mechanically_faithful": 0.95, "is_metric_accurate": 0.9, "is_prose_natural": 0.3,
                        "critique": "contains 'by doing implementing'"})
    judge, chat = _judge(reply)
    review = DecisionGate(DEFAULT_CONFIG, judge=judge).review(make_state(), "z", "Accomplished x.", "tpm")
    assert (review.gate_mode, review.metric_prob, review.natural_prob) == (GATE_JUDGE, 0.9, 0.3)
    assert "by doing" in review.critique
    sent = json.loads(chat.calls[0]["messages"][1]["content"])
    assert list(sent["QUESTIONS"]) == list(REVIEW_QUESTIONS) and sent["STATE"]["target_role"] == "tpm"


# ---- Tier 3: strictly opt-in --------------------------------------------------------------
def test_no_tier_available_raises_instead_of_silently_using_heuristics():
    judge, _ = _judge(ProviderUnavailable("down"))
    with pytest.raises(GateUnavailable, match="LLM judge unavailable"):
        DecisionGate(DEFAULT_CONFIG, judge=judge).triage(make_state())
    with pytest.raises(GateUnavailable):
        DecisionGate(DEFAULT_CONFIG).review(make_state(), "z", "c")


def test_offline_flag_forces_tier3_even_with_key_and_judge():
    judge, chat = _judge(TRIAGE_REPLY)
    gate = DecisionGate(DEFAULT_CONFIG, "k", judge=judge, offline=True)
    assert gate.triage(make_state()).gate_mode == GATE_OFFLINE and chat.calls == []


def test_offline_triage_rules():
    assert offline_gate.triage(make_state(latency=(1000.0, 200.0)))["impact_tier"] == 5
    assert offline_gate.triage(make_state(signals=()))["worth_prob"] < 0.60
    regression = offline_gate.triage(make_state(latency=(100.0, 150.0)))
    assert regression["worth_prob"] < 0.60 and regression["impact_tier"] == 1


def test_offline_review_flags_invented_mechanisms_unmeasured_metrics_and_bad_prose():
    gate = DecisionGate(DEFAULT_CONFIG, offline=True)
    good = gate.review(make_state(), "batching inserts via executemany", "Accomplished x, by batching y.")
    assert good.failures({}) == []
    assert gate.review(make_state(), "moving storage to Redis and Kafka", "by moving.").faithful_prob < 0.80
    assert gate.review(make_state(), "executemany", "a 4x throughput gain by batching").metric_prob < 0.80
    assert gate.review(make_state(), "executemany", "by doing implementing executemany").natural_prob < 0.80
    # the candidate itself never counts as evidence for its own claims
    assert gate.review(make_state(), "caching rows in redis", "by caching in redis").faithful_prob < 0.80


# ---- manual round-trip + routing ---------------------------------------------------------
def test_request_body_matches_what_the_api_client_sends():
    seen = []
    gate = DecisionGate(DEFAULT_CONFIG, "k", transport=_transport(TRIAGE_ANSWERS, seen))
    state = make_state()
    gate.triage(state)
    assert gate.request_body("triage", state) == seen[0]["body"]
    assert gate.request_body("review", state, "bullet", "aiml")["state"]["candidate_bullet"] == "bullet"


def test_manual_response_drives_tier1_and_bad_one_falls_through():
    reply = {"is_resume_worthy": {"probability": 0.97}, "impact_tier": {"score": 3.9},
             "best_fit_role": {"distribution": {"backend": 1.0}}}
    assert DecisionGate(DEFAULT_CONFIG).triage(make_state(), jev_response=reply).gate_mode == GATE_JEV
    judge, _ = _judge(TRIAGE_REPLY)
    result = DecisionGate(DEFAULT_CONFIG, judge=judge).triage(make_state(), jev_response={"oops": 1})
    assert result.gate_mode == GATE_JUDGE and "unusable" in result.notes[0]


def test_route_roles_and_weakest_mode():
    assert route_roles({"aiml": 0.1, "backend": 0.5, "mlops": 0.3, "tpm": 0.1}, "auto") == ["backend", "mlops"]
    assert route_roles({"aiml": 0.2, "backend": 0.2, "mlops": 0.2, "tpm": 0.21}, "auto") == ["tpm"]
    assert route_roles({}, "all") == ["aiml", "backend", "mlops", "tpm"] and route_roles({}, "tpm") == ["tpm"]
    assert weakest(GATE_JEV, GATE_JUDGE) == GATE_JUDGE and weakest(GATE_JUDGE, GATE_OFFLINE) == GATE_OFFLINE


def test_systemone_overload_529_is_retried_with_backoff(sleeps):
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(529, json={"error": "server overloaded"})
        return httpx.Response(200, json=TRIAGE_ANSWERS)
    result = DecisionGate(DEFAULT_CONFIG, "k", transport=httpx.MockTransport(handler)).triage(make_state())
    assert result.gate_mode == GATE_JEV and len(calls) == 3 and sleeps == [1.5, 3.0]


def test_offline_gate_ignores_empty_derived_keys_and_telemetry_noise():
    # Regression: a comment-only commit has {"signals": [], "mechanisms": []}; a noisy RSS sample on a tiny
    # process (here -57.7% memory) must not make the offline gate call it a resume-worthy code change.
    state = make_state(latency=(47.7, 45.8), memory=(14.5, 6.1), signals=())
    state["ast_structural_diff"] = {"signals": [], "mechanisms": []}
    assert offline_gate.triage(state)["worth_prob"] < 0.60


# ---- Stage-3 judge independence -----------------------------------------------------------
REVIEW_REPLY = json.dumps({"is_mechanically_faithful": 0.95, "is_metric_accurate": 0.95, "is_prose_natural": 0.9})


def _pool_cfg(*models):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    (p0, m0), *rest = models
    cfg["router"].update(active_provider=p0, active_model=m0,
                         fallback_models=[{"provider": p, "model": m} for p, m in rest])
    return cfg


def test_llm_judge_never_reviews_its_own_output():
    cfg = _pool_cfg(("groq", "openai/gpt-oss-120b"), ("groq", "qwen/qwen3.8-27b"), ("groq", "openai/gpt-oss-20b"))
    for generator, expected_judge in (("openai/gpt-oss-120b", "qwen/qwen3.8-27b"),
                                      ("qwen/qwen3.8-27b", "openai/gpt-oss-120b")):
        chat = FakeJudgeChat([REVIEW_REPLY])
        gate = DecisionGate(cfg, judge=LlmJudge(cfg, chat=chat))
        review = gate.review(make_state(), "z", "candidate", "backend", generator_model=generator)
        used = chat.calls[0]["cfg"]["router"]
        assert used["active_model"] == expected_judge
        assert generator not in [used["active_model"], *[f["model"] for f in used["fallback_models"]]]
        assert review.judge_model == f"groq/{expected_judge}"


def test_same_model_on_another_provider_is_still_excluded_and_lone_model_cannot_self_judge():
    cfg = _pool_cfg(("groq", "m"), ("hf", "m"), ("ollama", "other"))
    chat = FakeJudgeChat([REVIEW_REPLY])
    DecisionGate(cfg, judge=LlmJudge(cfg, chat=chat)).review(make_state(), "z", "c", generator_model="m")
    assert chat.calls[0]["cfg"]["router"]["active_model"] == "other"
    lone = _pool_cfg(("groq", "only"))
    with pytest.raises(GateUnavailable, match="no verified model other than only"):
        DecisionGate(lone, judge=LlmJudge(lone, chat=FakeJudgeChat([REVIEW_REPLY]))).review(
            make_state(), "z", "c", generator_model="only")


def test_review_cache_is_per_generator():
    cfg = _pool_cfg(("groq", "a"), ("groq", "b"))
    chat = FakeJudgeChat([REVIEW_REPLY, REVIEW_REPLY])
    gate = DecisionGate(cfg, judge=LlmJudge(cfg, chat=chat))
    gate.review(make_state(), "z", "same text", generator_model="a")
    assert gate.review(make_state(), "z", "same text", generator_model="a").cached
    gate.review(make_state(), "z", "same text", generator_model="b")  # judged afresh by "a", not a cache hit
    assert [c["cfg"]["router"]["active_model"] for c in chat.calls] == ["b", "a"]
