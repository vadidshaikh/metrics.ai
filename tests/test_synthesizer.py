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


def _run(chat, n_models=1, gate=None, role="tpm", others=None):
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

        def review(self, state, z, candidate, role, generator_model=""):
            assert generator_model == "model-0"  # the synthesizer tells the gate who wrote the candidate
            self.calls.append(role)
            if len(self.calls) == 1:
                bad = ReviewResult(0.95, 0.2, 0.9, "jev_api")
                bad.critique = diagnostic_critique(bad, self.thresholds)
                return bad
            return ReviewResult(0.95, 0.93, 0.92, "jev_api")

    gate = MetricWrongOnce()
    chat = FakeChat([json.dumps(_xyz()), json.dumps(_xyz())])
    result = _run(chat, gate=gate, role="tpm")
    assert result.attempts == 2 and result.review.metric_prob == 0.93 and gate.calls == ["tpm", "tpm"]
    feedback = chat.calls[1][-1]["content"]
    assert "metric=0.20" in feedback and "METRICS:" in feedback and "FLUENCY" not in feedback


def test_synthesize_rejects_a_clone_of_another_roles_bullet():
    from metrics_ai.synthesizer import clone_key

    from metrics_ai.synthesizer import _ratio

    first = _validate(_xyz())
    same_x_new_z = _xyz(z="consolidating per-row commits into one executemany transaction")
    distinct = _xyz(x="an 82.5% cut in end-to-end ingestion time",
                    z="consolidating per-row commits into one executemany transaction", cited=(1234.6, 216.0))
    chat = FakeChat([json.dumps(_xyz()), json.dumps(same_x_new_z), json.dumps(distinct)])
    result = _run(chat, role="tpm", others={"aiml": clone_key(first.model_dump())})
    assert result.attempts == 3
    assert "[Z] is 100% similar to the aiml [Z]" in chat.calls[1][-1]["content"] or \
        "[X] is 100% similar to the aiml [X]" in chat.calls[1][-1]["content"]
    assert "[X] is 100% similar to the aiml [X]" in chat.calls[2][-1]["content"]  # a new Z alone is not enough
    # [Y] is identical (same measured values) yet the bullet is accepted: only [X] and [Z] must differ.
    assert result.structured["y_measurement"] == first.y_measurement
    for part in ("x_accomplishment", "z_mechanism"):
        assert _ratio(result.structured[part], first.model_dump()[part]) < 0.70


def test_prompt_shows_other_roles_bullets():
    others = {"aiml": {"x": "a faster parsing pipeline", "z": "caching parses with lru_cache"}}
    content = build_messages(STATE, ALLOWED, "xyz", "tpm", "standard", others)[1]["content"]
    assert "OTHER_ROLES_ALREADY_WRITTEN" in content and "a faster parsing pipeline" in content
    assert "OTHER_ROLES_ALREADY_WRITTEN" not in build_messages(STATE, ALLOWED, "xyz", "tpm", "standard")[1]["content"]


def test_synthesize_fails_over_after_exhausting_retries():
    from metrics_ai.synthesizer import MAX_GUARD_RETRIES

    bad = json.dumps(_xyz(x=X.replace("82.5%", "83%")))
    per_model = MAX_GUARD_RETRIES + 1
    result = _run(FakeChat([bad] * per_model + [json.dumps(_xyz())]), n_models=2)
    assert result.model == "model-1" and result.attempts == per_model + 1


def test_synthesize_raises_when_every_model_fails():
    from metrics_ai.synthesizer import MAX_GUARD_RETRIES

    with pytest.raises(SynthesisError):
        _run(FakeChat(["not json"] * (MAX_GUARD_RETRIES + 1)))


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


def test_role_lenses_are_data_driven_and_check_what_z_leads_with():
    from metrics_ai.synthesizer import build_messages, lens_violations

    def bullet(**kw):
        obj = _validate(_xyz(**kw))
        return obj.x_accomplishment, obj.z_mechanism, obj.combined_xyz_bullet

    cache_first = "caching parsed source with lru_cache and batching inserts via executemany"
    aiml_x = "a leaner AST-parsing ingest pipeline"
    backend_x = "a single-transaction ledger write path"
    assert lens_violations("aiml", *bullet(x=aiml_x), STATE)                 # Z leads with batching, not caching
    assert not lens_violations("aiml", *bullet(x=aiml_x, z=cache_first), STATE)
    assert lens_violations("aiml", *bullet(z=cache_first), STATE)            # X names no parsing/profiling component
    assert lens_violations("aiml", *bullet(x="a 5.7x AST-parsing speedup", z=cache_first), STATE)  # mlops' multiplier
    assert not lens_violations("backend", *bullet(x=backend_x), STATE)      # X names the write path; Z leads batching
    assert lens_violations("backend", *bullet(), STATE)                      # X uses the % reserved for tpm
    assert lens_violations("backend", *bullet(x=backend_x, z=cache_first), STATE)
    # Units: measured latency values must carry "ms".
    assert lens_violations("backend", *bullet(x=backend_x, y="mean latency falling from 1234.6 to 216.0"), STATE)
    assert lens_violations("mlops", *bullet(), STATE)                        # X lacks the 5.7x speedup
    mlops = bullet(x="a 5.7x ingest benchmark speedup", y="latency falling from 1234.6 ms to 216.0 ms "
                   "with peak RSS 17.1% lower", z="caching parses with lru_cache and batching via executemany")
    assert not lens_violations("mlops", *mlops, STATE)
    assert not lens_violations("tpm", *bullet(), STATE)                      # X leads with 82.5%
    assert lens_violations("tpm", *mlops, STATE)
    assert lens_violations("tpm", *bullet(x="an 82.5% ingest latency reduction meeting the SLA"), STATE)
    # The requirements reach the prompt with values computed from the state, never hardcoded.
    content = build_messages(STATE, ALLOWED, "xyz", "mlops", "standard")[1]["content"]
    assert "'5.7x' speedup" in content and "'17.1%' or '40 MB'" in content


def test_redundant_x_y_and_bare_identifier_objects_are_rejected():
    from metrics_ai.prose import bare_identifier_objects, repeated_phrases

    assert repeated_phrases("a 95.6% reduction in mean latency", "mean latency from 3688.1 ms to 161.2 ms") == \
        ["mean latency"]
    assert repeated_phrases("a 95.6% reduction in ingest latency", "mean latency dropping from A to B") == []
    assert bare_identifier_objects("batching executemany, and caching results with lru_cache",
                                   {"executemany", "lru_cache"}) == ["batching executemany"]
    with pytest.raises(ValidationError, match="repeat"):
        _validate(_xyz(x="an 82.5% reduction in mean latency", y="mean latency falling from 1234.6 ms to 216.0 ms"))
    with pytest.raises(ValidationError, match="batching executemany"):
        _validate(_xyz(z="batching executemany and caching parsed source with lru_cache"))


def test_identifiers_as_modifiers_or_coordinated_with_nouns_are_allowed():
    from metrics_ai.prose import bare_identifier_objects

    idents = {"executemany", "lru_cache", "cached_profile", "insert_run", "profile_source"}
    assert bare_identifier_objects("caching cached_profile results with lru_cache", idents) == []
    assert bare_identifier_objects("hoisting insert_run and profile_source calls out of the loop", idents) == []
    assert bare_identifier_objects("batching via executemany and hoisting insert_run and profile_source",
                                   idents) == ["hoisting insert_run profile_source"]


def test_gerund_may_not_echo_the_identifier_stem():
    from metrics_ai.prose import stem_echoes

    idents = {"executemany", "cached_profile", "lru_cache"}
    assert stem_echoes("executing executemany for bulk inserts", idents) == ["executing executemany"]
    assert stem_echoes("caching cached_profile results with lru_cache", idents) == ["caching cached_profile"]
    assert stem_echoes("memoizing cached_profile results with lru_cache", idents) == []
    with pytest.raises(ValidationError, match="repeats the identifier's stem"):
        _validate(_xyz(z="executing executemany for bulk inserts and caching parses with lru_cache"))


def test_similarity_is_symmetric_so_role_order_cannot_hide_a_clone():
    import difflib

    from metrics_ai.role_lens import _ratio, too_similar

    # A real pair from the dogfood run where SequenceMatcher disagreed with itself by argument order.
    a, b = "a 95.9% reduction in profiling latency", "a 95.9% reduction in end-to-end workflow latency"
    forward, backward = (difflib.SequenceMatcher(None, s, t).ratio() for s, t in ((a, b), (b, a)))
    assert forward != backward and _ratio(a, b) == _ratio(b, a) == max(forward, backward) >= 0.70
    z = "batching writes via executemany"
    assert too_similar({"x": b, "z": z + " and hoisting calls"}, {"aiml": {"x": a, "z": "memoizing parses"}})


def test_headline_numbers_and_z_leads_are_partitioned_across_roles():
    from metrics_ai.synthesizer import lens_violations

    state = copy.deepcopy(STATE)
    state["ast_structural_diff"]["calls_hoisted_out_of_loops"] = ["insert_run"]
    cache_first = "caching parsed source with lru_cache and batching inserts via executemany"
    aiml = _validate(_xyz(x="an 82.5% cut in AST-parsing time", z=cache_first))
    assert any("reserved for the tpm bullet" in v for v in lens_violations(
        "aiml", aiml.x_accomplishment, aiml.z_mechanism, aiml.combined_xyz_bullet, state))
    mlops_y = "latency falling from 1234.6 ms to 216.0 ms with peak RSS 17.1% lower"
    hoist_first = "hoisting insert_run calls out of the loop and batching inserts via executemany"
    for z, ok in ((cache_first, False), (hoist_first, True)):
        obj = _validate(_xyz(x="a 5.7x ingest benchmark speedup", y=mlops_y, z=z))
        assert (not lens_violations("mlops", obj.x_accomplishment, obj.z_mechanism, obj.combined_xyz_bullet,
                                    state)) is ok


def test_vague_intensifiers_and_tpm_framing_are_kept_out_of_other_roles_x():
    from metrics_ai.synthesizer import lens_violations

    def violations(role, x):
        obj = _validate(_xyz(x=x, z="caching parsed source with lru_cache and batching inserts via executemany"))
        return lens_violations(role, obj.x_accomplishment, obj.z_mechanism, obj.combined_xyz_bullet, STATE)

    assert any("vague intensifier" in v for v in violations("aiml", "a major AST-parsing speedup"))
    assert any("tpm framing" in v for v in violations("aiml", "a reduction in AST-parsing latency"))
    assert not violations("aiml", "a memoized AST-parsing stage with no redundant per-record parses")
