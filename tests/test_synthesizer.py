from __future__ import annotations

import copy
import json

import pytest
from pydantic import ValidationError

from metrics_ai.collector import allowed_numbers
from metrics_ai.config import DEFAULT_CONFIG
from metrics_ai.decision_gate import DecisionGate
from metrics_ai.model_router import parse_json
from metrics_ai.prose import assemble_xyz, normalize_z, prose_defects
from metrics_ai.renderers import render_pr_md
from metrics_ai.schemas import StarResponse, XyzResponse, extract_numbers
from metrics_ai.synthesizer import (SynthesisError, build_messages, ledger_hash, synthesize,
                                    validation_context)
from tests.conftest import make_state

STATE = make_state(latency=(1234.56, 216.04), memory=(48.26, 40.0),
                   extra_diff={"caching_decorators_added": ["lru_cache"],
                               "mechanisms": ["batched database writes into one transaction via executemany()",
                                              "cached repeated parse() results in memory with @lru_cache"]})
ALLOWED = allowed_numbers(STATE)
CTX = validation_context(STATE, ALLOWED, "xyz", "standard")

X = "an 82.5% reduction in ledger ingest latency"
Y = "mean latency falling from 1234.6 ms to 216.0 ms"
Z = "batching SQLite inserts with executemany and caching parsed source with lru_cache"


def _xyz(x=X, y=Y, z=Z, cited=(82.5, 1234.6, 216.0)):
    return {"x_accomplishment": x, "y_measurement": y, "z_mechanism": z, "cited_numbers": list(cited)}


def _validate(data, ctx=CTX):
    return XyzResponse.model_validate(data, context=ctx)


# ---- numeric guard ----------------------------------------------------------------------
def test_number_extraction():
    assert extract_numbers("SQLite3 and py311 got 5.7x faster, 82.5% less, 40 MB.") == [5.7, 82.5, 40.0]
    assert extract_numbers("from 3435.1ms to 160ms, 22.4MB peak") == [3435.1, 160.0, 22.4]


def test_valid_bullet_is_assembled_in_python():
    obj = _validate(_xyz())
    assert obj.combined_xyz_bullet == f"Accomplished {X} as measured by {Y}, by {Z}."
    assert 130 <= len(obj.combined_xyz_bullet) <= 220


def test_rounded_number_is_rejected():
    with pytest.raises(ValidationError, match=r"\['83'\]"):
        _validate(_xyz(x=X.replace("82.5%", "83%")))


def test_invented_metric_and_bad_cited_number_are_rejected():
    with pytest.raises(ValidationError, match="not in allowed_numbers"):
        _validate(_xyz(y=Y + " across 10 hours"))
    with pytest.raises(ValidationError, match="cited_numbers"):
        _validate(_xyz(cited=(99.9,)))


# ---- prose linter -----------------------------------------------------------------------
def test_by_doing_prefix_is_normalized_away():
    assert normalize_z("by doing batching writes") == "batching writes"
    assert normalize_z("By through via batching writes.") == "batching writes"
    obj = _validate(_xyz(z="by doing " + Z))
    assert ", by batching SQLite" in obj.combined_xyz_bullet and "by doing" not in obj.combined_xyz_bullet


def test_by_doing_implementing_is_rejected():
    # "doing implementing" survives prefix stripping, so the assembled "by doing implementing" must fail.
    with pytest.raises(ValidationError, match="by doing implementing"):
        _validate(_xyz(z="doing implementing executemany batching and lru_cache caching"))
    assert prose_defects("Accomplished x, by doing implementing y.") == ["by doing implementing"]
    assert prose_defects("solved by by via doing parsing") == ["by by", "doing parsing"]
    assert prose_defects(assemble_xyz("a", "b", "batching y")) == []


def test_merged_mechanisms_without_conjunction_are_rejected():
    with pytest.raises(ValidationError, match="'and' or 'while'"):
        _validate(_xyz(z="batching SQLite inserts with executemany plus lru_cache-cached parsing"))


def test_x_must_be_noun_phrase_and_z_must_be_gerund():
    with pytest.raises(ValidationError, match="noun phrase"):
        _validate(_xyz(x="reduced ledger ingest latency by 82.5%"))
    with pytest.raises(ValidationError, match="gerund"):
        _validate(_xyz(z="executemany batching and lru_cache caching of parsed source"))


def test_bullet_must_cite_a_concrete_identifier():
    with pytest.raises(ValidationError, match="concrete identifier"):
        _validate(_xyz(z="batching database inserts into one transaction and caching repeated parses"))


def test_density_bounds():
    ultra = validation_context(STATE, ALLOWED, "xyz", "ultra-compact")
    with pytest.raises(ValidationError, match="ultra-compact"):
        _validate(_xyz(), ultra)
    with pytest.raises(ValidationError, match="allowed_numbers"):
        XyzResponse.model_validate(_xyz())


def test_star_validator_numbers_and_prose():
    star = {"situation": "Ledger ingest was slow at 1234.6 ms.", "task": "Reduce ingest latency.",
            "action": "Batched inserts with executemany.", "result": "Latency fell 82.5% to 216.0 ms.",
            "cited_numbers": [1234.6, 82.5, 216.0]}
    StarResponse.model_validate(star, context={"allowed_numbers": ALLOWED})
    with pytest.raises(ValidationError):
        StarResponse.model_validate({**star, "result": "Latency fell 80%."}, context={"allowed_numbers": ALLOWED})
    with pytest.raises(ValidationError, match="awkward"):
        StarResponse.model_validate({**star, "action": "Improved it by doing implementing batches."},
                                    context={"allowed_numbers": ALLOWED})


def test_parse_json_strips_think_and_fences():
    assert parse_json('<think>hmm {"no": 1}</think>```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(json.JSONDecodeError):
        parse_json("no json here")


def test_prompt_lists_numbers_mechanisms_identifiers_and_budget():
    content = build_messages(STATE, ALLOWED, "xyz", "backend", "standard")[1]["content"]
    assert '"82.5"' in content and "MECHANISMS" in content and "executemany" in content
    assert "130-220 characters" in content and "gerund" in content


# ---- synthesis loop ---------------------------------------------------------------------
class FakeChat:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, cfg, messages, start_index=0, **kwargs):
        self.calls.append(copy.deepcopy(messages))
        return "hf", f"model-{start_index}", self.replies.pop(0), start_index


def _cfg(n_models=1):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["router"].update(active_provider="hf", active_model="m0",
                         fallback_models=[{"provider": "hf", "model": f"m{i}"} for i in range(1, n_models)])
    return cfg


def _run(chat, n_models=1, gate=None, role="backend", others=None):
    return synthesize(_cfg(n_models), gate or DecisionGate(DEFAULT_CONFIG, offline=True), STATE, ALLOWED, "xyz",
                      role, "standard", chat=chat, others=others)


def test_synthesize_retries_after_numeric_hallucination():
    chat = FakeChat([json.dumps(_xyz(x=X.replace("82.5%", "83%"))), json.dumps(_xyz())])
    result = _run(chat)
    assert result.attempts == 2 and "['83']" in chat.calls[1][-1]["content"]
    lines = result.output_text.splitlines()
    assert lines[1].startswith("├─ [X]") and lines[3].startswith("└─ [Z]")
    assert result.review.gate_mode == "offline_heuristic" and result.review.natural_prob >= 0.80


def test_synthesize_feeds_linter_error_back_for_by_doing_implementing():
    chat = FakeChat([json.dumps(_xyz(z="doing implementing executemany and lru_cache")), json.dumps(_xyz())])
    result = _run(chat)
    assert result.attempts == 2 and "by doing implementing" in chat.calls[1][-1]["content"]
    assert "by doing" not in result.output_text


def test_synthesize_reprompts_with_diagnostic_review_critique():
    from metrics_ai.decision_gate import ReviewResult, diagnostic_critique

    class MetricWrongOnce:
        thresholds = {"is_mechanically_faithful": 0.8, "is_metric_accurate": 0.8, "is_prose_natural": 0.8}

        def __init__(self):
            self.calls = []

        def review(self, state, z, candidate, role):
            self.calls.append(role)
            if len(self.calls) == 1:
                bad = ReviewResult(0.95, 0.2, 0.9, "jev_api")
                bad.critique = diagnostic_critique(bad, self.thresholds)
                return bad
            return ReviewResult(0.95, 0.93, 0.92, "jev_api")

    gate = MetricWrongOnce()
    chat = FakeChat([json.dumps(_xyz()), json.dumps(_xyz())])
    result = _run(chat, gate=gate, role="mlops")
    assert result.attempts == 2 and result.review.metric_prob == 0.93 and gate.calls == ["mlops", "mlops"]
    feedback = chat.calls[1][-1]["content"]
    assert "metric=0.20" in feedback and "METRICS:" in feedback and "FLUENCY" not in feedback


def test_synthesize_rejects_a_clone_of_another_roles_bullet():
    first = _validate(_xyz()).combined_xyz_bullet
    distinct = _xyz(x="a 5.7x speedup in the ledger ingest benchmark",
                    z="caching parsed source with lru_cache and batching inserts via executemany")
    chat = FakeChat([json.dumps(_xyz()), json.dumps(distinct)])
    result = _run(chat, role="mlops", others={"backend": first})
    assert result.attempts == 2 and "similar to the backend bullet" in chat.calls[1][-1]["content"]
    assert result.structured["combined_xyz_bullet"] != first


def test_synthesize_fails_over_after_exhausting_retries():
    bad = json.dumps(_xyz(x=X.replace("82.5%", "83%")))
    result = _run(FakeChat([bad] * 4 + [json.dumps(_xyz())]), n_models=2)
    assert result.model == "model-1" and result.attempts == 5


def test_synthesize_raises_when_every_model_fails():
    with pytest.raises(SynthesisError):
        _run(FakeChat(["not json"] * 4))


def test_pr_markdown_and_ledger_hash():
    md = render_pr_md(STATE)
    assert "| Mean latency (ms) | 1234.6 | 216.0 | -82.5% | ✅ improved |" in md and "`executemany`" in md
    assert ledger_hash(STATE, "a") == ledger_hash(copy.deepcopy(STATE), "a") != ledger_hash(STATE, "b")


def test_y_must_be_a_noun_phrase_not_a_clause():
    with pytest.raises(ValidationError, match=r"\[Y\] must be a noun phrase"):
        _validate(_xyz(y="mean latency fell from 1234.6 ms to 216.0 ms"))


def test_code_punctuation_is_stripped_and_single_mechanism_needs_no_conjunction():
    obj = _validate(_xyz(z="batching SQLite inserts into one transaction via executemany()"))
    assert "executemany()" not in obj.combined_xyz_bullet and "via executemany." in obj.combined_xyz_bullet
    assert obj.z_mechanism.endswith("via executemany")  # the [Z] tree line matches the bullet
    assert "@" not in _validate(_xyz(z="batching inserts with executemany and caching parses with @lru_cache"))\
        .combined_xyz_bullet


def test_unmeasured_metric_claims_are_rejected():
    # make_state has no throughput telemetry, so "throughput" must be refused.
    with pytest.raises(ValidationError, match="throughput"):
        _validate(_xyz(x="a 5.7x speedup in ingest throughput"))


def test_star_rejects_double_negative_and_unmeasured_metric():
    ctx = validation_context(STATE, ALLOWED, "star", "standard")
    star = {"situation": "Ledger ingest took 1234.6 ms per benchmark run.", "task": "Reduce ingest latency.",
            "action": "Batched inserts with executemany and cached parses with lru_cache.",
            "result": "Mean latency fell to 216.0 ms, an 82.5% reduction.", "cited_numbers": [1234.6, 216.0, 82.5]}
    StarResponse.model_validate(star, context=ctx)
    with pytest.raises(ValidationError, match="double negative"):
        StarResponse.model_validate({**star, "result": "Latency fell to 216.0 ms, a reduction of -82.5%."},
                                    context=ctx)
    with pytest.raises(ValidationError, match="throughput"):
        StarResponse.model_validate({**star, "situation": "Slow ingest at 1234.6 ms constrained throughput."},
                                    context=ctx)


def test_multiplier_reduction_is_rejected_but_speedup_is_fine():
    with pytest.raises(ValidationError, match="multiplier misuse"):
        _validate(_xyz(x="a 5.7x reduction in ledger ingest latency"))
    assert "5.7x speedup" in _validate(_xyz(x="a 5.7x speedup in ledger ingest")).combined_xyz_bullet


def test_star_rejects_single_sample_and_accepts_iteration_count():
    ctx = validation_context(STATE, ALLOWED, "star", "standard")
    star = {"situation": "Ledger ingest took 1234.6 ms averaged over 3 benchmark iterations.",
            "task": "Reduce ingest latency.", "action": "Batched inserts with executemany.",
            "result": "Mean latency fell to 216.0 ms, an 82.5% reduction.", "cited_numbers": [1234.6, 3, 216.0, 82.5]}
    StarResponse.model_validate(star, context=ctx)
    with pytest.raises(ValidationError, match="single sample"):
        StarResponse.model_validate({**star, "result": star["result"] + " The measurement was a single sample."},
                                    context=ctx)
    with pytest.raises(ValidationError, match="multiplier misuse"):
        StarResponse.model_validate({**star, "result": "Latency saw a 5.7x reduction."}, context=ctx)
