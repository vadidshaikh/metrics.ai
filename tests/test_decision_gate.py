from __future__ import annotations

import json

import httpx

from metrics_ai import offline_gate
from metrics_ai.config import DEFAULT_CONFIG
from metrics_ai.decision_gate import DecisionGate, route_roles
from tests.conftest import make_state


def _jev_transport(answers, seen=None, status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append({"auth": request.headers.get("authorization"), "body": json.loads(request.content)})
        return httpx.Response(status, json=answers)
    return httpx.MockTransport(handler)


def test_offline_triage_major_gain_is_worthy_and_tier5():
    result = DecisionGate(DEFAULT_CONFIG).triage(make_state(latency=(1000.0, 200.0)))
    assert result.source == "offline"
    assert result.worth_prob >= 0.60
    assert result.impact_tier == 5
    assert abs(sum(result.role_distribution.values()) - 1.0) < 0.01


def test_offline_triage_tiers_follow_rubric():
    tiers = {lat: offline_gate.triage(make_state(latency=(100.0, lat), memory=(10.0, 10.0),
                                                 extra_diff={}))["impact_tier"]
             for lat in (98.0, 90.0, 80.0, 50.0)}
    # complexity drops 33.3% in make_state, so best gain is at least tier 3
    assert tiers[98.0] == 3 and tiers[80.0] == 3 and tiers[50.0] == 4


def test_offline_triage_rejects_no_code_change():
    state = make_state(signals=())
    assert offline_gate.triage(state)["worth_prob"] < 0.60


def test_offline_triage_rejects_regression():
    result = offline_gate.triage(make_state(latency=(100.0, 150.0)))
    assert result["worth_prob"] < 0.60 and result["impact_tier"] == 1


def test_route_roles_auto_threshold_and_fallback():
    assert route_roles({"aiml": 0.1, "backend": 0.5, "mlops": 0.3, "tpm": 0.1}, "auto") == ["backend", "mlops"]
    assert route_roles({"aiml": 0.24, "backend": 0.24, "mlops": 0.28, "tpm": 0.24}, "auto") == ["mlops"]
    assert route_roles({"aiml": 0.2, "backend": 0.2, "mlops": 0.2, "tpm": 0.21}, "auto") == ["tpm"]
    assert route_roles({}, "all") == ["aiml", "backend", "mlops", "tpm"]
    assert route_roles({}, "tpm") == ["tpm"]


def test_online_jev_triage_parses_typed_answers():
    seen = []
    answers = {"answers": {
        "is_resume_worthy": {"probability": 0.91},
        "impact_tier": {"value": "4", "expected": 4.2},
        "best_fit_role": {"distribution": {"aiml": 0.05, "backend": 0.7, "mlops": 0.2, "tpm": 0.05}},
    }}
    gate = DecisionGate(DEFAULT_CONFIG, api_key="k", transport=_jev_transport(answers, seen))
    result = gate.triage(make_state())
    assert (result.source, result.worth_prob, result.impact_tier, result.impact_score) == ("jev", 0.91, 4, 4.2)
    assert result.role_distribution["backend"] == 0.7
    body = seen[0]["body"]
    assert seen[0]["auth"] == "Bearer k" and body["model"] == "typesafe/jev-1.13.0"
    assert [q["type"] for q in body["questions"]] == ["noul", "score", "choice"]
    assert set(json.loads(body["input"])) == {"ast_structural_diff", "complexity_delta", "telemetry_delta"}


def test_online_jev_list_shaped_response():
    answers = {"results": [{"id": "is_resume_worthy", "probability": 0.7},
                           {"id": "impact_tier", "score": 3},
                           {"id": "best_fit_role", "distribution": {"backend": 1.0}}]}
    result = DecisionGate(DEFAULT_CONFIG, "k", _jev_transport(answers)).triage(make_state())
    assert result.source == "jev" and result.impact_tier == 3


def test_jev_http_error_falls_back_offline():
    gate = DecisionGate(DEFAULT_CONFIG, api_key="k", transport=_jev_transport({}, status=503))
    result = gate.triage(make_state())
    assert result.source == "offline" and "Jev unavailable" in result.notes[0]


def test_jev_malformed_response_falls_back_offline():
    result = DecisionGate(DEFAULT_CONFIG, "k", _jev_transport({"answers": {}})).triage(make_state())
    assert result.source == "offline"


def test_offline_faithfulness_accepts_evidenced_mechanisms():
    state = make_state(extra_diff={"caching_decorators_added": ["lru_cache"]})
    gate = DecisionGate(DEFAULT_CONFIG)
    prob, source = gate.faithfulness(state, "batching inserts via executemany and lru_cache parsing", "x")
    assert source == "offline" and prob >= 0.75


def test_offline_faithfulness_rejects_invented_mechanisms():
    prob, _ = DecisionGate(DEFAULT_CONFIG).faithfulness(
        make_state(), "migrating to Redis with an async Kafka consumer", "x")
    assert prob < 0.75


def test_online_faithfulness_uses_noul_question():
    seen = []
    transport = _jev_transport({"answers": {"is_mechanically_faithful": {"probability": 0.88}}}, seen)
    prob, source = DecisionGate(DEFAULT_CONFIG, "k", transport).faithfulness(make_state(), "z", "full")
    assert (prob, source) == (0.88, "jev")
    payload = json.loads(seen[0]["body"]["input"])
    assert set(payload) == {"ast_structural_diff", "files_modified", "z_clause", "candidate"}
