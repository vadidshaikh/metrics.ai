# metrics.ai

A local-first developer telemetry, benchmarking and impact synthesis engine. `metrics` profiles real
baseline-vs-target runs of your code and diffs the commits' Python ASTs and cyclomatic complexity. It
computes every delta in Python, then turns them into Google XYZ resume bullets, STAR interview answers or
PR Markdown tables. A numeric guard and a prose linter reject any unmeasured number or clumsy phrasing.

## Install

```bash
pip install metrics-ai          # or, from a checkout: pip install -e .
```

## Quickstart

```bash
metrics init                                   # .metrics/ledger.db, config.toml, .gitignore safety (--hook for post-commit)
metrics auth --groq-key gsk_...                # or --hf-token / local Ollama; auto-discovers + locks the best model
metrics auth --jev-key sk-... [--jev-endpoint URL --jev-model NAME]   # optional Tier-1 SystemOne gate
git checkout <baseline>  && metrics wrap "python bench.py"
git checkout <target>    && metrics wrap "python bench.py"
metrics generate --format xyz --role auto      # xyz | star | pr-md ; roles auto|aiml|backend|mlops|tpm|all
metrics prune --keep-latest                    # or --all to start from an empty ledger
metrics ui                                     # dashboard at http://127.0.0.1:8420
```

`metrics pytest [ARGS]` ingests `pytest-benchmark` results instead of wall-clock profiling.
`metrics jev-request` prints the exact SystemOne request body, for running it by hand (for example in a
Jev playground); `metrics generate --jev-response reply.json` then uses that reply as the Tier-1 triage.

## How it works

1. **Capture** (`collector.py`, `ast_diff.py`, `profiler.py`). Diffs the Python files between two commits
   and lists each distinct structural mechanism as its own item, for example "batched database writes
   into one transaction via executemany()". It also measures `radon` complexity and net LOC. `wrap`
   samples the process tree's RSS every 10 ms with `psutil`.
2. **Arithmetic.** Every percentage, ratio and speedup is computed in Python and rounded to 1 decimal.
   Each one goes into an `allowed_numbers` set.
3. **Stage 1, decision gate** (`decision_gate.py`, `gate_questions.py`). Asks `is_resume_worthy` (noul),
   `impact_tier` (score, 0-indexed, shown as 1–5) and `best_fit_role` (choice), using the SystemOne
   `{model, state, questions}` contract. It uses one of three tiers, recorded per row as `gate_mode`:
   * **`jev_api`:** a configured SystemOne endpoint, either TypeSafe Jev or a compatible server such as
     OpenJev.
   * **`llm_judge`:** the same questions answered by the locked LLM at temperature 0 into a strict
     Pydantic schema (`llm_judge.py`), used when no key is set.
   * **`offline_heuristic`:** deterministic rules, used only with `--offline-no-llm` or when no model is
     reachable.

   Synthesis halts below p=0.60 unless `--force` is passed.
4. **Stage 2, synthesis** (`synthesizer.py`, `schemas.py`, `prose.py`). The LLM writes the X, Y and Z
   parts. Python strips any leading "by doing"/"by"/"via" and code punctuation, then assembles
   *"Accomplished X as measured by Y, by Z."* The Pydantic validator rejects:
   * any number not in `allowed_numbers`;
   * "by doing <gerund>"-style collisions;
   * an X or Y written as a clause instead of a noun phrase, or a Z that doesn't start with a gerund;
   * several mechanisms with no "and";
   * double negatives such as "a reduction of -95.7%";
   * a metric that was never measured;
   * no concrete code identifier, or a length outside the `--density` bounds.

   The error goes back to the model, with up to 3 retries.
5. **Stage 3, review.** `is_mechanically_faithful` and `is_prose_natural` must both reach ≥ 0.80. Below
   that, the model is re-prompted.
6. **Model routing** (`model_router.py`). Lists live Groq, Hugging Face Router and Ollama models, drops
   non-text models, scores the rest by tier and probes the top 4. If none pass, it probes further batches,
   skipping providers that return 401/402/403. The winner and fallbacks are locked into `config.toml`. A
   429 is retried once after a short `Retry-After`; otherwise a 429 or 5xx fails over to the next model.

Environment variables (`GROQ_API_KEY`, `HF_TOKEN`, `TYPESAFE_API_KEY`/`JEV_API_KEY`) override keys
stored in `.metrics/config.toml`, and `metrics auth` warns when that happens. Keys are only written after
`.metrics/` is confirmed to be git-ignored.

## GitHub Action

```yaml
- uses: actions/checkout@v4
  with: { fetch-depth: 0 }
- uses: ./            # or <owner>/metrics.ai@v0
  with:
    command: python benchmarks/bench_ledger_ingest.py
```

The action profiles the PR base and head commits and writes the `pr-md` table to the job summary and to
the `markdown` output.

## Live Dogfooded Example

`metrics.ai` profiled its own benchmark (`benchmarks/bench_ledger_ingest.py`: 500 ledger inserts plus AST
profiling of `collector.py`) on Windows 11 with Python 3.14.7, starting from an empty ledger. Commit A
wrote each row with its own commit and re-parsed the AST for every record. Commit B uses one
`executemany()` transaction and an `lru_cache`'d AST profile.

Both gates ran as **`gate_mode = jev_api`**, answered live by OpenJev (`openjev-latest`, a Jev-compatible
model) at codiv.ai. Bullets were written by Groq models. Full terminal output, the raw SQLite rows and the
remaining limitations are in [VERIFICATION.md](VERIFICATION.md).

| | Commit | `mean_latency_ms` (SQLite) | `peak_memory_mb` (SQLite) | run_id |
| :--- | :--- | ---: | ---: | :--- |
| A, baseline | `4eb5e5f` | 3640.6882333333215 | 24.9609375 | `c03ecb0e61e84e52adca34f4d98f97f4` |
| B, target | `269c8bd` | 157.26216666674495 | 23.28125 | `63bb88ae0cff456aae6326519c9ce400` |

Stage-1 gate (OpenJev): `is_resume_worthy` p=1.000 · `impact_tier` 5/5 · `best_fit_role` backend 0.999.

`metrics generate --format pr-md`:

| Metric | Baseline `4eb5e5f` | Target `269c8bd` | Delta (%) | Status |
| :--- | ---: | ---: | ---: | :--- |
| Mean latency (ms) | 3640.7 | 157.3 | -95.7% | ✅ improved |
| Peak memory RSS (MB) | 25.0 | 23.3 | -6.7% | ✅ improved |
| Throughput (items/s) | n/a | n/a | n/a | ➖ n/a |
| Avg cyclomatic complexity | 3.3 | 3.2 | -3.0% | ✅ improved |
| Speedup | 1.0x | 23.2x | — | 🚀 |

`metrics generate --format xyz --role backend` (`groq/qwen/qwen3.8-27b`; OpenJev faithful 0.998,
natural 0.999; event `03af2bcac0ec43dea61f4169f36ac1f2`):

```text
Accomplished a 95.7% latency reduction as measured by mean latency dropping from 3640.7 ms to 157.3 ms, by batching writes via executemany and caching results with lru_cache.
├─ [X] a 95.7% latency reduction
├─ [Y] mean latency dropping from 3640.7 ms to 157.3 ms
└─ [Z] batching writes via executemany and caching results with lru_cache
```

`metrics generate --format star --role backend` (`groq/openai/gpt-oss-120b`; OpenJev faithful 1.000,
natural 1.000; event `72848fdc717745478cd5559ecc57091a`):

```text
[SITUATION] I executed the benchmark bench_ledger_ingest.py to measure ingestion performance. The baseline run recorded a mean latency of 3640.7 ms and a peak memory usage of 25 MB.
[TASK] My objective was to reduce both latency and memory consumption while preserving the existing ingestion logic.
[ACTION] I applied three targeted optimizations: first, I batched database writes into a single transaction using executemany(); second, I cached repeated cached_profile() results in memory with the @lru_cache decorator; third, I moved the per‑iteration insert_run() and profile_source() calls out of the loop body, eliminating redundant calls inside the loop.
[RESULT] The optimized benchmark showed a mean latency of 157.3 ms, a 95.7% reduction, and peak memory of 23.3 MB, a 6.7% reduction. Overall execution speed increased by a 23.2x multiplier. Cyclomatic complexity fell from 3.3 to 3.2, a 3% reduction, and the code base grew by 14 lines. The measurement was based on a single sample.
```

The last STAR sentence is misleading. "single sample" refers to one ledger row, and that row is the mean
of 3 runs; see VERIFICATION.md.

## Development

```bash
pip install -e .
pytest            # warnings are errors (filterwarnings = ["error"])
```
