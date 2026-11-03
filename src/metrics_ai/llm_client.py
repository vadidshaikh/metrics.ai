"""OpenAI-compatible chat calls with 429 backoff and cross-provider failover."""

from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx

from metrics_ai.config import get_secret

GROQ_BASE = "https://api.groq.com/openai/v1"
HF_BASE = "https://router.huggingface.co/v1"
RETRYABLE = {429, 500, 502, 503, 504}
BACKOFF_S = (1.5, 3.0)          # waits before the 2nd and 3rd attempt on a 429
MAX_RETRY_AFTER_S = 20.0        # longest Retry-After we are willing to honour inline
CHAIN_COOLDOWN_S = 20.0         # one extra pass over the whole chain when *everything* is rate-limited


class ProviderUnavailable(RuntimeError):
    """Raised when every configured model failed with a retryable error."""


def base_url(provider: str, cfg: dict[str, Any]) -> str:
    if provider == "groq":
        return GROQ_BASE
    if provider == "hf":
        return HF_BASE
    return cfg["providers"]["ollama_url"].rstrip("/") + "/v1"


def headers(provider: str, cfg: dict[str, Any]) -> dict[str, str]:
    key = {"groq": "groq_api_key", "hf": "hf_token"}.get(provider)
    return {"Authorization": f"Bearer {get_secret(cfg, key)}"} if key else {}


def retry_after(resp: httpx.Response) -> float | None:
    """Seconds to wait from a 429's Retry-After header, if present and short enough."""
    try:
        wait = float(resp.headers.get("retry-after", ""))
    except ValueError:
        return None
    return wait if 0 <= wait <= MAX_RETRY_AFTER_S else None


def model_chain(cfg: dict[str, Any]) -> list[tuple[str, str]]:
    router = cfg.get("router", {})
    chain = [(router["active_provider"], router["active_model"])] if router.get("active_model") else []
    chain += [(f["provider"], f["model"]) for f in router.get("fallback_models", [])]
    return chain


def _call_model(client: httpx.Client, cfg: dict[str, Any], provider: str, body: dict[str, Any]) -> httpx.Response:
    """POST with exponential backoff on 429 (1.5 s, 3.0 s, or a longer short Retry-After)."""
    url = f"{base_url(provider, cfg)}/chat/completions"
    resp = client.post(url, json=body, headers=headers(provider, cfg))
    for step in BACKOFF_S:
        if resp.status_code != 429:
            break
        time.sleep(max(step, retry_after(resp) or 0.0))
        resp = client.post(url, json=body, headers=headers(provider, cfg))
    return resp


def chat(cfg: dict[str, Any], messages: list[dict[str, str]], start_index: int = 0,
         transport: httpx.BaseTransport | None = None, max_tokens: int = 4000,
         temperature: float = 0.2) -> tuple[str, str, str, int]:
    """Run a chat completion, failing over on 429/5xx across providers.

    Returns (provider, model, text, chain index).
    """
    chain = model_chain(cfg)
    if not chain:
        raise ProviderUnavailable("No active model locked. Run `metrics auth` first.")
    errors: list[str] = []
    with httpx.Client(transport=transport, timeout=120.0) as client:
        for attempt in range(2):
            rate_limited_only = True
            for index in range(start_index, len(chain)):
                provider, model = chain[index]
                body = {"model": model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
                try:
                    resp = _call_model(client, cfg, provider, body)
                except httpx.TransportError as exc:
                    errors.append(f"{provider}/{model}: {type(exc).__name__}")
                    rate_limited_only = False
                    continue
                if resp.status_code in RETRYABLE:
                    errors.append(f"{provider}/{model}: HTTP {resp.status_code}")
                    rate_limited_only &= resp.status_code == 429
                    continue
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"].get("content") or ""
                return provider, model, content, index
            if attempt == 0 and rate_limited_only:
                time.sleep(CHAIN_COOLDOWN_S)  # every provider is rate-limited: wait once, then retry the chain
                continue
            break
    raise ProviderUnavailable("All models failed: " + "; ".join(errors))


def parse_json(text: str) -> dict[str, Any]:
    """Extract the JSON object from a model reply (tolerates <think> blocks and code fences)."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"```(?:json)?", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise json.JSONDecodeError("no JSON object found", text, 0)
    return json.loads(text[start:end + 1])
