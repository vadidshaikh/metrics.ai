"""Live model enumeration, capability scoring, micro-probing and failover across providers."""

from __future__ import annotations

import fnmatch
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from metrics_ai.config import get_secret

GROQ_BASE = "https://api.groq.com/openai/v1"
HF_BASE = "https://router.huggingface.co/v1"
EXCLUDE = ("whisper", "tts", "guard", "embed", "vision", "audio", "moderation", "rerank")
# Ordered preference tiers: (glob over the lower-cased model basename, score).
TIERS = [
    ("qwen3-coder*", 100), ("qwen2.5-coder*", 97), ("gpt-oss-120b", 95),
    ("qwen3-235b*", 94), ("qwen3-32b*", 93), ("deepseek-v4*", 92), ("deepseek-v3*", 91),
    ("llama-3.3-70b*", 90), ("llama-4-*", 89), ("deepseek-r1*", 88),
    ("kimi-k2*", 87), ("glm-5*", 85), ("glm-4*", 83), ("gpt-oss-20b", 81), ("gemma-4*", 79),
    ("llama-3.1-8b*", 75),
]
DEFAULT_SCORE = 40
HF_STRUCTURED_BONUS = 5
PROBE_TOP_N = 4
PROBE_TIMEOUT_S = 4.0
RETRYABLE = {429, 500, 502, 503, 504}


@dataclass
class Candidate:
    provider: str
    model: str
    score: int
    size_b: float = 0.0
    probe_ms: float | None = None
    probe_ok: bool | None = None
    note: str = ""


class ProviderUnavailable(RuntimeError):
    """Raised when every configured model failed with a retryable error."""


def base_url(provider: str, cfg: dict[str, Any]) -> str:
    if provider == "groq":
        return GROQ_BASE
    if provider == "hf":
        return HF_BASE
    return cfg["providers"]["ollama_url"].rstrip("/") + "/v1"


def _headers(provider: str, cfg: dict[str, Any]) -> dict[str, str]:
    key = {"groq": "groq_api_key", "hf": "hf_token"}.get(provider)
    return {"Authorization": f"Bearer {get_secret(cfg, key)}"} if key else {}


def score_model(model_id: str, hf_meta: dict[str, Any] | None = None) -> int | None:
    """Capability score, or None when the model is excluded (non-text modality / not live)."""
    name = model_id.split("/")[-1].lower()
    if any(term in model_id.lower() for term in EXCLUDE):
        return None
    providers = (hf_meta or {}).get("providers") or []
    if providers and not any(p.get("status") == "live" for p in providers):
        return None
    score = next((s for pattern, s in TIERS if fnmatch.fnmatch(name, pattern)), DEFAULT_SCORE)
    if any(p.get("status") == "live" and p.get("supports_structured_output") for p in providers):
        score += HF_STRUCTURED_BONUS
    return score


def _size(model_id: str) -> float:
    sizes = [float(m) for m in re.findall(r"(\d+(?:\.\d+)?)b\b", model_id.lower())]
    return max(sizes, default=0.0)


def enumerate_models(cfg: dict[str, Any], client: httpx.Client) -> tuple[list[Candidate], list[str]]:
    candidates: list[Candidate] = []
    notes: list[str] = []
    sources = []
    if get_secret(cfg, "groq_api_key"):
        sources.append(("groq", f"{GROQ_BASE}/models"))
    if get_secret(cfg, "hf_token"):
        sources.append(("hf", f"{HF_BASE}/models"))
    sources.append(("ollama", cfg["providers"]["ollama_url"].rstrip("/") + "/api/tags"))
    for provider, url in sources:
        try:
            resp = client.get(url, headers=_headers(provider, cfg),
                              timeout=2.0 if provider == "ollama" else 10.0)
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            notes.append(f"{provider}: unavailable ({type(exc).__name__})")
            continue
        entries = payload.get("models", []) if provider == "ollama" else payload.get("data", [])
        for entry in entries:
            model_id = entry.get("name") or entry.get("model") if provider == "ollama" else entry.get("id")
            if not model_id:
                continue
            score = score_model(model_id, entry if provider == "hf" else None)
            if score is not None:
                candidates.append(Candidate(provider, model_id, score, _size(model_id)))
    return candidates, notes


def rank(candidates: list[Candidate], prefer: str = "") -> list[Candidate]:
    return sorted(candidates, key=lambda c: (c.provider == prefer, c.score, c.size_b), reverse=True)


def probe(cfg: dict[str, Any], cand: Candidate, client: httpx.Client) -> Candidate:
    body = {"model": cand.model, "max_tokens": 15, "temperature": 0,
            "messages": [{"role": "user", "content": 'Reply with exactly this JSON: {"ok": true}'}]}
    start = time.perf_counter()
    try:
        resp = client.post(f"{base_url(cand.provider, cfg)}/chat/completions", json=body,
                           headers=_headers(cand.provider, cfg), timeout=PROBE_TIMEOUT_S)
        cand.probe_ms = round((time.perf_counter() - start) * 1000.0, 1)
        cand.probe_ok = resp.status_code == 200 and bool(resp.json().get("choices"))
        cand.note = "" if cand.probe_ok else f"HTTP {resp.status_code}"
    except (httpx.HTTPError, ValueError) as exc:
        cand.probe_ok, cand.note = False, type(exc).__name__
    return cand


def discover(cfg: dict[str, Any], transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    """Enumerate -> filter -> score -> probe top 4 -> lock winner + fallbacks into `cfg`."""
    with httpx.Client(transport=transport) as client:
        candidates, notes = enumerate_models(cfg, client)
        ranked = rank(candidates, cfg["providers"].get("prefer", ""))
        top = ranked[:PROBE_TOP_N]
        with ThreadPoolExecutor(max_workers=PROBE_TOP_N) as pool:
            list(pool.map(lambda c: probe(cfg, c, client), top))
    passing = [c for c in top if c.probe_ok]
    router = cfg.setdefault("router", {})
    if passing:
        router["active_provider"], router["active_model"] = passing[0].provider, passing[0].model
        router["fallback_models"] = [{"provider": c.provider, "model": c.model} for c in passing[1:]]
        router["discovered_at"] = int(time.time())
    return {"candidates": [asdict(c) for c in ranked], "probed": [asdict(c) for c in top],
            "winner": asdict(passing[0]) if passing else None, "notes": notes,
            "total_discovered": len(candidates)}


def model_chain(cfg: dict[str, Any]) -> list[tuple[str, str]]:
    router = cfg.get("router", {})
    chain = [(router["active_provider"], router["active_model"])] if router.get("active_model") else []
    chain += [(f["provider"], f["model"]) for f in router.get("fallback_models", [])]
    return chain


def chat(cfg: dict[str, Any], messages: list[dict[str, str]], start_index: int = 0,
         transport: httpx.BaseTransport | None = None, max_tokens: int = 2000) -> tuple[str, str, str, int]:
    """Run a chat completion, failing over on 429/5xx. Returns (provider, model, text, chain index)."""
    chain = model_chain(cfg)
    if not chain:
        raise ProviderUnavailable("No active model locked. Run `metrics auth` first.")
    errors = []
    with httpx.Client(transport=transport, timeout=120.0) as client:
        for index in range(start_index, len(chain)):
            provider, model = chain[index]
            body = {"model": model, "messages": messages, "temperature": 0.2, "max_tokens": max_tokens}
            try:
                resp = client.post(f"{base_url(provider, cfg)}/chat/completions", json=body,
                                   headers=_headers(provider, cfg))
            except httpx.TransportError as exc:
                errors.append(f"{provider}/{model}: {type(exc).__name__}")
                continue
            if resp.status_code in RETRYABLE:
                errors.append(f"{provider}/{model}: HTTP {resp.status_code}")
                continue
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"].get("content") or ""
            return provider, model, content, index
    raise ProviderUnavailable("All models failed: " + "; ".join(errors))
