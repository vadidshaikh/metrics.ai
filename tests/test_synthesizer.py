from __future__ import annotations

import copy
import json

import pytest
from pydantic import ValidationError

from metrics_ai.collector import allowed_numbers
from metrics_ai.config import DEFAULT_CONFIG
from metrics_ai.decision_gate import DecisionGate
from metrics_ai.renderers import render_pr_md
from metrics_ai.schemas import StarResponse, XyzResponse, extract_numbers
from metrics_ai.synthesizer import SynthesisError, build_messages, ledger_hash, parse_json, synthesize
from tests.conftest import make_state

STATE = make_state(latency=(1234.56, 216.04), memory=(48.26, 40.0),
                   extra_diff={"caching_decorators_added": ["lru_cache"]})
ALLOWED = allowed_numbers(STATE)


def _xyz(bullet, cited=(82.5,), z="batching inserts with executemany"):
    return {"x_accomplishment": "Cut ingest latency", "y_measurement": "82.5% lower mean latency",
            "z_mechanism": z, "combined_xyz_bullet": bullet, "cited_numbers": list(cited)}


GOOD = ("Cut ledger ingest latency by 82.5% (1234.6 ms to 216.0 ms, 5.7x faster) as measured by "
        "psutil wall-clock profiling, by batching SQLite inserts through executemany.")


def test_number_extraction_ignores_identifiers_and_allows_x_suffix():
    assert extract_numbers("SQLite3 and py311 got 5.7x faster, 82.5% less, 40 MB.") == [5.7, 82.5, 40.0]


def test_validator_accepts_measured_numbers():
    obj = XyzResponse.model_validate(_xyz(GOOD), context={"allowed_numbers": ALLOWED, "density": "standard"})
    assert obj.cited_numbers == [82.5]


def test_validator_rejects_rounded_number():
    with pytest.raises(ValidationError, match=r"\['83'\]"):
        XyzResponse.model_validate(_xyz(GOOD.replace("82.5%", "83%")), context={"allowed_numbers": ALLOWED})


def test_validator_rejects_invented_metric_and_bad_cited():
    with pytest.raises(ValidationError):
        XyzResponse.model_validate(_xyz(GOOD + " Saved 10 hours."), context={"allowed_numbers": ALLOWED})
    with pytest.raises(ValidationError, match="cited_numbers"):
        XyzResponse.model_validate(_xyz(GOOD, cited=(99.9,)), context={"allowed_numbers": ALLOWED})


def test_validator_enforces_density_and_requires_context():
    with pytest.raises(ValidationError, match="ultra-compact"):
        XyzResponse.model_validate(_xyz(GOOD), context={"allowed_numbers": ALLOWED, "density": "ultra-compact"})
    with pytest.raises(ValidationError, match="allowed_numbers"):
        XyzResponse.model_validate(_xyz(GOOD))


def test_star_validator():
    star = {"situation": "Ledger ingest was slow at 1234.6 ms.", "task": "Reduce ingest latency.",
            "action": "Batched inserts with executemany.", "result": "Latency fell 82.5% to 216.0 ms.",
            "cited_numbers": [1234.6, 82.5, 216.0]}
    StarResponse.model_validate(star, context={"allowed_numbers": ALLOWED})
    with pytest.raises(ValidationError):
        StarResponse.model_validate({**star, "result": "Latency fell 80%."}, context={"allowed_numbers": ALLOWED})


def test_parse_json_strips_think_and_fences():
    assert parse_json('<think>hmm {"no": 1}</think>```json\n{"a": 1}\n```') == {"a": 1}
    with pytest.raises(json.JSONDecodeError):
        parse_json("no json here")


def test_prompt_lists_allowed_numbers_and_density():
    msgs = build_messages(STATE, ALLOWED, "xyz", "backend", "standard")
    assert "150-210 characters" in msgs[1]["content"] and '"82.5"' in msgs[1]["content"]


class FakeChat:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, cfg, messages, start_index=0):
        self.calls.append(copy.deepcopy(messages))
        return "hf", f"model-{start_index}", self.replies.pop(0), start_index


def _cfg(n_models=1):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["router"].update(active_provider="hf", active_model="m0",
                         fallback_models=[{"provider": "hf", "model": f"m{i}"} for i in range(1, n_models)])
    return cfg


def test_synthesize_retries_after_numeric_hallucination():
    chat = FakeChat([json.dumps(_xyz(GOOD.replace("82.5%", "83%"))), json.dumps(_xyz(GOOD))])
    result = synthesize(_cfg(), DecisionGate(DEFAULT_CONFIG), STATE, ALLOWED, "xyz", "backend", "standard", chat=chat)
    assert result.attempts == 2
    assert "['83']" in chat.calls[1][-1]["content"]
    assert result.output_text.splitlines()[1].startswith("├─ [X]")
    assert result.output_text.splitlines()[3].startswith("└─ [Z]")


def test_synthesize_reprompts_on_unfaithful_mechanism():
    bad = _xyz(GOOD.replace("executemany", "Redis"), z="moving storage to Redis and Kafka")
    chat = FakeChat([json.dumps(bad), json.dumps(_xyz(GOOD))])
    result = synthesize(_cfg(), DecisionGate(DEFAULT_CONFIG), STATE, ALLOWED, "xyz", "backend", "standard", chat=chat)
    assert result.attempts == 2 and result.faithful_prob >= 0.75
    assert "STRICTER" in chat.calls[1][-1]["content"]


def test_synthesize_fails_over_to_next_model_after_exhausting_retries():
    bad = json.dumps(_xyz(GOOD.replace("82.5%", "83%")))
    chat = FakeChat([bad] * 4 + [json.dumps(_xyz(GOOD))])
    result = synthesize(_cfg(2), DecisionGate(DEFAULT_CONFIG), STATE, ALLOWED, "xyz", "aiml", "standard", chat=chat)
    assert result.model == "model-1" and result.attempts == 5


def test_synthesize_raises_when_every_model_fails():
    chat = FakeChat(["not json"] * 4)
    with pytest.raises(SynthesisError):
        synthesize(_cfg(), DecisionGate(DEFAULT_CONFIG), STATE, ALLOWED, "xyz", "aiml", "standard", chat=chat)


def test_pr_markdown_table_uses_exact_numbers():
    md = render_pr_md(STATE)
    assert "| Mean latency (ms) | 1234.6 | 216.0 | -82.5% | ✅ improved |" in md
    assert "| Speedup | 1.0x | 5.7x |" in md
    assert "#### AST summary" in md and "`executemany`" in md


def test_ledger_hash_is_deterministic_and_output_sensitive():
    assert ledger_hash(STATE, "a") == ledger_hash(copy.deepcopy(STATE), "a")
    assert ledger_hash(STATE, "a") != ledger_hash(STATE, "b")
