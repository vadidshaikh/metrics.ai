# VERIFICATION — clean live dogfooding of `metrics.ai` on its own repository

This is the proof record for the Section 8 protocol, run on 2026-11-03. Every number comes from
`.metrics/ledger.db` (git-ignored) after a `metrics prune --all` reset. Terminal blocks are the captured
output, pasted verbatim (Pydantic's documentation-link lines are omitted).

| | |
| :--- | :--- |
| Machine | Windows 11 Home 10.0.26200 · Python 3.14.7 |
| Code | `626a20a` (STAR and PR artifacts from `cb47284`; they were re-audited against `626a20a`, see Step 5) |
| Gate (Stage 1 + Stage 3) | Configured Tier 1: **OpenJev** `openjev-latest` @ `https://api.codiv.ai/v1/systemone`. It returned **HTTP 529 "overloaded"** on every call during the run, so every row is **`gate_mode = llm_judge`** (Tier 2: `groq/openai/gpt-oss-120b`, temperature 0). |
| Generator | Groq: `openai/gpt-oss-120b` locked; fallbacks `qwen/qwen3.8-27b`, `openai/gpt-oss-20b` (primary provider) and `ollama/qwen2:0.5b` (secondary provider) |

| Commit | SHA | Message |
| :--- | :--- | :--- |
| **A** (baseline) | `4eb5e5f5b2516ad81628105312e8d02e21a21336` | perf(baseline): sequential ledger ingestion and AST parsing |
| **B** (target) | `269c8bd3a4a197bdc464df05f204cfca3d1de47d` | perf(storage): batch sqlite executemany and optimize ast diff extraction |

## Checklist from the spec

| Requirement | Result |
| :--- | :--- |
| `pytest -q` passes 5 consecutive times with `filterwarnings = ["error"]` | ✅ 5/5 (116 tests). A further 20/20 soak run passed at `cb47284`. |
| No timing flakes | ✅ Profiler tests use a mocked `psutil` and clock. The intermittent CLI failure's root cause was found and fixed: the offline gate treated an empty `mechanisms` list as a code change, so RSS noise could pass a comment-only commit. |
| `qwen/qwen3.8-27b` in Tier 2 (≥ 88) | ✅ score **93** |
| `allam-2-7b` excluded | ✅ absent from candidates and fallbacks |
| Cross-provider fallbacks recorded | ✅ top 3 Groq + top 2 HF (both HTTP 402, no credits, so skipped) + Ollama. `ollama/qwen2:0.5b` is stored as a secondary-provider fallback. |
| `--role all` completes all 4 roles in one command | ✅ aiml, backend, mlops and tpm in a single run (exit 0) |
| Role-differentiated bullets, no "Nx reduction" | ✅ Each passes its data-driven lens and no pair of roles has X/Z ≥ 80% similar. No `Nx reduction` anywhere. |
| STAR references the 3 iterations, never "single sample" | ✅ "measured over 3 iterations" |
| `prune --keep-latest`; API matches SQLite 1:1 | ✅ |

---

## Step 1 — Deterministic test suite

```text
$ pytest -q   # run 1/5
116 passed in 16.55s
$ pytest -q   # run 2/5
116 passed in 16.61s
$ pytest -q   # run 3/5
116 passed in 16.46s
$ pytest -q   # run 4/5
116 passed in 16.56s
$ pytest -q   # run 5/5
116 passed in 16.47s
```

## Step 2 — Clean-slate reset

```text
$ metrics init
✓ Ledger ready at D:\metrics.ai\.metrics\ledger.db
✓ Config at D:\metrics.ai\.metrics\config.toml
✓ .metrics/ already git-ignored
$ metrics prune --all
       Ledger prune (VACUUM complete)
┌─────────────────────┬─────────┬───────────┐
│ Table               │ Deleted │ Remaining │
├─────────────────────┼─────────┼───────────┤
│ commits             │ 2       │ 0         │
│ telemetry_runs      │ 2       │ 0         │
│ synthesized_impacts │ 6       │ 0         │
└─────────────────────┴─────────┴───────────┘
```

## Step 3 — Profile Commit A and Commit B (same command)

The CLI ran from a separate tool venv with the *current* metrics.ai installed. `wrap` puts the checked-out
repo's `src/` first on the child's `PYTHONPATH`, so each benchmark measures its own commit's
`storage.py`. The first line of each block shows which file was imported.

```text
$ git checkout 4eb5e5f  # perf(baseline): sequential ledger ingestion and AST parsing
benchmark imports D:\metrics.ai\src\metrics_ai\storage.py | Ledger.insert_runs present: False
$ metrics wrap "python benchmarks/bench_ledger_ingest.py"
ingested 500 records in 3552.9 ms
ingested 500 records in 3519.1 ms
ingested 500 records in 3573.9 ms
                            Telemetry captured for 4eb5e5f5b2
┌──────────────────────────────────────────┬──────────────┬──────────┬────────────┬──────┐
│ Command                                  │ Mean latency │ Peak RSS │ Throughput │ Exit │
├──────────────────────────────────────────┼──────────────┼──────────┼────────────┼──────┤
│ python benchmarks/bench_ledger_ingest.py │ 3688.1 ms    │ 24.6 MB  │ —          │ 0    │
└──────────────────────────────────────────┴──────────────┴──────────┴────────────┴──────┘
run_id=cc704c0fcfe244208f6cfc9cb058d861
```

```text
$ git checkout 269c8bd  # perf(storage): batch sqlite executemany and optimize ast diff extraction
benchmark imports D:\metrics.ai\src\metrics_ai\storage.py | Ledger.insert_runs present: True
$ metrics wrap "python benchmarks/bench_ledger_ingest.py"
ingested 500 records in 30.1 ms
ingested 500 records in 28.7 ms
ingested 500 records in 29.3 ms
                            Telemetry captured for 269c8bd3a4
┌──────────────────────────────────────────┬──────────────┬──────────┬────────────┬──────┐
│ Command                                  │ Mean latency │ Peak RSS │ Throughput │ Exit │
├──────────────────────────────────────────┼──────────────┼──────────┼────────────┼──────┤
│ python benchmarks/bench_ledger_ingest.py │ 161.2 ms     │ 23.9 MB  │ —          │ 0    │
└──────────────────────────────────────────┴──────────────┴──────────┴────────────┴──────┘
run_id=4560c66ad9a946caaa1bb0b7b3d42291
```

Per-run telemetry stored in `raw_benchmark_json`:

```text
4eb5e5f benchmark_iterations= 3 per_run_ms= [3713.4, 3644.7, 3706.1] per_run_mb= [24.55, 24.54, 24.5]
269c8bd benchmark_iterations= 3 per_run_ms= [173.9, 156.7, 153.0] per_run_mb= [23.73, 23.86, 23.4]
```

## Step 4 — Model discovery (`metrics auth --prefer groq`)

```text
                            Model discovery (124 eligible models)
┌──────┬──────────┬────────────────────────────────────┬───────┬───────────┬─────────────────┐
│ Rank │ Provider │ Model                              │ Score │ Probe     │ Status          │
├──────┼──────────┼────────────────────────────────────┼───────┼───────────┼─────────────────┤
│ 1    │ groq     │ openai/gpt-oss-120b                │ 95    │ 310.7 ms  │ LOCKED          │
│ 2    │ groq     │ qwen/qwen3.8-27b                   │ 93    │ 1055.8 ms │ fallback        │
│ 3    │ groq     │ openai/gpt-oss-20b                 │ 81    │ 1792.8 ms │ fallback        │
│ 4    │ hf       │ openai/gpt-oss-120b                │ 100   │ 249.3 ms  │ failed HTTP 402 │
│ 5    │ hf       │ Qwen/Qwen3.5-397B-A17B             │ 98    │ 354.9 ms  │ failed HTTP 402 │
│ 6    │ hf       │ Qwen/Qwen3-235B-A22B-Instruct-2507 │ 98    │ —         │ not probed      │
│ 7    │ hf       │ Qwen/Qwen3-235B-A22B-Thinking-2507 │ 98    │ —         │ not probed      │
│ 8    │ hf       │ Qwen/Qwen3.5-122B-A10B             │ 98    │ —         │ not probed      │
│ 9    │ hf       │ Qwen/Qwen3.6-35B-A3B               │ 98    │ —         │ not probed      │
│ 10   │ hf       │ Qwen/Qwen3.5-35B-A3B               │ 98    │ —         │ not probed      │
│ 11   │ hf       │ Qwen/Qwen3-32B                     │ 98    │ —         │ not probed      │
│ 12   │ hf       │ Qwen/Qwen3-30B-A3B                 │ 98    │ —         │ not probed      │
│ 113  │ ollama   │ qwen2:0.5b                         │ 40    │ 3962.8 ms │ fallback        │
└──────┴──────────┴────────────────────────────────────┴───────┴───────────┴─────────────────┘
! hf: credentials/billing rejected (HTTP 401/402/403); skipping provider
Locked groq/openai/gpt-oss-120b (+3 fallbacks)
System-1 gate: Tier-1 jev_api via openjev · https://api.codiv.ai/v1/systemone · model openjev-latest
```

## Step 5 — Live synthesis and prose audit

### `metrics generate --commit 269c8bd --format xyz --role all`

The OpenJev 529 note, the Tier-2 judge critique, and every rejected attempt are shown as they happened:
linter, density, role-lens and clone rejections, and empty model replies.

```text
               Delta 4eb5e5f → 269c8bd
┌──────────────────────┬──────────┬────────┬────────┐
│ Metric               │ Baseline │ Target │ Delta  │
├──────────────────────┼──────────┼────────┼────────┤
│ Mean latency (ms)    │ 3688.1   │ 161.2  │ -95.6% │
│ Peak memory (MB)     │ 24.6     │ 23.9   │ -2.8%  │
│ Throughput (items/s) │ —        │ —      │ —      │
│ Avg cyclomatic       │ 3.3      │ 3.2    │ -3.0%  │
│ Speedup              │          │ 22.9x  │        │
└──────────────────────┴──────────┴────────┴────────┘
                 Stage-1 decision gate (gate_mode: llm_judge)
┌─────────────────────────┬───────────────────────────────────────────────────┐
│ Question                │ Answer                                            │
├─────────────────────────┼───────────────────────────────────────────────────┤
│ is_resume_worthy (noul) │ p=0.960  [PASS @ 0.60]                            │
│ impact_tier (score)     │ 5 / 5  (normalized score 5.00)                    │
│ best_fit_role (choice)  │ backend=0.700, tpm=0.200, mlops=0.100, aiml=0.000 │
└─────────────────────────┴───────────────────────────────────────────────────┘
SystemOne API unavailable (HTTPStatusError: Server error '529 <none>' for url 'https://api.codiv.ai/v1/systemone'
For more information check: https://developer.mozi).
Tier-2 LLM judge (groq/openai/gpt-oss-120b, temperature 0).
judge critique: The diff shows a substantive refactor: batching DB writes with executemany, adding an LRU cache, and
hoisting per‑iteration calls out of loops. Telemetry confirms a massive latency reduction (~96%) and a 22.9× speedup,
with a modest memory gain and a slight cyclomatic simplification. While loop count rose slightly, the overall
architectural improvement is clear and resume‑worthy.
 openai/gpt-oss-120b attempt rejected: no JSON object found: line 1 column 1 (char 0)
 openai/gpt-oss-120b attempt rejected: role lens not followed: aiml lens: [Z]'s FIRST clause is the caching change,
naming cached_profile or lru_cache
 qwen/qwen3.8-27b attempt rejected: 1 validation error for XyzResponse
  Value error, prose linter rejected the bullet: assembled bullet is 239 characters; density 'standard' requires
130-220 characters [type=value_error, input_value={'x_accomplishment': 'a 9...: [95.6, 3688.1, 161.2]},
input_type=dict]
┌──────────────────────────────────────────────────── XYZ · aiml ─────────────────────────────────────────────────────┐
│ Accomplished a 95.6% reduction in ingest latency as measured by mean latency from 3688.1 ms to 161.2 ms, by caching │
│ cached_profile results with lru_cache and batching writes via executemany.                                          │
│ ├─ [X] a 95.6% reduction in ingest latency                                                                          │
│ ├─ [Y] mean latency from 3688.1 ms to 161.2 ms                                                                      │
│ └─ [Z] caching cached_profile results with lru_cache and batching writes via executemany                            │
└───── groq/qwen/qwen3.8-27b · llm_judge faithful=0.97 metric=0.98 natural=0.93 · attempts=4 · hash=989ab0383933 ─────┘
{"role": "aiml", "cited_numbers": [95.6, 3688.1, 161.2]}
┌─────────────────────────────────────────────────── XYZ · backend ───────────────────────────────────────────────────┐
│ Accomplished a 95.6% reduction in ingest latency as measured by mean latency from 3688.1 ms to 161.2 ms, by         │
│ batching database writes into one transaction via executemany.                                                      │
│ ├─ [X] a 95.6% reduction in ingest latency                                                                          │
│ ├─ [Y] mean latency from 3688.1 ms to 161.2 ms                                                                      │
│ └─ [Z] batching database writes into one transaction via executemany                                                │
└─── groq/openai/gpt-oss-120b · llm_judge faithful=0.97 metric=0.98 natural=0.95 · attempts=1 · hash=006ffd8f009e ────┘
{"role": "backend", "cited_numbers": [95.6, 3688.1, 161.2]}
 qwen/qwen3.8-27b attempt rejected: 1 validation error for XyzResponse
  Value error, prose linter rejected the bullet: assembled bullet is 228 characters; density 'standard' requires
130-220 characters }, input_type=dict]
┌──────────────────────────────────────────────────── XYZ · mlops ────────────────────────────────────────────────────┐
│ Accomplished a 22.9x speedup in bench_ledger_ingest as measured by mean latency dropping from 3688.1 ms to 161.2 ms │
│ with peak RSS down 2.8%, by batching writes via executemany and caching with lru_cache.                             │
│ ├─ [X] a 22.9x speedup in bench_ledger_ingest                                                                       │
│ ├─ [Y] mean latency dropping from 3688.1 ms to 161.2 ms with peak RSS down 2.8%                                     │
│ └─ [Z] batching writes via executemany and caching with lru_cache                                                   │
└───── groq/qwen/qwen3.8-27b · llm_judge faithful=0.97 metric=0.96 natural=0.94 · attempts=2 · hash=90091e813cd6 ─────┘
{"role": "mlops", "cited_numbers": [22.9, 3688.1, 161.2, 2.8]}
┌───────────────────────────────────────────────────── XYZ · tpm ─────────────────────────────────────────────────────┐
│ Accomplished a 95.6% reduction in ingest latency as measured by mean latency dropping from 3688.1 ms to 161.2 ms,   │
│ by batching database writes via executemany and caching cached_profile results with lru_cache.                      │
│ ├─ [X] a 95.6% reduction in ingest latency                                                                          │
│ ├─ [Y] mean latency dropping from 3688.1 ms to 161.2 ms                                                             │
│ └─ [Z] batching database writes via executemany and caching cached_profile results with lru_cache                   │
└─── groq/openai/gpt-oss-120b · llm_judge faithful=1.00 metric=1.00 natural=0.95 · attempts=1 · hash=0bd53fe7c86f ────┘
{"role": "tpm", "cited_numbers": [95.6, 3688.1, 161.2]}
```

### `metrics generate --commit 269c8bd --format star --role backend`

```text
               Delta 4eb5e5f → 269c8bd
┌──────────────────────┬──────────┬────────┬────────┐
│ Metric               │ Baseline │ Target │ Delta  │
├──────────────────────┼──────────┼────────┼────────┤
│ Mean latency (ms)    │ 3688.1   │ 161.2  │ -95.6% │
│ Peak memory (MB)     │ 24.6     │ 23.9   │ -2.8%  │
│ Throughput (items/s) │ —        │ —      │ —      │
│ Avg cyclomatic       │ 3.3      │ 3.2    │ -3.0%  │
│ Speedup              │          │ 22.9x  │        │
└──────────────────────┴──────────┴────────┴────────┘
                 Stage-1 decision gate (gate_mode: llm_judge)
┌─────────────────────────┬───────────────────────────────────────────────────┐
│ Question                │ Answer                                            │
├─────────────────────────┼───────────────────────────────────────────────────┤
│ is_resume_worthy (noul) │ p=0.950  [PASS @ 0.60]                            │
│ impact_tier (score)     │ 5 / 5  (normalized score 5.00)                    │
│ best_fit_role (choice)  │ backend=0.700, tpm=0.200, mlops=0.100, aiml=0.000 │
└─────────────────────────┴───────────────────────────────────────────────────┘
SystemOne API unavailable (HTTPStatusError: Server error '529 <none>' for url 'https://api.codiv.ai/v1/systemone'
For more information check: https://developer.mozi).
Tier-2 LLM judge (groq/openai/gpt-oss-120b, temperature 0).
judge critique: The diff shows a substantive refactor: batching DB writes with executemany, adding an @lru_cache
decorator, and hoisting per‑iteration calls out of loops. Telemetry confirms a massive latency reduction (~96%) and a
22.9× speedup, with a modest memory drop and negligible cyclomatic change. LOC increased slightly, but the functional
gains outweigh the added lines. Overall, this is a genuine, high‑impact backend optimization suitable for a resume.
┌────────────────────────────────────────────────── STAR · backend ───────────────────────────────────────────────────┐
│ [SITUATION] The benchmark script benchmarks/bench_ledger_ingest.py executed a workload with a baseline mean latency │
│ of 3688.1 ms and peak memory of 24.6 MB, measured over 3 iterations.                                                │
│ [TASK] Reduce the mean latency and peak memory of the ledger ingestion process defined in src/metrics_ai/storage.py │
│ and src/metrics_ai/cli.py.                                                                                          │
│ [ACTION] First, I replaced per-row database writes with batched database writes into one transaction via            │
│ executemany() by adding the Ledger.insert_runs method in src/metrics_ai/storage.py. Second, I cached repeated       │
│ cached_profile() results in memory with @lru_cache in benchmarks/bench_ledger_ingest.py. Third, I moved             │
│ per-iteration insert_run() and profile_source() calls out of the loop body to eliminate redundant execution.        │
│ [RESULT] These changes produced a 95.6% reduction in mean latency to 161.2 ms, a 2.8% reduction in peak memory to   │
│ 23.9 MB, and a 22.9x speedup, while decreasing cyclomatic complexity from 3.3 to 3.2.                               │
└───── groq/qwen/qwen3.8-27b · llm_judge faithful=0.96 metric=0.92 natural=0.95 · attempts=1 · hash=dc9b0e6b2e29 ─────┘
{"role": "backend", "cited_numbers": [3688.1, 24.6, 3.0, 95.6, 161.2, 2.8, 23.9, 22.9, 3.3, 3.2]}
```

### `metrics generate --commit 269c8bd --format pr-md`

```text
               Delta 4eb5e5f → 269c8bd
┌──────────────────────┬──────────┬────────┬────────┐
│ Metric               │ Baseline │ Target │ Delta  │
├──────────────────────┼──────────┼────────┼────────┤
│ Mean latency (ms)    │ 3688.1   │ 161.2  │ -95.6% │
│ Peak memory (MB)     │ 24.6     │ 23.9   │ -2.8%  │
│ Throughput (items/s) │ —        │ —      │ —      │
│ Avg cyclomatic       │ 3.3      │ 3.2    │ -3.0%  │
│ Speedup              │          │ 22.9x  │        │
└──────────────────────┴──────────┴────────┴────────┘
                 Stage-1 decision gate (gate_mode: llm_judge)
┌─────────────────────────┬───────────────────────────────────────────────────┐
│ Question                │ Answer                                            │
├─────────────────────────┼───────────────────────────────────────────────────┤
│ is_resume_worthy (noul) │ p=0.960  [PASS @ 0.60]                            │
│ impact_tier (score)     │ 5 / 5  (normalized score 5.00)                    │
│ best_fit_role (choice)  │ backend=0.700, mlops=0.150, tpm=0.100, aiml=0.050 │
└─────────────────────────┴───────────────────────────────────────────────────┘
SystemOne API unavailable (HTTPStatusError: Server error '529 <none>' for url 'https://api.codiv.ai/v1/systemone'
For more information check: https://developer.mozi).
Tier-2 LLM judge (groq/openai/gpt-oss-120b, temperature 0).
judge critique: The diff shows a substantive refactor: batched DB writes via executemany, LRU caching, and hoisting of
per‑iteration calls, all of which are reflected in a dramatic -95.6% latency reduction and a 22.9× speedup. Cyclomatic
complexity drops marginally, and memory usage improves slightly. The changes are clearly functional and
performance‑oriented rather than cosmetic, making the state highly resume‑worthy. The impact tier is an
order‑of‑magnitude breakthrough. The technical mechanisms align most closely with backend engineering, with secondary
relevance to mlops (benchmarking) and TPM concerns.
PR Markdown · deterministic renderer · hash=f51ad5e3c14c
### metrics.ai telemetry delta: `4eb5e5f` → `269c8bd`

Command: `python benchmarks/bench_ledger_ingest.py`

| Metric | Baseline `4eb5e5f` | Target `269c8bd` | Delta (%) | Status |
| :--- | ---: | ---: | ---: | :--- |
| Mean latency (ms) | 3688.1 | 161.2 | -95.6% | ✅ improved |
| Peak memory RSS (MB) | 24.6 | 23.9 | -2.8% | ✅ improved |
| Throughput (items/s) | n/a | n/a | n/a | ➖ n/a |
| Avg cyclomatic complexity | 3.3 | 3.2 | -3.0% | ✅ improved |
| Speedup | 1.0x | 22.9x | — | 🚀 |

#### AST summary

- Structural signals: `batched_operations_added`, `caching_decorator_added`, `calls_hoisted_out_of_loops`, `modularization`
- Batched calls added: `executemany`
- Caching decorators added: `lru_cache`
- Calls removed from loop bodies: `insert_run`, `profile_source`
- Functions added: `benchmarks/bench_ledger_ingest.py::cached_profile`, `src/metrics_ai/storage.py::Ledger.insert_runs`
- Loop constructs: 16 → 18
- Net LOC delta: +14; files modified: `benchmarks/bench_ledger_ingest.py`, `src/metrics_ai/cli.py`, `src/metrics_ai/storage.py`
```

### Final artifacts (exact `output_text` from SQLite after `prune --keep-latest`)

```text
===== star backend 0f14d24cedea440c8b1017f8366ffcb4
[SITUATION] The benchmark script benchmarks/bench_ledger_ingest.py executed a workload with a baseline mean latency of 3688.1 ms and peak memory of 24.6 MB, measured over 3 iterations.
[TASK] Reduce the mean latency and peak memory of the ledger ingestion process defined in src/metrics_ai/storage.py and src/metrics_ai/cli.py.
[ACTION] First, I replaced per-row database writes with batched database writes into one transaction via executemany() by adding the Ledger.insert_runs method in src/metrics_ai/storage.py. Second, I cached repeated cached_profile() results in memory with @lru_cache in benchmarks/bench_ledger_ingest.py. Third, I moved per-iteration insert_run() and profile_source() calls out of the loop body to eliminate redundant execution.
[RESULT] These changes produced a 95.6% reduction in mean latency to 161.2 ms, a 2.8% reduction in peak memory to 23.9 MB, and a 22.9x speedup, while decreasing cyclomatic complexity from 3.3 to 3.2.
===== pr-md n/a 10a64c08c6704f46a0e750c6abe8e1aa
### metrics.ai telemetry delta: `4eb5e5f` → `269c8bd`

Command: `python benchmarks/bench_ledger_ingest.py`

| Metric | Baseline `4eb5e5f` | Target `269c8bd` | Delta (%) | Status |
| :--- | ---: | ---: | ---: | :--- |
| Mean latency (ms) | 3688.1 | 161.2 | -95.6% | ✅ improved |
| Peak memory RSS (MB) | 24.6 | 23.9 | -2.8% | ✅ improved |
| Throughput (items/s) | n/a | n/a | n/a | ➖ n/a |
| Avg cyclomatic complexity | 3.3 | 3.2 | -3.0% | ✅ improved |
| Speedup | 1.0x | 22.9x | — | 🚀 |

#### AST summary

- Structural signals: `batched_operations_added`, `caching_decorator_added`, `calls_hoisted_out_of_loops`, `modularization`
- Batched calls added: `executemany`
- Caching decorators added: `lru_cache`
- Calls removed from loop bodies: `insert_run`, `profile_source`
- Functions added: `benchmarks/bench_ledger_ingest.py::cached_profile`, `src/metrics_ai/storage.py::Ledger.insert_runs`
- Loop constructs: 16 → 18
- Net LOC delta: +14; files modified: `benchmarks/bench_ledger_ingest.py`, `src/metrics_ai/cli.py`, `src/metrics_ai/storage.py`
===== xyz aiml 784db66acb5d44578788ea2ccb6e73ae
Accomplished a 95.6% reduction in ingest latency as measured by mean latency from 3688.1 ms to 161.2 ms, by caching cached_profile results with lru_cache and batching writes via executemany.
├─ [X] a 95.6% reduction in ingest latency
├─ [Y] mean latency from 3688.1 ms to 161.2 ms
└─ [Z] caching cached_profile results with lru_cache and batching writes via executemany
===== xyz backend 8cf75f3ddd4549d7bb09809b34463a10
Accomplished a 95.6% reduction in ingest latency as measured by mean latency from 3688.1 ms to 161.2 ms, by batching database writes into one transaction via executemany.
├─ [X] a 95.6% reduction in ingest latency
├─ [Y] mean latency from 3688.1 ms to 161.2 ms
└─ [Z] batching database writes into one transaction via executemany
===== xyz mlops 5e14054adfed4f94ac4b6861d86b91e8
Accomplished a 22.9x speedup in bench_ledger_ingest as measured by mean latency dropping from 3688.1 ms to 161.2 ms with peak RSS down 2.8%, by batching writes via executemany and caching with lru_cache.
├─ [X] a 22.9x speedup in bench_ledger_ingest
├─ [Y] mean latency dropping from 3688.1 ms to 161.2 ms with peak RSS down 2.8%
└─ [Z] batching writes via executemany and caching with lru_cache
===== xyz tpm 74fe157e78bb4b72b924ee55e011ec8c
Accomplished a 95.6% reduction in ingest latency as measured by mean latency dropping from 3688.1 ms to 161.2 ms, by batching database writes via executemany and caching cached_profile results with lru_cache.
├─ [X] a 95.6% reduction in ingest latency
├─ [Y] mean latency dropping from 3688.1 ms to 161.2 ms
└─ [Z] batching database writes via executemany and caching cached_profile results with lru_cache
```

### Audit against the final validators

Each stored artifact was re-validated against the code at `626a20a`, which includes the rules added during
this run. The audit checked the numeric guard, the prose linter, multiplier and single-sample rules,
unmeasured metrics, the role lens and pairwise clone similarity.

```text
star backend  PASSES final validator; prose rules ok
xyz  aiml     PASSES final validator; text reproduced=True; lens=ok
xyz  backend  PASSES final validator; text reproduced=True; lens=ok
xyz  mlops    PASSES final validator; text reproduced=True; lens=ok
xyz  tpm      PASSES final validator; text reproduced=True; lens=ok
clone check aiml    : distinct (max X/Z similarity < 80%)
clone check backend : distinct (max X/Z similarity < 80%)
clone check mlops   : distinct (max X/Z similarity < 80%)
clone check tpm     : distinct (max X/Z similarity < 80%)
```

Every number traces back to the SQLite rows below. `collector.py` computed them in Python:

* latency `(161.1846 − 3688.0661) / 3688.0661 × 100 = −95.6%`
* peak RSS `(23.8555 − 24.5547) / 24.5547 × 100 = −2.8%`
* speedup `3688.0661 / 161.1846 = 22.9x`
* complexity `(3.2 − 3.3) / 3.3 × 100 = −3.0%`; `benchmark_iterations = 3` per commit

## Step 6 — Ledger cleanliness and dashboard API

```text
$ metrics prune --keep-latest
       Ledger prune (VACUUM complete)
┌─────────────────────┬─────────┬───────────┐
│ Table               │ Deleted │ Remaining │
├─────────────────────┼─────────┼───────────┤
│ commits             │ 0       │ 2         │
│ telemetry_runs      │ 0       │ 2         │
│ synthesized_impacts │ 13      │ 6         │
└─────────────────────┴─────────┴───────────┘
```

```text
commits:
  {'sha': '4eb5e5f5b2516ad81628105312e8d02e21a21336', 'parent_sha': '4616258eb995ff6f2b33a49acb038d4740fa5b1d', 'cyclomatic_before': None, 'cyclomatic_after': None, 'loc_delta': None}
  {'sha': '269c8bd3a4a197bdc464df05f204cfca3d1de47d', 'parent_sha': '4eb5e5f5b2516ad81628105312e8d02e21a21336', 'cyclomatic_before': 3.3, 'cyclomatic_after': 3.2, 'loc_delta': 14}
telemetry_runs:
  {'run_id': 'cc704c0fcfe244208f6cfc9cb058d861', 'commit_sha': '4eb5e5f5b2516ad81628105312e8d02e21a21336', 'command': 'python benchmarks/bench_ledger_ingest.py', 'mean_latency_ms': 3688.0661000000146, 'peak_memory_mb': 24.5546875, 'exit_code': 0, 'benchmark_iterations': 3, 'per_run_ms': [3713.4, 3644.7, 3706.1], 'per_run_mb': [24.55, 24.54, 24.5]}
  {'run_id': '4560c66ad9a946caaa1bb0b7b3d42291', 'commit_sha': '269c8bd3a4a197bdc464df05f204cfca3d1de47d', 'command': 'python benchmarks/bench_ledger_ingest.py', 'mean_latency_ms': 161.18456666663405, 'peak_memory_mb': 23.85546875, 'exit_code': 0, 'benchmark_iterations': 3, 'per_run_ms': [173.9, 156.7, 153.0], 'per_run_mb': [23.73, 23.86, 23.4]}
synthesized_impacts:
  {'event_id': '0f14d24cedea440c8b1017f8366ffcb4', 'role': 'backend', 'format_type': 'star', 'gate_mode': 'llm_judge', 'jev_worth_prob': 0.95, 'jev_impact_score': 5.0, 'jev_role_prob': 0.7, 'jev_faithful_prob': 0.96, 'natural_prob': 0.95, 'provider_used': 'groq', 'model_used': 'qwen/qwen3.8-27b', 'ledger_hash': 'dc9b0e6b2e292341', 'metric_prob': 0.92}
  {'event_id': '10a64c08c6704f46a0e750c6abe8e1aa', 'role': 'n/a', 'format_type': 'pr-md', 'gate_mode': 'llm_judge', 'jev_worth_prob': 0.96, 'jev_impact_score': 5.0, 'jev_role_prob': None, 'jev_faithful_prob': None, 'natural_prob': None, 'provider_used': 'deterministic', 'model_used': 'renderers.render_pr_md', 'ledger_hash': 'f51ad5e3c14cf196', 'metric_prob': None}
  {'event_id': '784db66acb5d44578788ea2ccb6e73ae', 'role': 'aiml', 'format_type': 'xyz', 'gate_mode': 'llm_judge', 'jev_worth_prob': 0.96, 'jev_impact_score': 5.0, 'jev_role_prob': 0.0, 'jev_faithful_prob': 0.97, 'natural_prob': 0.93, 'provider_used': 'groq', 'model_used': 'qwen/qwen3.8-27b', 'ledger_hash': '989ab03839334a42', 'metric_prob': 0.98}
  {'event_id': '8cf75f3ddd4549d7bb09809b34463a10', 'role': 'backend', 'format_type': 'xyz', 'gate_mode': 'llm_judge', 'jev_worth_prob': 0.96, 'jev_impact_score': 5.0, 'jev_role_prob': 0.7, 'jev_faithful_prob': 0.97, 'natural_prob': 0.95, 'provider_used': 'groq', 'model_used': 'openai/gpt-oss-120b', 'ledger_hash': '006ffd8f009e7686', 'metric_prob': 0.98}
  {'event_id': '5e14054adfed4f94ac4b6861d86b91e8', 'role': 'mlops', 'format_type': 'xyz', 'gate_mode': 'llm_judge', 'jev_worth_prob': 0.96, 'jev_impact_score': 5.0, 'jev_role_prob': 0.1, 'jev_faithful_prob': 0.97, 'natural_prob': 0.94, 'provider_used': 'groq', 'model_used': 'qwen/qwen3.8-27b', 'ledger_hash': '90091e813cd6f334', 'metric_prob': 0.96}
  {'event_id': '74fe157e78bb4b72b924ee55e011ec8c', 'role': 'tpm', 'format_type': 'xyz', 'gate_mode': 'llm_judge', 'jev_worth_prob': 0.96, 'jev_impact_score': 5.0, 'jev_role_prob': 0.2, 'jev_faithful_prob': 1.0, 'natural_prob': 0.95, 'provider_used': 'groq', 'model_used': 'openai/gpt-oss-120b', 'ledger_hash': '0bd53fe7c86fab3c', 'metric_prob': 1.0}
```

```text
Serving dashboard on http://127.0.0.1:8420
GET /api/ledger -> 200
commits              sqlite=2 api=2 identical=True
telemetry_runs       sqlite=2 api=2 identical=True
synthesized_impacts  sqlite=6 api=6 identical=True
  4eb5e5f mean_latency_ms=3688.0661000000146 peak_memory_mb=24.5546875
  269c8bd mean_latency_ms=161.18456666663405 peak_memory_mb=23.85546875
GET / -> Chart.js + CanvasJS + gate_mode column
RESULT: API matches SQLite exactly
server shut down cleanly (port 8420 closed)
```

## Remaining limitations (stated plainly)

* **Tier 1 did not answer during this run.** OpenJev (codiv.ai) returned HTTP 529 "The model is temporarily
  unavailable" on every call, despite 1.5 s / 3.0 s backoff retries. Both gates therefore ran on the
  Tier-2 LLM judge. That judge uses the same Groq model family as the generator, so it is not an
  independent evaluator. The OpenJev path worked live on 2026-10-28; see that run in git history.
* **Roles are distinct but not dramatically different.** aiml, backend and tpm share the same
  [X] ("a 95.6% reduction in ingest latency"); they differ in [Z] (what the bullet leads with) and in what
  each lens requires. All of them describe the same single commit.
* **Role lenses are generic, not the spec's literal text.** Section 7.1 of the spec hardcodes this run's
  numbers ("23.2x", "25.0 MB → 23.3 MB"). The implemented lenses name each role's facet, and the required
  values are computed from the measured state; this run measured 22.9x, not 23.2x.
* **One ledger value is null.** Commit A's `commits` row has `cyclomatic_*`/`loc_delta` = null, because it
  was recorded by `wrap` as HEAD, not diffed. The complexity values used for the A→B delta live on B's row.
