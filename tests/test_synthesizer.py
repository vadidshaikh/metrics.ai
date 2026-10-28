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


def _run(chat, n_models=1, gate=None):
    return synthesize(_cfg(n_models), gate or DecisionGate(DEFAULT_CONFIG), STATE, ALLOWED, "xyz",
                      "backend", "standard", chat=chat)


def test_synthesize_retries_after_numeric_hallucination():
    chat = FakeChat([json.dumps(_xyz(x=X.replace("82.5%", "83%"))), json.dumps(_xyz())])
    result = _run(chat)
    assert result.attempts == 2 and "['83']" in chat.calls[1][-1]["content"]
    lines = result.output_text.splitlines()
    assert lines[1].startswith("├─ [X]") and lines[3].startswith("└─ [Z]")
    assert result.gate_mode == "offline_heuristic" and result.natural_prob >= 0.80


def test_synthesize_feeds_linter_error_back_for_by_doing_implementing():
    chat = FakeChat([json.dumps(_xyz(z="doing implementing executemany and lru_cache")), json.dumps(_xyz())])
    result = _run(chat)
    assert result.attempts == 2 and "by doing implementing" in chat.calls[1][-1]["content"]
    assert "by doing" not in result.output_text


def test_synthesize_reprompts_with_review_critique():
    class LowFluencyOnce:
        def __init__(self):
            self.calls = 0

        def review(self, state, z, candidate):
            from metrics_ai.decision_gate import ReviewResult
            self.calls += 1
            if self.calls == 1:
                return ReviewResult(0.95, 0.4, "llm_judge", "run-on clause")
            return ReviewResult(0.95, 0.92, "llm_judge", "")

    chat = FakeChat([json.dumps(_xyz()), json.dumps(_xyz())])
    result = _run(chat, gate=LowFluencyOnce())
    assert result.attempts == 2 and result.gate_mode == "llm_judge" and result.natural_prob == 0.92
    assert "run-on clause" in chat.calls[1][-1]["content"]


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
