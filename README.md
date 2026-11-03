# metrics.ai

A local-first developer telemetry, benchmarking and impact synthesis engine. `metrics` profiles real
baseline-vs-target runs of your code and diffs the commits' Python ASTs and cyclomatic complexity. It
computes every delta in Python, then turns them into role-tailored Google XYZ resume bullets, STAR
interview answers or PR Markdown tables. A numeric guard and a prose linter reject any unmeasured number
or clumsy phrasing.

## Install

```bash
pip install metrics-ai          # or, from a checkout: pip install -e .
```

## Quickstart

```bash
metrics init                                   # .metrics/ledger.db, config.toml, .gitignore safety (--hook for post-commit)
metrics auth --groq-key gsk_... --prefer groq  # and/or --hf-token / local Ollama; discovers, probes and locks models
metrics auth --jev-key sk-codiv-...            # optional Tier-1 gate via OpenJev (or --typesafe-key for TypeSafe Jev)
git checkout <baseline>  && metrics wrap "python bench.py"
git checkout <target>    && metrics wrap "python bench.py"
metrics generate --format xyz --role all       # xyz | star | pr-md ; roles auto|aiml|backend|mlops|tpm|all
metrics prune --keep-latest                    # or --all to start from an empty ledger
metrics ui                                     # dashboard at http://127.0.0.1:8420
```

`metrics pytest [ARGS]` ingests `pytest-benchmark` results instead of wall-clock profiling.
`metrics jev-request` prints the exact SystemOne request body for a manual run, and
`metrics generate --jev-response reply.json` uses that reply as the Tier-1 triage.

## How it works

1. **Capture** (`collector.py`, `ast_diff.py`, `profiler.py`).
   * `wrap` runs the command 3 times, sampling the process tree's RSS every 10 ms with `psutil`. It stores
     `per_run_ms`, `per_run_mb` and `benchmark_iterations`.
   * It puts the repo's own `src/` first on the child's `PYTHONPATH`, so a checked-out commit is measured
     with that commit's code.
   * The AST diff lists each distinct structural mechanism separately, for example "batched database writes
     into one transaction via executemany()". `radon` supplies complexity and net LOC.
2. **Arithmetic.** Every percentage, ratio and speedup is computed in Python, rounded to 1 decimal, and
   added to `allowed_numbers`. The state also carries `measured_metrics` and `unmeasured_metrics`; for
   example, `throughput_items_sec` is unmeasured when there's no throughput data.
3. **Stage 1, decision gate** (`decision_gate.py`, `gate_questions.py`). Asks `is_resume_worthy` (noul),
   `impact_tier` (score, 0-indexed, shown as 1–5) and `best_fit_role` (choice), using the SystemOne
   `{model, state, questions}` contract with `{type, instructions, criteria}` questions. Each row records
   which tier answered as `gate_mode`:
   * **`jev_api`:** OpenJev (`api.codiv.ai`, the default) or TypeSafe Jev when a TypeSafe key is set.
     429/502/503/529 responses are retried after 1.5 s and 3.0 s.
   * **`llm_judge`:** the same questions answered by the locked LLM at temperature 0 into a strict
     Pydantic schema. Used when no SystemOne endpoint answers.
   * **`offline_heuristic`:** deterministic rules, **only** with `--offline-no-llm`. Otherwise an
     unavailable gate is an explicit error.

   Synthesis halts below p=0.60 unless `--force` is passed.
4. **Stage 2, synthesis** (`synthesizer.py`, `schemas.py`, `prose.py`).
   * The LLM writes the X, Y and Z parts. Python normalizes them and assembles
     *"Accomplished X as measured by Y, by Z."*
   * **Pydantic validator.** It rejects:
     * unmeasured numbers;
     * "by doing"-style collisions;
     * verb-led X/Y, and X and Y repeating the same phrase;
     * a Z that isn't a gerund, several mechanisms without "and", or a gerund with a bare-identifier
       object ("batching executemany");
     * double negatives and "Nx reduction" (a multiplier is a speedup);
     * unmeasured-metric claims, and "single sample" in STAR;
     * no concrete identifier, or a length outside the density bounds.
   * **Role lenses.**
     * Data-driven requirements: aiml leads Z with the caching change; backend leads Z with the batched
       write; mlops puts the measured multiplier and the memory change in the bullet; tpm puts the latency
       percentage in X and never claims an SLA.
     * A clone check rejects a role whose X/Z is ≥ 80% similar to another role's in the same run.
   * Every failure is fed back to the model, with up to 3 retries.
5. **Stage 3, review.**
   * The gate receives the AST diff, files, telemetry, measured and unmeasured metrics, target role and
     candidate.
   * It asks `is_mechanically_faithful`, `is_metric_accurate` and `is_prose_natural`, and each must reach
     ≥ 0.80. A failure produces a specific critique for the retry.
   * Answers are cached by `sha256(canonical state + candidate)`, so identical text never gets fluctuating
     scores.
6. **Model routing** (`model_router.py`, `llm_client.py`).
   * Discovery: lists live Groq, Hugging Face Router and Ollama models, drops non-text models and `allam`,
     and scores the rest with regex tiers (`qwen3.x-NNb` is Tier 2).
   * Probing and locking: probes the top 3 on the preferred provider plus the top 2 on each other provider,
     then locks primary-provider fallbacks first and secondary providers after them.
   * Failover: a 429 backs off 1.5 s then 3.0 s, then fails over across providers. If every model is
     rate-limited, there is one more pass after a 20 s cooldown.

Environment variables (`GROQ_API_KEY`, `HF_TOKEN`, `JEV_API_KEY`, `TYPESAFE_API_KEY`) override keys
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

The action profiles the PR base and head commits and writes the deterministic `pr-md` table, using
`--offline-no-llm` (CI has no keys), to the job summary and to the `markdown` output.

## Live Dogfooded Example

`metrics.ai` profiled its own benchmark (`benchmarks/bench_ledger_ingest.py`: 500 ledger inserts plus AST
profiling of `collector.py`) on Windows 11 with Python 3.14.7, starting from an empty ledger. Commit A
wrote each row with its own commit and re-parsed the AST for every record. Commit B uses one
`executemany()` transaction and an `lru_cache`'d AST profile.

During this run OpenJev returned HTTP 529 ("overloaded") on every call, so both gates ran as
**`gate_mode = llm_judge`**, via `groq/openai/gpt-oss-120b` at temperature 0. Full terminal output, raw
SQLite rows and the remaining limitations are in [VERIFICATION.md](VERIFICATION.md).

| | Commit | `mean_latency_ms` (SQLite) | `peak_memory_mb` (SQLite) | iterations | run_id |
| :--- | :--- | ---: | ---: | ---: | :--- |
| A, baseline | `4eb5e5f` | 3688.0661000000146 | 24.5546875 | 3 | `cc704c0fcfe244208f6cfc9cb058d861` |
| B, target | `269c8bd` | 161.18456666663405 | 23.85546875 | 3 | `4560c66ad9a946caaa1bb0b7b3d42291` |

`metrics generate --format pr-md`:

| Metric | Baseline `4eb5e5f` | Target `269c8bd` | Delta (%) | Status |
| :--- | ---: | ---: | ---: | :--- |
| Mean latency (ms) | 3688.1 | 161.2 | -95.6% | ✅ improved |
| Peak memory RSS (MB) | 24.6 | 23.9 | -2.8% | ✅ improved |
| Throughput (items/s) | n/a | n/a | n/a | ➖ n/a |
| Avg cyclomatic complexity | 3.3 | 3.2 | -3.0% | ✅ improved |
| Speedup | 1.0x | 22.9x | — | 🚀 |

`metrics generate --format xyz --role all`, all four roles from one command:

```text
[aiml]    Accomplished a 95.6% reduction in ingest latency as measured by mean latency from 3688.1 ms to 161.2 ms, by caching cached_profile results with lru_cache and batching writes via executemany.
[backend] Accomplished a 95.6% reduction in ingest latency as measured by mean latency from 3688.1 ms to 161.2 ms, by batching database writes into one transaction via executemany.
[mlops]   Accomplished a 22.9x speedup in bench_ledger_ingest as measured by mean latency dropping from 3688.1 ms to 161.2 ms with peak RSS down 2.8%, by batching writes via executemany and caching with lru_cache.
[tpm]     Accomplished a 95.6% reduction in ingest latency as measured by mean latency dropping from 3688.1 ms to 161.2 ms, by batching database writes via executemany and caching cached_profile results with lru_cache.
```

`metrics generate --format star --role backend`:

```text
[SITUATION] The benchmark script benchmarks/bench_ledger_ingest.py executed a workload with a baseline mean latency of 3688.1 ms and peak memory of 24.6 MB, measured over 3 iterations.
[TASK] Reduce the mean latency and peak memory of the ledger ingestion process defined in src/metrics_ai/storage.py and src/metrics_ai/cli.py.
[ACTION] First, I replaced per-row database writes with batched database writes into one transaction via executemany() by adding the Ledger.insert_runs method in src/metrics_ai/storage.py. Second, I cached repeated cached_profile() results in memory with @lru_cache in benchmarks/bench_ledger_ingest.py. Third, I moved per-iteration insert_run() and profile_source() calls out of the loop body to eliminate redundant execution.
[RESULT] These changes produced a 95.6% reduction in mean latency to 161.2 ms, a 2.8% reduction in peak memory to 23.9 MB, and a 22.9x speedup, while decreasing cyclomatic complexity from 3.3 to 3.2.
```

## Development

```bash
pip install -e .
pytest            # warnings are errors (filterwarnings = ["error"])
```
