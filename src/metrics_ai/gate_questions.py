"""Canonical SystemOne `{type, instructions, criteria}` question definitions shared by all gate tiers."""

from __future__ import annotations

ROLES = ["aiml", "backend", "mlops", "tpm"]

ROLE_CRITERIA = {
    "aiml": "Embeddings, vector search, model inference, batching, caching, AST/token parsing, or RAG pipelines.",
    "backend": "Database queries, SQLite/MySQL connection pooling, bulk executemany transactions, API endpoints, "
               "or data structures.",
    "mlops": "Profiling wrappers, CI/CD benchmarking, container memory overhead, runtime telemetry monitoring, "
             "or deployment.",
    "tpm": "End-to-end system throughput scaling, latency SLA improvements, and maintainability metrics.",
}

IMPACT_LEVELS = [
    "negligible change (<5% runtime delta)",
    "minor cleanup (5% to 15% delta)",
    "solid module optimization (15% to 35% delta)",
    "high-impact bottleneck elimination (35% to 75% delta)",
    "order-of-magnitude breakthrough (>75% latency drop or >200% throughput gain)",
]

TRIAGE_QUESTIONS = {
    "is_resume_worthy": {
        "type": "noul",
        "instructions": "Does this state show a genuine code optimization or architectural improvement backed by "
                        "positive telemetry or complexity reduction in `telemetry_delta` and `ast_structural_diff`?",
        "criteria": {
            "true": "Substantive AST code refactor coupled with measurable latency reduction, throughput gain, "
                    "memory reduction, or cyclomatic simplification.",
            "false": "Formatting only, typo fix, comment edit, dead-code deletion without functional replacement, "
                     "or telemetry regression.",
        },
    },
    "impact_tier": {
        "type": "score",
        "instructions": "Rate the engineering magnitude of the pre-computed `telemetry_delta` and "
                        "`complexity_delta` values in the state.",
        "criteria": IMPACT_LEVELS,
    },
    "best_fit_role": {
        "type": "choice",
        "instructions": "Which engineering specialization best matches the technical mechanisms in "
                        "`ast_structural_diff`?",
        "criteria": ROLE_CRITERIA,
    },
}

REVIEW_QUESTIONS = {
    "is_mechanically_faithful": {
        "type": "noul",
        "instructions": "Is every technical mechanism, library, and architectural action claimed in "
                        "`candidate_bullet` directly supported by `ast_structural_diff` without inventing "
                        "unmentioned tools?",
        "criteria": {
            "true": "All technical claims accurately reflect the AST mechanisms and modified files.",
            "false": "The bullet invents technologies, databases, or changes absent from the state.",
        },
    },
    "is_prose_natural": {
        "type": "noul",
        "instructions": "Does `candidate_bullet` read as fluent, natural executive English free of duplicated "
                        "prepositions (such as 'by doing implementing'), fused noun-verb collisions, or run-on "
                        "clauses?",
        "criteria": {
            "true": "Grammatically clean, parallel, and concise technical prose.",
            "false": "Contains awkward phrasing such as 'by doing implementing', merged mechanisms lacking "
                     "conjunctions, or grammatical errors.",
        },
    },
}


def normalize_tier(raw_score: float) -> tuple[int, float]:
    """SystemOne `score` answers are 0-indexed (0.0..4.0); shift onto the 1..5 display scale."""
    score = min(max(float(raw_score), 0.0), 4.0) + 1.0
    return int(round(score)), round(score, 3)


def normalize_distribution(dist: dict[str, float]) -> dict[str, float]:
    clean = {r: max(float(dist.get(r, 0.0) or 0.0), 0.0) for r in ROLES}
    total = sum(clean.values())
    if total <= 0:
        return {r: round(1 / len(ROLES), 3) for r in ROLES}
    return {r: round(v / total, 3) for r, v in clean.items()}
