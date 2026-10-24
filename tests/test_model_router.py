from __future__ import annotations

import copy
import json

import httpx
import pytest

from metrics_ai import model_router
from metrics_ai.config import DEFAULT_CONFIG
from metrics_ai.model_router import Candidate, rank, score_model


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gk")
    monkeypatch.setenv("HF_TOKEN", "hk")
    return copy.deepcopy(DEFAULT_CONFIG)


def test_tier_scores():
    assert score_model("qwen/qwen3-coder-480b") == 100
    assert score_model("Qwen/Qwen2.5-Coder-32B-Instruct") == 97
    assert score_model("openai/gpt-oss-120b") == 95
    assert score_model("meta-llama/Llama-3.3-70B-Instruct") == 90
    assert 88 <= score_model("deepseek-ai/DeepSeek-V4-Pro") <= 94
    assert 75 <= score_model("moonshotai/Kimi-K2.5") <= 87
    assert score_model("llama-3.1-8b-instant") == 75
    assert score_model("mistral-small") == 40


@pytest.mark.parametrize("model_id", ["whisper-large-v3", "playai-tts", "llama-guard-4-12b",
                                      "nomic-embed-text", "llama-3.2-vision", "omni-moderation",
                                      "bge-reranker", "distil-audio", "canopylabs/orpheus-v1-english"])
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


def _router_transport(fail_models=(), seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("api.groq.com/openai/v1/models"):
            assert request.headers["authorization"] == "Bearer gk"
            return httpx.Response(200, json={"data": [{"id": "llama-3.3-70b-versatile"},
                                                      {"id": "whisper-large-v3"}, {"id": "gpt-oss-20b"}]})
        if url.endswith("router.huggingface.co/v1/models"):
            assert request.headers["authorization"] == "Bearer hk"
            return httpx.Response(200, json={"data": [
                {"id": "openai/gpt-oss-120b", "providers": [{"status": "live", "supports_structured_output": True}]},
                {"id": "Qwen/Qwen3-32B", "providers": [{"status": "live"}]},
                {"id": "x/offline-model", "providers": [{"status": "error"}]}]})
        if url.endswith("/api/tags"):
            return httpx.Response(200, json={"models": [{"name": "qwen2:0.5b"}]})
        if url.endswith("/chat/completions"):
            body = json.loads(request.content)
            if seen is not None:
                seen.append(body["model"])
            if body["model"] in fail_models:
                return httpx.Response(429, json={"error": "rate limited"})
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def test_discover_ranks_probes_and_locks(cfg):
    report = model_router.discover(cfg, transport=_router_transport(fail_models={"openai/gpt-oss-120b"}))
    ids = [c["model"] for c in report["candidates"]]
    assert "whisper-large-v3" not in ids and "x/offline-model" not in ids
    assert ids[0] == "openai/gpt-oss-120b" and len(report["probed"]) == 4
    assert report["winner"]["model"] == "Qwen/Qwen3-32B"
    assert cfg["router"]["active_provider"] == "hf"
    assert [f["model"] for f in cfg["router"]["fallback_models"]] == ["llama-3.3-70b-versatile", "gpt-oss-20b"]


def test_discover_skips_provider_with_rejected_credentials(cfg):
    def handler(request):
        if "/chat/completions" in str(request.url) and "ollama" not in str(request.url) \
                and "11434" not in str(request.url):
            return httpx.Response(401, json={"error": "Invalid username or password."})
        return _router_transport().handle_request(request)
    report = model_router.discover(cfg, transport=httpx.MockTransport(handler))
    assert report["winner"]["provider"] == "ollama"
    assert len(report["probed"]) == 5  # 4 remote failures, then the ollama batch
    assert any("rejected" in n for n in report["notes"])


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


def test_chat_fails_over_on_429(cfg):
    cfg["router"].update(active_provider="hf", active_model="m1",
                         fallback_models=[{"provider": "groq", "model": "m2"}])
    seen = []
    provider, model, text, index = model_router.chat(
        cfg, [{"role": "user", "content": "hi"}], transport=_router_transport({"m1"}, seen))
    assert (provider, model, index) == ("groq", "m2", 1) and seen == ["m1", "m2"]
    assert json.loads(text) == {"ok": True}


def test_chat_raises_when_all_fail(cfg):
    cfg["router"].update(active_provider="hf", active_model="m1", fallback_models=[])
    with pytest.raises(model_router.ProviderUnavailable):
        model_router.chat(cfg, [], transport=_router_transport({"m1"}))


def test_chat_requires_locked_model():
    with pytest.raises(model_router.ProviderUnavailable):
        model_router.chat(copy.deepcopy(DEFAULT_CONFIG), [])
