"""Cross-provider model enumeration, capability scoring, micro-probing and fallback locking.

Chat/HTTP failover lives in `llm_client`; its public names are re-exported here for callers.
"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from metrics_ai.config import get_secret
from metrics_ai.llm_client import (GROQ_BASE, HF_BASE, ProviderUnavailable, base_url, chat,  # noqa: F401
                                   headers, model_chain, parse_json)

EXCLUDE = ("whisper", "tts", "guard", "embed", "vision", "audio", "moderation", "rerank",
           "orpheus",  # Groq's text-to-speech family, not caught by "tts"
           "allam")    # fails strict Pydantic JSON schemas; never used for code/JSON synthesis
# Ordered preference tiers: (regex searched in the lower-cased model basename, score).
TIERS = [
    (r"qwen3.*coder", 100), (r"qwen2\.5.*coder", 97), (r"gpt-oss-120b", 95),
    (r"qwen3(\.\d+)?-\d+b", 93), (r"deepseek-v4", 92), (r"deepseek-v3", 91), (r"llama-3\.3-70b", 90),
    (r"llama-4-", 89), (r"deepseek-r1", 88),
    (r"kimi-k2", 87), (r"glm-5", 85), (r"glm-4", 83), (r"gpt-oss-20b", 81), (r"gemma-4", 80),
    (r"llama-3\.1-70b", 78),
]
DEFAULT_SCORE = 40
HF_STRUCTURED_BONUS = 5
PRIMARY_PROBES = 3      # top models probed on the preferred provider
SECONDARY_PROBES = 2    # top models probed on every other configured provider
PROBE_TOP_N = 4
PROBE_TIMEOUT_S = 4.0
MAX_PROBE_BATCHES = 3
# Provider-level rejections (bad key, no credits, forbidden): no model on that provider will pass.
AUTH_FAILURES = {"HTTP 401", "HTTP 402", "HTTP 403"}


@dataclass
class Candidate:
    provider: str
    model: str
    score: int
    size_b: float = 0.0
    probe_ms: float | None = None
    probe_ok: bool | None = None
    note: str = ""


def score_model(model_id: str, hf_meta: dict[str, Any] | None = None) -> int | None:
    """Capability score, or None when the model is excluded (non-text modality / not live)."""
    name = model_id.split("/")[-1].lower()
    if any(term in model_id.lower() for term in EXCLUDE):
        return None
    providers = (hf_meta or {}).get("providers") or []
    if providers and not any(p.get("status") == "live" for p in providers):
        return None
    score = next((s for pattern, s in TIERS if re.search(pattern, name)), DEFAULT_SCORE)
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
            resp = client.get(url, headers=headers(provider, cfg), timeout=2.0 if provider == "ollama" else 10.0)
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
                           headers=headers(cand.provider, cfg), timeout=PROBE_TIMEOUT_S)
        cand.probe_ms = round((time.perf_counter() - start) * 1000.0, 1)
        cand.probe_ok = resp.status_code == 200 and bool(resp.json().get("choices"))
        cand.note = "" if cand.probe_ok else f"HTTP {resp.status_code}"
    except (httpx.HTTPError, ValueError) as exc:
        cand.probe_ok, cand.note = False, type(exc).__name__
    return cand


def probe_plan(ranked: list[Candidate], primary: str) -> list[Candidate]:
    """Top 3 on the primary provider plus the top 2 on every secondary provider, in rank order."""
    plan, per_provider = [], {}
    for cand in ranked:
        limit = PRIMARY_PROBES if cand.provider == primary else SECONDARY_PROBES
        if per_provider.get(cand.provider, 0) < limit:
            plan.append(cand)
            per_provider[cand.provider] = per_provider.get(cand.provider, 0) + 1
    return plan


def _probe_all(cfg: dict[str, Any], batch: list[Candidate], client: httpx.Client) -> None:
    with ThreadPoolExecutor(max_workers=max(1, min(8, len(batch)))) as pool:
        list(pool.map(lambda c: probe(cfg, c, client), batch))


def discover(cfg: dict[str, Any], transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    """Enumerate -> filter -> score -> cross-provider probe -> lock winner + fallbacks into `cfg`."""
    prefer = cfg["providers"].get("prefer", "")
    with httpx.Client(transport=transport) as client:
        candidates, notes = enumerate_models(cfg, client)
        ranked = rank(candidates, prefer)
        primary = prefer if any(c.provider == prefer for c in ranked) else (ranked[0].provider if ranked else "")
        probed = probe_plan(ranked, primary)
        _probe_all(cfg, probed, client)
        dead = {p for p in {c.provider for c in probed}
                if all(c.note in AUTH_FAILURES for c in probed if c.provider == p)}
        for provider in sorted(dead):
            notes.append(f"{provider}: credentials/billing rejected (HTTP 401/402/403); skipping provider")
        # Nothing passed: keep probing further batches of the healthy providers.
        for _batch in range(MAX_PROBE_BATCHES):
            if any(c.probe_ok for c in probed):
                break
            batch = [c for c in ranked if c not in probed and c.provider not in dead][:PROBE_TOP_N]
            if not batch:
                break
            _probe_all(cfg, batch, client)
            probed += batch
    passing = [c for c in ranked if c in probed and c.probe_ok]
    ordered = [c for c in passing if c.provider == primary] + [c for c in passing if c.provider != primary]
    router = cfg.setdefault("router", {})
    if ordered:
        router["active_provider"], router["active_model"] = ordered[0].provider, ordered[0].model
        router["fallback_models"] = [{"provider": c.provider, "model": c.model} for c in ordered[1:]]
        router["discovered_at"] = int(time.time())
    return {"candidates": [asdict(c) for c in ranked], "probed": [asdict(c) for c in probed],
            "winner": asdict(ordered[0]) if ordered else None, "notes": notes,
            "total_discovered": len(candidates), "primary_provider": primary}
