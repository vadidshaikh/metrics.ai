"""Pydantic v2 structured-output schemas with the numeric hallucination blocker."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, ValidationInfo, model_validator

# Integers/decimals not glued to identifiers (so `SQLite3` is ignored); a trailing `x` is allowed.
NUMBER_RE = re.compile(r"(?<![A-Za-z0-9_.])(\d+(?:\.\d+)?)(?=x\b|[^A-Za-z0-9_]|$)")

DENSITY_RANGES = {
    "ultra-compact": (1, 114),
    "standard": (150, 210),
    "detailed": (220, 280),
}


def extract_numbers(text: str) -> list[float]:
    return [float(m.group(1).rstrip(".")) for m in NUMBER_RE.finditer(text)]


def unknown_numbers(texts: list[str], allowed: set[float]) -> list[str]:
    bad = []
    for text in texts:
        for match in NUMBER_RE.finditer(text):
            value = float(match.group(1))
            if not any(abs(value - a) < 1e-9 for a in allowed):
                bad.append(match.group(1))
    return bad


def _ctx(info: ValidationInfo) -> dict[str, Any]:
    ctx = info.context or {}
    if "allowed_numbers" not in ctx:
        raise ValueError("validation context must provide 'allowed_numbers'")
    return ctx


def _check_cited(cited: list[float], allowed: set[float]) -> None:
    stray = [c for c in cited if not any(abs(float(c) - a) < 1e-9 for a in allowed)]
    if stray:
        raise ValueError(f"cited_numbers contains values not in the measured telemetry: {stray}")


class XyzResponse(BaseModel):
    x_accomplishment: str = Field(min_length=3)
    y_measurement: str = Field(min_length=3)
    z_mechanism: str = Field(min_length=3)
    combined_xyz_bullet: str = Field(min_length=10)
    cited_numbers: list[float] = Field(default_factory=list)

    @model_validator(mode="after")
    def numbers_must_be_measured(self, info: ValidationInfo) -> "XyzResponse":
        ctx = _ctx(info)
        allowed: set[float] = ctx["allowed_numbers"]
        bad = unknown_numbers([self.combined_xyz_bullet], allowed)
        if bad:
            raise ValueError(
                f"combined_xyz_bullet contains numbers {bad} that are not in allowed_numbers "
                f"{sorted(allowed)}. Use measured values verbatim; never round or invent metrics.")
        _check_cited(self.cited_numbers, allowed)
        density = ctx.get("density")
        if density in DENSITY_RANGES:
            lo, hi = DENSITY_RANGES[density]
            length = len(self.combined_xyz_bullet)
            if not lo <= length <= hi:
                raise ValueError(f"combined_xyz_bullet is {length} characters; density '{density}' "
                                 f"requires {lo}-{hi} characters.")
        return self


class StarResponse(BaseModel):
    situation: str = Field(min_length=10)
    task: str = Field(min_length=10)
    action: str = Field(min_length=10)
    result: str = Field(min_length=10)
    cited_numbers: list[float] = Field(default_factory=list)

    @model_validator(mode="after")
    def numbers_must_be_measured(self, info: ValidationInfo) -> "StarResponse":
        allowed: set[float] = _ctx(info)["allowed_numbers"]
        bad = unknown_numbers([self.situation, self.task, self.action, self.result], allowed)
        if bad:
            raise ValueError(f"STAR text contains numbers {bad} that are not in allowed_numbers "
                             f"{sorted(allowed)}. Use measured values verbatim.")
        _check_cited(self.cited_numbers, allowed)
        return self
