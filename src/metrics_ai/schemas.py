"""Pydantic v2 structured-output schemas: numeric hallucination blocker + prose linter."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, ValidationInfo, model_validator

from metrics_ai.prose import assemble_xyz, lint_xyz, normalize_x, normalize_y, normalize_z, prose_defects

# Integers/decimals not glued to a preceding identifier (so `SQLite3` is ignored). Trailing units
# (`160ms`, `5.7x`) are allowed, and a match may never stop part-way through a decimal (`3435.1ms`).
NUMBER_RE = re.compile(r"(?<![A-Za-z0-9_.])(\d+(?:\.\d+)?)(?!\.?\d)")

DENSITY_RANGES = {
    "ultra-compact": (60, 115),
    "standard": (130, 220),
    "detailed": (220, 295),
}


def extract_numbers(text: str) -> list[float]:
    return [float(m.group(1)) for m in NUMBER_RE.finditer(text)]


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
    combined_xyz_bullet: str = ""
    cited_numbers: list[float] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def assemble(cls, data: Any) -> Any:
        """Normalize X/Y/Z and assemble the bullet in Python; the LLM's own sentence is discarded."""
        if isinstance(data, dict) and all(isinstance(data.get(k), str) for k in
                                          ("x_accomplishment", "y_measurement", "z_mechanism")):
            x, y, z = data["x_accomplishment"], data["y_measurement"], data["z_mechanism"]
            data = {**data, "x_accomplishment": normalize_x(x), "y_measurement": normalize_y(y),
                    "z_mechanism": normalize_z(z), "combined_xyz_bullet": assemble_xyz(x, y, z)}
        return data

    @model_validator(mode="after")
    def numbers_and_prose_must_pass(self, info: ValidationInfo) -> "XyzResponse":
        ctx = _ctx(info)
        allowed: set[float] = ctx["allowed_numbers"]
        bad = unknown_numbers([self.combined_xyz_bullet], allowed)
        if bad:
            raise ValueError(
                f"combined_xyz_bullet contains numbers {bad} that are not in allowed_numbers "
                f"{sorted(allowed)}. Use measured values verbatim; never round or invent metrics.")
        _check_cited(self.cited_numbers, allowed)
        problems = lint_xyz(self.x_accomplishment, self.z_mechanism, self.combined_xyz_bullet,
                            ctx.get("identifiers"), int(ctx.get("mechanism_count", 0)))
        density = ctx.get("density")
        if density in DENSITY_RANGES:
            lo, hi = DENSITY_RANGES[density]
            length = len(self.combined_xyz_bullet)
            if not lo <= length <= hi:
                problems.append(f"assembled bullet is {length} characters; density '{density}' "
                                f"requires {lo}-{hi} characters")
        if problems:
            raise ValueError("prose linter rejected the bullet: " + "; ".join(problems))
        return self


class StarResponse(BaseModel):
    situation: str = Field(min_length=10)
    task: str = Field(min_length=10)
    action: str = Field(min_length=10)
    result: str = Field(min_length=10)
    cited_numbers: list[float] = Field(default_factory=list)

    @model_validator(mode="after")
    def numbers_and_prose_must_pass(self, info: ValidationInfo) -> "StarResponse":
        allowed: set[float] = _ctx(info)["allowed_numbers"]
        fields = [self.situation, self.task, self.action, self.result]
        bad = unknown_numbers(fields, allowed)
        if bad:
            raise ValueError(f"STAR text contains numbers {bad} that are not in allowed_numbers "
                             f"{sorted(allowed)}. Use measured values verbatim.")
        _check_cited(self.cited_numbers, allowed)
        defects = [d for f in fields for d in prose_defects(f)]
        if defects:
            raise ValueError(f"prose linter rejected the STAR answer: awkward phrasing {defects}")
        return self
