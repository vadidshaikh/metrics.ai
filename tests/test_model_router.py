from __future__ import annotations

import copy
import json

import httpx
import pytest

from metrics_ai import llm_client, model_router
from metrics_ai.config import DEFAULT_CONFIG
from metrics_ai.model_router import Candidate, probe_plan, rank, score_model


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gk")
    monkeypatch.setenv("HF_TOKEN", "hk")
    return copy.deepcopy(DEFAULT_CONFIG)


def test_tier_scores():
    assert score_model("qwen/qwen3-coder-480b") == 100
    assert score_model("Qwen/Qwen2.5-Coder-32B-Instruct") == 97
    assert score_model("openai/gpt-oss-120b") == 95
    assert 88 <= score_model("qwen/qwen3.8-27b") <= 94      # dotted qwen3 versions are Tier 2
    assert 88 <= score_model("Qwen/Qwen3-32B") <= 94
    assert 88 <= score_model("Qwen/Qwen3-235B-A22B-Instruct-2507") <= 94
    assert score_model("meta-llama/Llama-3.3-70B-Instruct") == 90
    assert 88 <= score_model("deepseek-ai/DeepSeek-V4-Pro") <= 94
    assert 78 <= score_model("moonshotai/Kimi-K2.5") <= 87
    assert 78 <= score_model("openai/gpt-oss-20b") <= 87
    assert 78 <= score_model("llama-3.1-70b-versatile") <= 87
    assert score_model("llama-3.1-8b-instant") == 40         # Tier 4 now
    assert score_model("mistral-small") == 40


@pytest.mark.parametrize("model_id", ["whisper-large-v3", "playai-tts", "llama-guard-4-12b",
                                      "nomic-embed-text", "llama-3.2-vision", "omni-moderation",
                                      "bge-reranker", "distil-audio", "canopylabs/orpheus-v1-english",
                                      "allam-2-7b"])
def test_exclusion_filter(model_id):
    assert score_model(model_id) is None


def test_hf_structured_bonus_and_live_filter():
    live = {"providers": [{"status": "live", "supports_structured_output": True}]}
    assert score_model("openai/gpt-oss-120b", live) == 100
    staging = {"providers": [{"status": "staging", "supports_structured_output": True}]}
    assert score_model("openai/gpt-oss-120b", staging) is None


def test_rank_prefers_provider_then_score_then_size():
    cands = [Candidate("hf", "a", 95), Candidate("groq", "b", 90),
             Candidate("hf", "c-7b", 97, 7.0), Candidate("hf", "c-32b", 97, 32.0)]
    assert [c.model for c in rank(cands)] == ["c-32b", "c-7b", "a", "b"]
    assert rank(cands, prefer="groq")[0].model == "b"


def test_probe_plan_is_top3_primary_plus_top2_per_secondary():
    ranked = rank([Candidate("groq", f"g{i}", 90 - i) for i in range(5)] +
                  [Candidate("hf", f"h{i}", 99 - i) for i in range(4)] +
                  [Candidate("ollama", "o0", 40)], prefer="groq")
    assert [c.model for c in probe_plan(ranked, "groq")] == ["g0", "g1", "g2", "h0", "h1", "o0"]


def _router_transport(fail_models=(), seen=None, status=429):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("api.groq.com/openai/v1/models"):
            assert request.headers["authorization"] == "Bearer gk"
            return httpx.Response(200, json={"data": [{"id": "openai/gpt-oss-120b"}, {"id": "qwen/qwen3.8-27b"},
                                                      {"id": "openai/gpt-oss-20b"}, {"id": "llama-3.1-8b-instant"},
                                                      {"id": "allam-2-7b"}, {"id": "whisper-large-v3"}]})
        if url.endswith("router.huggingface.co/v1/models"):
            assert request.headers["authorization"] == "Bearer hk"
            return httpx.Response(200, json={"data": [
                {"id": "Qwen/Qwen2.5-Coder-32B-Instruct", "providers": [{"status": "live"}]},
                {"id": "Qwen/Qwen3-32B", "providers": [{"status": "live"}]},
                {"id": "meta-llama/Llama-3.3-70B-Instruct", "providers": [{"status": "live"}]},
                {"id": "x/offline-model", "providers": [{"status": "error"}]}]})
        if url.endswith("/api/tags"):
            return httpx.Response(200, json={"models": [{"name": "qwen2:0.5b"}]})
        if url.endswith("/chat/completions"):
            body = json.loads(request.content)
            if seen is not None:
                seen.append(body["model"])
            if body["model"] in fail_models:
                return httpx.Response(status, json={"error": "rejected"})
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def test_discover_cross_provider_probes_and_orders_fallbacks(cfg):
    cfg["providers"]["prefer"] = "groq"
    report = model_router.discover(cfg, transport=_router_transport())
    ids = [c["model"] for c in report["candidates"]]
    assert "allam-2-7b" not in ids and "whisper-large-v3" not in ids and "x/offline-model" not in ids
    probed = [(c["provider"], c["model"]) for c in report["probed"]]
    assert probed == [("groq", "openai/gpt-oss-120b"), ("groq", "qwen/qwen3.8-27b"), ("groq", "openai/gpt-oss-20b"),
                      ("hf", "Qwen/Qwen2.5-Coder-32B-Instruct"), ("hf", "Qwen/Qwen3-32B"), ("ollama", "qwen2:0.5b")]
    assert (cfg["router"]["active_provider"], cfg["router"]["active_model"]) == ("groq", "openai/gpt-oss-120b")
    assert [(f["provider"], f["model"]) for f in cfg["router"]["fallback_models"]] == [
        ("groq", "qwen/qwen3.8-27b"), ("groq", "openai/gpt-oss-20b"),       # primary first ...
        ("hf", "Qwen/Qwen2.5-Coder-32B-Instruct"), ("hf", "Qwen/Qwen3-32B"),  # ... then secondaries
        ("ollama", "qwen2:0.5b")]


def test_discover_skips_provider_with_rejected_credentials(cfg):
    cfg["providers"]["prefer"] = "hf"
    hf_models = {"Qwen/Qwen2.5-Coder-32B-Instruct", "Qwen/Qwen3-32B", "meta-llama/Llama-3.3-70B-Instruct"}
    report = model_router.discover(cfg, transport=_router_transport(hf_models, status=402))
    assert report["winner"]["provider"] == "groq"
    assert any("hf: credentials/billing rejected" in n for n in report["notes"])
    assert all(f["provider"] != "hf" for f in cfg["router"]["fallback_models"])


def test_discover_without_keys_uses_ollama_only():
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    report = model_router.discover(cfg, transport=_router_transport())
    assert [c["provider"] for c in report["candidates"]] == ["ollama"]
    assert cfg["router"]["active_model"] == "qwen2:0.5b"


def test_discover_ollama_down_is_noted(cfg):
    def handler(request):
        if "/api/tags" in str(request.url):
            raise httpx.ConnectError("refused")
        return _router_transport().handle_request(request)
    report = model_router.discover(cfg, transport=httpx.MockTransport(handler))
    assert any(n.startswith("ollama: unavailable") for n in report["notes"])


# ---- chat: backoff and cross-provider failover --------------------------------------------
def _chain(cfg, *entries):
    (p0, m0), *rest = entries
    cfg["router"].update(active_provider=p0, active_model=m0,
                         fallback_models=[{"provider": p, "model": m} for p, m in rest])


def test_chat_backs_off_1_5_then_3_0_before_failing_over_to_secondary_provider(cfg, sleeps):
    _chain(cfg, ("groq", "m1"), ("hf", "m2"))
    seen = []
    provider, model, text, index = model_router.chat(cfg, [], transport=_router_transport({"m1"}, seen))
    assert (provider, model, index) == ("hf", "m2", 1) and json.loads(text) == {"ok": True}
    assert seen == ["m1", "m1", "m1", "m2"] and sleeps == [1.5, 3.0]


def test_chat_honours_longer_retry_after_on_same_model(cfg, sleeps):
    _chain(cfg, ("groq", "m1"))
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "7"}, json={})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    assert model_router.chat(cfg, [], transport=httpx.MockTransport(handler))[2] == "ok"
    assert sleeps == [7.0] and len(calls) == 2


def test_chat_cools_down_once_when_every_provider_is_rate_limited(cfg, sleeps):
    _chain(cfg, ("groq", "m1"), ("hf", "m2"))
    calls = []

    def handler(request):
        calls.append(json.loads(request.content)["model"])
        if len(calls) <= 6:  # 3 attempts each on both models, then the window resets
            return httpx.Response(429, json={})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    provider, model, text, _ = model_router.chat(cfg, [], transport=httpx.MockTransport(handler))
    assert (model, text) == ("m1", "ok")
    assert sleeps == [1.5, 3.0, 1.5, 3.0, llm_client.CHAIN_COOLDOWN_S]


def test_chat_raises_when_all_fail_after_cooldown(cfg, sleeps):
    _chain(cfg, ("hf", "m1"))
    with pytest.raises(model_router.ProviderUnavailable):
        model_router.chat(cfg, [], transport=_router_transport({"m1"}))
    assert llm_client.CHAIN_COOLDOWN_S in sleeps


def test_chat_5xx_fails_over_without_cooldown(cfg, sleeps):
    _chain(cfg, ("hf", "m1"))
    with pytest.raises(model_router.ProviderUnavailable):
        model_router.chat(cfg, [], transport=_router_transport({"m1"}, status=503))
    assert sleeps == []


def test_chat_requires_locked_model():
    with pytest.raises(model_router.ProviderUnavailable):
        model_router.chat(copy.deepcopy(DEFAULT_CONFIG), [])
