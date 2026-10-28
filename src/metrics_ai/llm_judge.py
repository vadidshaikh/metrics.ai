"""Tier 2: keyless structured LLM-as-decision-judge (temperature 0, strict Pydantic schema)."""

from __future__ import annotations

import json
from typing import Any, Callable

from pydantic import BaseModel, Field, ValidationError

from metrics_ai import model_router

JUDGE_SYSTEM = """You are a calibrated System-1 decision judge. You receive a JSON STATE and QUESTIONS.
Each question has `type`, `instructions` and `criteria`. Answer every question:
- type "noul": probability (0.0-1.0) that the "true" criterion holds rather than the "false" one.
- type "score": probability-weighted 0-indexed level (0.0-4.0) over the ordered `criteria` list.
- type "choice": probability distribution over the `criteria` keys, summing to 1.0.
Judge only from STATE. Never compute new metrics. Be strict: awkward or ungrammatical prose, or any
claim not supported by `ast_structural_diff`/`files_modified`, must score low.
Reply with ONE JSON object matching SCHEMA and nothing else."""

MAX_JUDGE_ATTEMPTS = 3


class JudgeError(RuntimeError):
    pass


class TriageJudgment(BaseModel):
    is_resume_worthy: float = Field(ge=0.0, le=1.0)
    impact_tier: float = Field(ge=0.0, le=4.0)
    role_distribution: dict[str, float]
    critique: str = ""


class ReviewJudgment(BaseModel):
    is_mechanically_faithful: float = Field(ge=0.0, le=1.0)
    is_prose_natural: float = Field(ge=0.0, le=1.0)
    critique: str = ""


TRIAGE_SCHEMA = ('{"is_resume_worthy": float 0-1, "impact_tier": float 0-4, '
                 '"role_distribution": {"aiml": float, "backend": float, "mlops": float, "tpm": float}, '
                 '"critique": str}')
REVIEW_SCHEMA = ('{"is_mechanically_faithful": float 0-1, "is_prose_natural": float 0-1, '
                 '"critique": str (name the exact defect when either score is low)}')


class LlmJudge:
    """Asks the model locked by `model_router` the same questions Jev would answer."""

    def __init__(self, cfg: dict[str, Any], chat: Callable[..., tuple[str, str, str, int]] | None = None):
        self.cfg = cfg
        self.chat = chat or model_router.chat
        self.last_model = ""

    def _ask(self, schema: type[BaseModel], shape: str, state: dict[str, Any],
             questions: dict[str, Any]) -> BaseModel:
        messages = [
            {"role": "system", "content": JUDGE_SYSTEM},
            {"role": "user", "content": json.dumps({"STATE": state, "QUESTIONS": questions,
                                                    "SCHEMA": shape}, indent=1)},
        ]
        errors = []
        for _ in range(MAX_JUDGE_ATTEMPTS):
            try:
                provider, model, text, _ = self.chat(self.cfg, messages, temperature=0.0, max_tokens=3000)
            except model_router.ProviderUnavailable as exc:
                raise JudgeError(str(exc)) from exc
            self.last_model = f"{provider}/{model}"
            try:
                return schema.model_validate(model_router.parse_json(text))
            except (ValidationError, json.JSONDecodeError) as exc:
                errors.append(str(exc).splitlines()[0])
                messages += [{"role": "assistant", "content": text},
                             {"role": "user", "content": f"Invalid reply: {exc}\nReturn JSON matching SCHEMA only."}]
        raise JudgeError("LLM judge returned no valid verdict: " + " | ".join(errors))

    def triage(self, state: dict[str, Any], questions: dict[str, Any]) -> TriageJudgment:
        return self._ask(TriageJudgment, TRIAGE_SCHEMA, state, questions)

    def review(self, state: dict[str, Any], questions: dict[str, Any]) -> ReviewJudgment:
        return self._ask(ReviewJudgment, REVIEW_SCHEMA, state, questions)
