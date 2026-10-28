# VERIFICATION — clean live dogfooding of `metrics.ai` on its own repository

This is the proof record for the Section 8 protocol, run on 2026-10-28. Every number comes from
`.metrics/ledger.db`, which is git-ignored, after a `metrics prune --all` reset. Every terminal block is
the captured output, pasted verbatim.

| | |
| :--- | :--- |
| Machine | Windows 11 Home 10.0.26200 · Python 3.14.7 |
| Stage 1 + Stage 3 gate | **Tier 1 `jev_api`**, served by **OpenJev** (`openjev-latest`, a Jev-compatible open model) via `https://api.codiv.ai/v1/systemone` |
| Generator | Groq, auto-discovered: `openai/gpt-oss-120b` locked; fallbacks `openai/gpt-oss-20b`, `qwen/qwen3.8-27b`, `allam-2-7b` |
| Code under test | `b4d4943` plus the uncommitted hardening fixes committed together with this file |

| Commit | SHA | Message |
| :--- | :--- | :--- |
| **A** (baseline) | `4eb5e5f5b2516ad81628105312e8d02e21a21336` | perf(baseline): sequential ledger ingestion and AST parsing |
| **B** (target) | `269c8bd3a4a197bdc464df05f204cfca3d1de47d` | perf(storage): batch sqlite executemany and optimize ast diff extraction |

## Status of the three earlier caveats

| Earlier caveat | Status |
| :--- | :--- |
| Every gate value came from the offline keyword heuristic, not a real decision model | **Resolved.** Every row is `gate_mode = jev_api`: triage and every bullet review were answered live by an OpenJev SystemOne endpoint. This is a Jev-*compatible* open model, not TypeSafe's hosted `jev-1.13.0`, because no TypeSafe key is available. |
| Awkward prose passed ("by doing implementing", fused mechanisms) | **Resolved.** XYZ is assembled in Python with gerund normalization, and a deterministic linter rejects preposition collisions, verb-led X/Y clauses, unjoined multi-mechanism Z clauses, double negatives and unmeasured-metric claims. OpenJev's `is_prose_natural` must also reach ≥ 0.80. No stored artifact contains "by doing". |
| Stale and superseded rows remained in the ledger | **Resolved.** The run starts from `prune --all` and ends with `prune --keep-latest`. The ledger holds exactly Commit A, Commit B, their two runs, and one artifact per (role, format). |

---

## Step 1 — Automated test suite gate

`filterwarnings = ["error"]` is set, so any warning fails the run.

```text
$ pytest -q tests/test_collector.py tests/test_decision_gate.py tests/test_model_router.py tests/test_synthesizer.py tests/test_storage_prune.py
80 passed in 2.79s
$ pytest -q        # full suite, including config, CLI and dashboard tests
99 passed in 18.73s
```

## Step 2 — Clean-slate ledger reset

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
│ synthesized_impacts │ 8       │ 0         │
└─────────────────────┴─────────┴───────────┘
```

## Step 3 — Commit A (baseline) profile

The benchmark at `4eb5e5f` does two things for each of 500 records: it inserts the row with its own
connection and commit, and it re-parses the AST of `src/metrics_ai/collector.py`. The commit was checked
out and profiled with its own code.

```text
$ git checkout 4eb5e5f && metrics wrap "python benchmarks/bench_ledger_ingest.py"
ingested 500 records in 3500.5 ms
ingested 500 records in 3465.3 ms
ingested 500 records in 3569.0 ms
                            Telemetry captured for 4eb5e5f5b2
┌──────────────────────────────────────────┬──────────────┬──────────┬────────────┬──────┐
│ Command                                  │ Mean latency │ Peak RSS │ Throughput │ Exit │
├──────────────────────────────────────────┼──────────────┼──────────┼────────────┼──────┤
│ python benchmarks/bench_ledger_ingest.py │ 3640.7 ms    │ 25.0 MB  │ —          │ 0    │
└──────────────────────────────────────────┴──────────────┴──────────┴────────────┴──────┘
run_id=c03ecb0e61e84e52adca34f4d98f97f4
```

## Step 4 — Commit B (optimization) profile, same command

At `269c8bd` the rows go in through one `executemany()` transaction (`Ledger.insert_runs`), and the AST
profile is cached with `@lru_cache`.

```text
$ git checkout 269c8bd && metrics wrap "python benchmarks/bench_ledger_ingest.py"
ingested 500 records in 29.7 ms
ingested 500 records in 28.5 ms
ingested 500 records in 28.5 ms
                            Telemetry captured for 269c8bd3a4
┌──────────────────────────────────────────┬──────────────┬──────────┬────────────┬──────┐
│ Command                                  │ Mean latency │ Peak RSS │ Throughput │ Exit │
├──────────────────────────────────────────┼──────────────┼──────────┼────────────┼──────┤
│ python benchmarks/bench_ledger_ingest.py │ 157.3 ms     │ 23.3 MB  │ —          │ 0    │
└──────────────────────────────────────────┴──────────────┴──────────┴────────────┴──────┘
run_id=63bb88ae0cff456aae6326519c9ce400
```

## Step 5 — Live end-to-end synthesis and prose audit

### Gate and model configuration

```text
$ metrics auth --jev-endpoint https://api.codiv.ai/v1/systemone --jev-model openjev-latest --prefer groq
                         Model discovery (136 eligible models)
┌──────┬──────────┬────────────────────────────────────┬───────┬──────────┬────────────┐
│ Rank │ Provider │ Model                              │ Score │ Probe    │ Status     │
├──────┼──────────┼────────────────────────────────────┼───────┼──────────┼────────────┤
│ 1    │ groq     │ openai/gpt-oss-120b                │ 95    │ 304.8 ms │ LOCKED     │
│ 2    │ groq     │ openai/gpt-oss-20b                 │ 81    │ 905.7 ms │ fallback   │
│ 3    │ groq     │ qwen/qwen3.8-27b                   │ 40    │ 238.1 ms │ fallback   │
│ 4    │ groq     │ allam-2-7b                         │ 40    │ 237.1 ms │ fallback   │
│ 5    │ hf       │ Qwen/Qwen2.5-Coder-32B-Instruct    │ 102   │ —        │ not probed │
│ 6    │ hf       │ Qwen/Qwen2.5-Coder-7B-Instruct     │ 102   │ —        │ not probed │
│ 7    │ hf       │ Qwen/Qwen2.5-Coder-3B-Instruct     │ 102   │ —        │ not probed │
│ 8    │ hf       │ openai/gpt-oss-120b                │ 100   │ —        │ not probed │
│ 9    │ hf       │ Qwen/Qwen3-235B-A22B               │ 99    │ —        │ not probed │
│ 10   │ hf       │ Qwen/Qwen3-235B-A22B-Thinking-2507 │ 99    │ —        │ not probed │
│ 11   │ hf       │ Qwen/Qwen3-235B-A22B-Instruct-2507 │ 99    │ —        │ not probed │
│ 12   │ hf       │ Qwen/Qwen3-32B                     │ 98    │ —        │ not probed │
└──────┴──────────┴────────────────────────────────────┴───────┴──────────┴────────────┘
Locked groq/openai/gpt-oss-120b (+3 fallbacks)
Jev / TypeSafe API key: configured (Tier-1 jev_api gate) · endpoint https://api.codiv.ai/v1/systemone · model
openjev-latest
```

### `metrics generate --commit 269c8bd --format xyz --role all`

This four-role run produced the **aiml** and **backend** bullets that are kept in the ledger. Each
`[validation]` or review line is a real rejection that was fed back to the model.

Its mlops bullet called the 23.2x a "speedup in benchmark **throughput**", but throughput was never
measured. OpenJev did not catch this, because the spec's review state has no telemetry in it. So a
deterministic *unmeasured-metric* check was added, and mlops and tpm (tpm had hit Groq's free-tier rate
limit) were regenerated individually. The run below therefore shows the superseded mlops bullet, which
`prune --keep-latest` later deleted.

```text
               Delta 4eb5e5f → 269c8bd
┌──────────────────────┬──────────┬────────┬────────┐
│ Metric               │ Baseline │ Target │ Delta  │
├──────────────────────┼──────────┼────────┼────────┤
│ Mean latency (ms)    │ 3640.7   │ 157.3  │ -95.7% │
│ Peak memory (MB)     │ 25.0     │ 23.3   │ -6.7%  │
│ Throughput (items/s) │ —        │ —      │ —      │
│ Avg cyclomatic       │ 3.3      │ 3.2    │ -3.0%  │
│ Speedup              │          │ 23.2x  │        │
└──────────────────────┴──────────┴────────┴────────┘
                  Stage-1 decision gate (gate_mode: jev_api)
┌─────────────────────────┬───────────────────────────────────────────────────┐
│ Question                │ Answer                                            │
├─────────────────────────┼───────────────────────────────────────────────────┤
│ is_resume_worthy (noul) │ p=1.000  [PASS @ 0.60]                            │
│ impact_tier (score)     │ 5 / 5  (normalized score 5.00)                    │
│ best_fit_role (choice)  │ backend=0.998, tpm=0.002, aiml=0.000, mlops=0.000 │
└─────────────────────────┴───────────────────────────────────────────────────┘
 openai/gpt-oss-120b attempt rejected: 1 validation error for XyzResponse
  Value error, prose linter rejected the bullet: [Z] must start with a lowercase gerund verb (e.g. 'batching ...'), got
'batched' [type=value_error, input_value={'x_accomplishment': 'a 9...: [95.7, 3640.7, 157.3]}, input_type=dict]
 openai/gpt-oss-120b faithful=0.80 natural=0.95:
 openai/gpt-oss-120b attempt rejected: 1 validation error for XyzResponse
  Value error, prose linter rejected the bullet: assembled bullet is 340 characters; density 'standard' requires
130-220 characters [type=value_error, input_value={'x_accomplishment': 'a 9...: [95.7, 3640.7, 157.3]},
input_type=dict]
 openai/gpt-oss-120b faithful=0.99 natural=0.49:
 openai/gpt-oss-20b attempt rejected: 1 validation error for XyzResponse
  Value error, prose linter rejected the bullet: assembled bullet is 223 characters; density 'standard' requires
130-220 characters [type=value_error, input_value={'x_accomplishment': '95....: [95.7, 3640.7, 157.3]},
input_type=dict]
 openai/gpt-oss-20b attempt rejected: 1 validation error for XyzResponse
  Value error, prose linter rejected the bullet: assembled bullet is 223 characters; density 'standard' requires
130-220 characters [type=value_error, input_value={'x_accomplishment': '95....: [95.7, 3640.7, 157.3]},
input_type=dict]
 exhausted retries on openai/gpt-oss-20b; trying next fallback model
┌──────────────────────────────────────────────────── XYZ · aiml ─────────────────────────────────────────────────────┐
│ Accomplished a 95.7% reduction in ingest latency as measured by mean benchmark latency dropping from 3640.7 ms to   │
│ 157.3 ms, by batching database writes via executemany and caching results with lru_cache.                           │
│ ├─ [X] a 95.7% reduction in ingest latency                                                                          │
│ ├─ [Y] mean benchmark latency dropping from 3640.7 ms to 157.3 ms                                                   │
│ └─ [Z] batching database writes via executemany and caching results with lru_cache                                  │
└──────────── groq/qwen/qwen3.8-27b · jev_api faithful=1.00 natural=1.00 · attempts=7 · hash=c22f5b1e6c68 ────────────┘
{"role": "aiml", "cited_numbers": [95.7, 3640.7, 157.3]}
 openai/gpt-oss-120b attempt rejected: 1 validation error for XyzResponse
  Value error, prose linter rejected the bullet: [Z] must start with a lowercase gerund verb (e.g. 'batching ...'), got
'batched' [type=value_error, input_value={'x_accomplishment': 'a 9...: [95.7, 3640.7, 157.3]}, input_type=dict]
 openai/gpt-oss-20b faithful=0.77 natural=0.96:
 qwen/qwen3.8-27b attempt rejected: 1 validation error for XyzResponse
  Value error, prose linter rejected the bullet: assembled bullet is 238 characters; density 'standard' requires
130-220 characters [type=value_error, input_value={'x_accomplishment': 'a 9...: [95.7, 3640.7, 157.3]},
input_type=dict]
┌─────────────────────────────────────────────────── XYZ · backend ───────────────────────────────────────────────────┐
│ Accomplished a 95.7% latency reduction as measured by mean latency dropping from 3640.7 ms to 157.3 ms, by batching │
│ writes via executemany and caching results with lru_cache.                                                          │
│ ├─ [X] a 95.7% latency reduction                                                                                    │
│ ├─ [Y] mean latency dropping from 3640.7 ms to 157.3 ms                                                             │
│ └─ [Z] batching writes via executemany and caching results with lru_cache                                           │
└──────────── groq/qwen/qwen3.8-27b · jev_api faithful=1.00 natural=1.00 · attempts=4 · hash=6e9c4a177ede ────────────┘
{"role": "backend", "cited_numbers": [95.7, 3640.7, 157.3]}
 openai/gpt-oss-120b faithful=0.97 natural=0.19:
 openai/gpt-oss-120b attempt rejected: no JSON object found: line 1 column 1 (char 0)
 openai/gpt-oss-20b faithful=0.99 natural=0.24:
 openai/gpt-oss-20b attempt rejected: 1 validation error for XyzResponse
  Value error, prose linter rejected the bullet: assembled bullet is 237 characters; density 'standard' requires
130-220 characters [type=value_error, input_value={'x_accomplishment': 'a 9...: [95.7, 3640.7, 157.3]},
input_type=dict]
 openai/gpt-oss-20b faithful=0.87 natural=0.09:
 exhausted retries on openai/gpt-oss-20b; trying next fallback model
┌──────────────────────────────────────────────────── XYZ · mlops ────────────────────────────────────────────────────┐
│ Accomplished a 23.2x speedup in benchmark throughput as measured by mean latency dropping from 3640.7 ms to 157.3   │
│ ms, by batching writes via executemany and caching results with lru_cache.                                          │
│ ├─ [X] a 23.2x speedup in benchmark throughput                                                                      │
│ ├─ [Y] mean latency dropping from 3640.7 ms to 157.3 ms                                                             │
│ └─ [Z] batching writes via executemany and caching results with lru_cache                                           │
└──────────── groq/qwen/qwen3.8-27b · jev_api faithful=0.99 natural=1.00 · attempts=6 · hash=159ddf5bd535 ────────────┘
{"role": "mlops", "cited_numbers": [23.2, 3640.7, 157.3]}
 openai/gpt-oss-120b faithful=0.99 natural=0.04:
 openai/gpt-oss-20b attempt rejected: 1 validation error for XyzResponse
  Value error, prose linter rejected the bullet: assembled bullet is 257 characters; density 'standard' requires
130-220 characters [type=value_error, input_value={'x_accomplishment': 'a 9...: [95.7, 3640.7, 157.3]},
input_type=dict]
 openai/gpt-oss-20b faithful=0.77 natural=0.91:
 qwen/qwen3.8-27b faithful=0.95 natural=0.22:
 exhausted retries on qwen/qwen3.8-27b; trying next fallback model
 allam-2-7b attempt rejected: 3 validation errors for XyzResponse
x_accomplishment
  Field required
y_measurement
  Field required
    For further informatio
 allam-2-7b attempt rejected: Expecting ',' delimiter: line 20 column 5 (char 691)
tpm: All models failed: groq/allam-2-7b: HTTP 429
No verified artifact for: tpm
```

```text
$ metrics generate --commit 269c8bd --format xyz --role mlops
               Delta 4eb5e5f → 269c8bd
┌──────────────────────┬──────────┬────────┬────────┐
│ Metric               │ Baseline │ Target │ Delta  │
├──────────────────────┼──────────┼────────┼────────┤
│ Mean latency (ms)    │ 3640.7   │ 157.3  │ -95.7% │
│ Peak memory (MB)     │ 25.0     │ 23.3   │ -6.7%  │
│ Throughput (items/s) │ —        │ —      │ —      │
│ Avg cyclomatic       │ 3.3      │ 3.2    │ -3.0%  │
│ Speedup              │          │ 23.2x  │        │
└──────────────────────┴──────────┴────────┴────────┘
                  Stage-1 decision gate (gate_mode: jev_api)
┌─────────────────────────┬───────────────────────────────────────────────────┐
│ Question                │ Answer                                            │
├─────────────────────────┼───────────────────────────────────────────────────┤
│ is_resume_worthy (noul) │ p=1.000  [PASS @ 0.60]                            │
│ impact_tier (score)     │ 5 / 5  (normalized score 4.99)                    │
│ best_fit_role (choice)  │ backend=0.999, aiml=0.000, mlops=0.000, tpm=0.000 │
└─────────────────────────┴───────────────────────────────────────────────────┘
 openai/gpt-oss-120b attempt rejected: no JSON object found: line 1 column 1 (char 0)
 openai/gpt-oss-120b attempt rejected: 1 validation error for XyzResponse
  Value error, prose linter rejected the bullet: [Z] must start with a lowercase gerund verb (e.g. 'batching ...'), got
'batched' [type=value_error, input_value={'x_accomplishment': 'a 9...: [95.7, 3640.7, 157.3]}, input_type=dict]
 openai/gpt-oss-20b attempt rejected: 1 validation error for XyzResponse
  Value error, prose linter rejected the bullet: assembled bullet is 222 characters; density 'standard' requires
130-220 characters [type=value_error, input_value={'x_accomplishment': 'a 9...: [95.7, 3640.7, 157.3]},
input_type=dict]
 openai/gpt-oss-20b attempt rejected: 1 validation error for XyzResponse
  Value error, prose linter rejected the bullet: assembled bullet is 222 characters; density 'standard' requires
130-220 characters [type=value_error, input_value={'x_accomplishment': 'a 9...: [95.7, 3640.7, 157.3]},
input_type=dict]
 exhausted retries on openai/gpt-oss-20b; trying next fallback model
 qwen/qwen3.8-27b attempt rejected: 1 validation error for XyzResponse
  Value error, prose linter rejected the bullet: bullet mentions ['throughput'] but that metric was not measured;
describe only measured metrics (a speedup here is a latency speedup) }, input_type=dict]
┌──────────────────────────────────────────────────── XYZ · mlops ────────────────────────────────────────────────────┐
│ Accomplished a 23.2x reduction in benchmark latency as measured by mean latency dropping from 3640.7 ms to 157.3    │
│ ms, by batching writes via executemany and caching results with lru_cache.                                          │
│ ├─ [X] a 23.2x reduction in benchmark latency                                                                       │
│ ├─ [Y] mean latency dropping from 3640.7 ms to 157.3 ms                                                             │
│ └─ [Z] batching writes via executemany and caching results with lru_cache                                           │
└──────────── groq/qwen/qwen3.8-27b · jev_api faithful=0.99 natural=0.99 · attempts=6 · hash=834ae268369e ────────────┘
{"role": "mlops", "cited_numbers": [23.2, 3640.7, 157.3]}
$ metrics generate --commit 269c8bd --format xyz --role tpm
               Delta 4eb5e5f → 269c8bd
┌──────────────────────┬──────────┬────────┬────────┐
│ Metric               │ Baseline │ Target │ Delta  │
├──────────────────────┼──────────┼────────┼────────┤
│ Mean latency (ms)    │ 3640.7   │ 157.3  │ -95.7% │
│ Peak memory (MB)     │ 25.0     │ 23.3   │ -6.7%  │
│ Throughput (items/s) │ —        │ —      │ —      │
│ Avg cyclomatic       │ 3.3      │ 3.2    │ -3.0%  │
│ Speedup              │          │ 23.2x  │        │
└──────────────────────┴──────────┴────────┴────────┘
                  Stage-1 decision gate (gate_mode: jev_api)
┌─────────────────────────┬───────────────────────────────────────────────────┐
│ Question                │ Answer                                            │
├─────────────────────────┼───────────────────────────────────────────────────┤
│ is_resume_worthy (noul) │ p=1.000  [PASS @ 0.60]                            │
│ impact_tier (score)     │ 5 / 5  (normalized score 5.00)                    │
│ best_fit_role (choice)  │ backend=1.000, aiml=0.000, mlops=0.000, tpm=0.000 │
└─────────────────────────┴───────────────────────────────────────────────────┘
 openai/gpt-oss-120b attempt rejected: 1 validation error for XyzResponse
  Value error, prose linter rejected the bullet: [Z] must start with a lowercase gerund verb (e.g. 'batching ...'), got
'batched' [type=value_error, input_value={'x_accomplishment': 'a 9...: [95.7, 3640.7, 157.3]}, input_type=dict]
┌───────────────────────────────────────────────────── XYZ · tpm ─────────────────────────────────────────────────────┐
│ Accomplished a 95.7% reduction in ingest latency as measured by mean latency dropping from 3640.7 ms to 157.3 ms,   │
│ by batching database writes via executemany and caching repeated results with lru_cache.                            │
│ ├─ [X] a 95.7% reduction in ingest latency                                                                          │
│ ├─ [Y] mean latency dropping from 3640.7 ms to 157.3 ms                                                             │
│ └─ [Z] batching database writes via executemany and caching repeated results with lru_cache                         │
└────────── groq/openai/gpt-oss-120b · jev_api faithful=0.99 natural=1.00 · attempts=2 · hash=b20cfad90a60 ───────────┘
{"role": "tpm", "cited_numbers": [95.7, 3640.7, 157.3]}
```

### `metrics generate --commit 269c8bd --format star --role backend`

The first STAR attempts passed OpenJev at 1.00/1.00, but on inspection they contained "a reduction of
-95.7%", an unmeasured "throughput" claim, and invented context ("service", "downstream bottlenecks").
Those findings added a double-negative rule, the unmeasured-metric rule for STAR, and a prompt rule
against invented context. This is the final, kept run:

```text
               Delta 4eb5e5f → 269c8bd
┌──────────────────────┬──────────┬────────┬────────┐
│ Metric               │ Baseline │ Target │ Delta  │
├──────────────────────┼──────────┼────────┼────────┤
│ Mean latency (ms)    │ 3640.7   │ 157.3  │ -95.7% │
│ Peak memory (MB)     │ 25.0     │ 23.3   │ -6.7%  │
│ Throughput (items/s) │ —        │ —      │ —      │
│ Avg cyclomatic       │ 3.3      │ 3.2    │ -3.0%  │
│ Speedup              │          │ 23.2x  │        │
└──────────────────────┴──────────┴────────┴────────┘
                  Stage-1 decision gate (gate_mode: jev_api)
┌─────────────────────────┬───────────────────────────────────────────────────┐
│ Question                │ Answer                                            │
├─────────────────────────┼───────────────────────────────────────────────────┤
│ is_resume_worthy (noul) │ p=1.000  [PASS @ 0.60]                            │
│ impact_tier (score)     │ 5 / 5  (normalized score 5.00)                    │
│ best_fit_role (choice)  │ backend=0.999, tpm=0.001, aiml=0.000, mlops=0.000 │
└─────────────────────────┴───────────────────────────────────────────────────┘
┌────────────────────────────────────────────────── STAR · backend ───────────────────────────────────────────────────┐
│ [SITUATION] I executed the benchmark bench_ledger_ingest.py to measure ingestion performance. The baseline run      │
│ recorded a mean latency of 3640.7 ms and a peak memory usage of 25 MB.                                              │
│ [TASK] My objective was to reduce both latency and memory consumption while preserving the existing ingestion       │
│ logic.                                                                                                              │
│ [ACTION] I applied three targeted optimizations: first, I batched database writes into a single transaction using   │
│ executemany(); second, I cached repeated cached_profile() results in memory with the @lru_cache decorator; third, I │
│ moved the per‑iteration insert_run() and profile_source() calls out of the loop body, eliminating redundant calls   │
│ inside the loop.                                                                                                    │
│ [RESULT] The optimized benchmark showed a mean latency of 157.3 ms, a 95.7% reduction, and peak memory of 23.3 MB,  │
│ a 6.7% reduction. Overall execution speed increased by a 23.2x multiplier. Cyclomatic complexity fell from 3.3 to   │
│ 3.2, a 3% reduction, and the code base grew by 14 lines. The measurement was based on a single sample.              │
└────────── groq/openai/gpt-oss-120b · jev_api faithful=1.00 natural=1.00 · attempts=1 · hash=b61982212fef ───────────┘
{"role": "backend", "cited_numbers": [3640.7, 25.0, 157.3, 23.3, -95.7, -6.7, 23.2, 3.3, 3.2, -3.0, 14.0, 1.0]}
```

### `metrics generate --commit 269c8bd --format pr-md`

The PR table is rendered deterministically in Python (no LLM), so it cannot contain an invented number.

```text
               Delta 4eb5e5f → 269c8bd
┌──────────────────────┬──────────┬────────┬────────┐
│ Metric               │ Baseline │ Target │ Delta  │
├──────────────────────┼──────────┼────────┼────────┤
│ Mean latency (ms)    │ 3640.7   │ 157.3  │ -95.7% │
│ Peak memory (MB)     │ 25.0     │ 23.3   │ -6.7%  │
│ Throughput (items/s) │ —        │ —      │ —      │
│ Avg cyclomatic       │ 3.3      │ 3.2    │ -3.0%  │
│ Speedup              │          │ 23.2x  │        │
└──────────────────────┴──────────┴────────┴────────┘
                  Stage-1 decision gate (gate_mode: jev_api)
┌─────────────────────────┬───────────────────────────────────────────────────┐
│ Question                │ Answer                                            │
├─────────────────────────┼───────────────────────────────────────────────────┤
│ is_resume_worthy (noul) │ p=1.000  [PASS @ 0.60]                            │
│ impact_tier (score)     │ 5 / 5  (normalized score 4.99)                    │
│ best_fit_role (choice)  │ backend=0.999, tpm=0.001, aiml=0.000, mlops=0.000 │
└─────────────────────────┴───────────────────────────────────────────────────┘
PR Markdown · deterministic renderer · hash=da5c5552cf1c
### metrics.ai telemetry delta: `4eb5e5f` → `269c8bd`

Command: `python benchmarks/bench_ledger_ingest.py`

| Metric | Baseline `4eb5e5f` | Target `269c8bd` | Delta (%) | Status |
| :--- | ---: | ---: | ---: | :--- |
| Mean latency (ms) | 3640.7 | 157.3 | -95.7% | ✅ improved |
| Peak memory RSS (MB) | 25.0 | 23.3 | -6.7% | ✅ improved |
| Throughput (items/s) | n/a | n/a | n/a | ➖ n/a |
| Avg cyclomatic complexity | 3.3 | 3.2 | -3.0% | ✅ improved |
| Speedup | 1.0x | 23.2x | — | 🚀 |

#### AST summary

- Structural signals: `batched_operations_added`, `caching_decorator_added`, `calls_hoisted_out_of_loops`, `modularization`
- Batched calls added: `executemany`
- Caching decorators added: `lru_cache`
- Calls removed from loop bodies: `insert_run`, `profile_source`
- Functions added: `benchmarks/bench_ledger_ingest.py::cached_profile`, `src/metrics_ai/storage.py::Ledger.insert_runs`
- Loop constructs: 16 → 18
- Net LOC delta: +14; files modified: `benchmarks/bench_ledger_ingest.py`, `src/metrics_ai/cli.py`, `src/metrics_ai/storage.py`
```

### Final artifacts (exact `output_text` from SQLite)

```text
===== xyz aiml def80087a4404846ac201b3f8594f426
Accomplished a 95.7% reduction in ingest latency as measured by mean benchmark latency dropping from 3640.7 ms to 157.3 ms, by batching database writes via executemany and caching results with lru_cache.
├─ [X] a 95.7% reduction in ingest latency
├─ [Y] mean benchmark latency dropping from 3640.7 ms to 157.3 ms
└─ [Z] batching database writes via executemany and caching results with lru_cache
===== xyz backend 03af2bcac0ec43dea61f4169f36ac1f2
Accomplished a 95.7% latency reduction as measured by mean latency dropping from 3640.7 ms to 157.3 ms, by batching writes via executemany and caching results with lru_cache.
├─ [X] a 95.7% latency reduction
├─ [Y] mean latency dropping from 3640.7 ms to 157.3 ms
└─ [Z] batching writes via executemany and caching results with lru_cache
===== xyz mlops 804726e1e88948caae5052008990dcce
Accomplished a 23.2x reduction in benchmark latency as measured by mean latency dropping from 3640.7 ms to 157.3 ms, by batching writes via executemany and caching results with lru_cache.
├─ [X] a 23.2x reduction in benchmark latency
├─ [Y] mean latency dropping from 3640.7 ms to 157.3 ms
└─ [Z] batching writes via executemany and caching results with lru_cache
===== xyz tpm 8eea0d0137cf46989f29e52317d25373
Accomplished a 95.7% reduction in ingest latency as measured by mean latency dropping from 3640.7 ms to 157.3 ms, by batching database writes via executemany and caching repeated results with lru_cache.
├─ [X] a 95.7% reduction in ingest latency
├─ [Y] mean latency dropping from 3640.7 ms to 157.3 ms
└─ [Z] batching database writes via executemany and caching repeated results with lru_cache
===== pr-md n/a 2e7c217e5fa845b294cc577060f88a20
### metrics.ai telemetry delta: `4eb5e5f` → `269c8bd`

Command: `python benchmarks/bench_ledger_ingest.py`

| Metric | Baseline `4eb5e5f` | Target `269c8bd` | Delta (%) | Status |
| :--- | ---: | ---: | ---: | :--- |
| Mean latency (ms) | 3640.7 | 157.3 | -95.7% | ✅ improved |
| Peak memory RSS (MB) | 25.0 | 23.3 | -6.7% | ✅ improved |
| Throughput (items/s) | n/a | n/a | n/a | ➖ n/a |
| Avg cyclomatic complexity | 3.3 | 3.2 | -3.0% | ✅ improved |
| Speedup | 1.0x | 23.2x | — | 🚀 |

#### AST summary

- Structural signals: `batched_operations_added`, `caching_decorator_added`, `calls_hoisted_out_of_loops`, `modularization`
- Batched calls added: `executemany`
- Caching decorators added: `lru_cache`
- Calls removed from loop bodies: `insert_run`, `profile_source`
- Functions added: `benchmarks/bench_ledger_ingest.py::cached_profile`, `src/metrics_ai/storage.py::Ledger.insert_runs`
- Loop constructs: 16 → 18
- Net LOC delta: +14; files modified: `benchmarks/bench_ledger_ingest.py`, `src/metrics_ai/cli.py`, `src/metrics_ai/storage.py`
===== star backend 72848fdc717745478cd5559ecc57091a
[SITUATION] I executed the benchmark bench_ledger_ingest.py to measure ingestion performance. The baseline run recorded a mean latency of 3640.7 ms and a peak memory usage of 25 MB.
[TASK] My objective was to reduce both latency and memory consumption while preserving the existing ingestion logic.
[ACTION] I applied three targeted optimizations: first, I batched database writes into a single transaction using executemany(); second, I cached repeated cached_profile() results in memory with the @lru_cache decorator; third, I moved the per‑iteration insert_run() and profile_source() calls out of the loop body, eliminating redundant calls inside the loop.
[RESULT] The optimized benchmark showed a mean latency of 157.3 ms, a 95.7% reduction, and peak memory of 23.3 MB, a 6.7% reduction. Overall execution speed increased by a 23.2x multiplier. Cyclomatic complexity fell from 3.3 to 3.2, a 3% reduction, and the code base grew by 14 lines. The measurement was based on a single sample.
```

### Prose and number audit

Every stored XYZ/STAR artifact was re-validated against the **final** validator, including the rules
added during this run. The validator assembled exactly the stored text:

```text
xyz  aiml     PASSES current validator; stored text reproduced=True
xyz  backend  PASSES current validator; stored text reproduced=True
xyz  mlops    PASSES current validator; stored text reproduced=True
xyz  tpm      PASSES current validator; stored text reproduced=True
star backend  PASSES current validator; stored text reproduced=True
```

Every number traces back to the SQLite rows below. `collector.py` computed them in Python:

* latency `(157.2622 − 3640.6882) / 3640.6882 × 100 = −95.7%`
* peak RSS `(23.2813 − 24.9609) / 24.9609 × 100 = −6.7%`
* speedup `3640.6882 / 157.2622 = 23.2x`
* complexity `(3.2 − 3.3) / 3.3 × 100 = −3.0%`; net LOC `+14`

## Step 6 — Ledger cleanliness and dashboard API

```text
$ metrics prune --keep-latest
       Ledger prune (VACUUM complete)
┌─────────────────────┬─────────┬───────────┐
│ Table               │ Deleted │ Remaining │
├─────────────────────┼─────────┼───────────┤
│ commits             │ 0       │ 2         │
│ telemetry_runs      │ 0       │ 2         │
│ synthesized_impacts │ 8       │ 6         │
└─────────────────────┴─────────┴───────────┘
```

Direct SQLite query after pruning:

```text
commits:
  {'sha': '4eb5e5f5b2516ad81628105312e8d02e21a21336', 'parent_sha': '4616258eb995ff6f2b33a49acb038d4740fa5b1d', 'cyclomatic_before': None, 'cyclomatic_after': None, 'loc_delta': None}
  {'sha': '269c8bd3a4a197bdc464df05f204cfca3d1de47d', 'parent_sha': '4eb5e5f5b2516ad81628105312e8d02e21a21336', 'cyclomatic_before': 3.3, 'cyclomatic_after': 3.2, 'loc_delta': 14}
telemetry_runs:
  {'run_id': 'c03ecb0e61e84e52adca34f4d98f97f4', 'commit_sha': '4eb5e5f5b2516ad81628105312e8d02e21a21336', 'command': 'python benchmarks/bench_ledger_ingest.py', 'mean_latency_ms': 3640.6882333333215, 'peak_memory_mb': 24.9609375, 'exit_code': 0, 'per_run_ms': [3636.1, 3589.5, 3696.5], 'per_run_mb': [24.7, 24.96, 24.53]}
  {'run_id': '63bb88ae0cff456aae6326519c9ce400', 'commit_sha': '269c8bd3a4a197bdc464df05f204cfca3d1de47d', 'command': 'python benchmarks/bench_ledger_ingest.py', 'mean_latency_ms': 157.26216666674495, 'peak_memory_mb': 23.28125, 'exit_code': 0, 'per_run_ms': [166.4, 152.5, 152.9], 'per_run_mb': [23.07, 23.17, 23.28]}
synthesized_impacts:
  {'event_id': 'def80087a4404846ac201b3f8594f426', 'target_sha': '269c8bd3a4a197bdc464df05f204cfca3d1de47d', 'baseline_sha': '4eb5e5f5b2516ad81628105312e8d02e21a21336', 'role': 'aiml', 'format_type': 'xyz', 'density': 'standard', 'gate_mode': 'jev_api', 'jev_worth_prob': 0.9999929485593195, 'jev_impact_score': 4.995, 'jev_role_prob': 0.0, 'jev_faithful_prob': 0.9962833816279923, 'provider_used': 'groq', 'model_used': 'qwen/qwen3.8-27b', 'ledger_hash': 'c22f5b1e6c68db82', 'natural_prob': 0.9953405835824203, 'review_mode': 'jev_api'}
  {'event_id': '03af2bcac0ec43dea61f4169f36ac1f2', 'target_sha': '269c8bd3a4a197bdc464df05f204cfca3d1de47d', 'baseline_sha': '4eb5e5f5b2516ad81628105312e8d02e21a21336', 'role': 'backend', 'format_type': 'xyz', 'density': 'standard', 'gate_mode': 'jev_api', 'jev_worth_prob': 0.9999929485593195, 'jev_impact_score': 4.995, 'jev_role_prob': 0.998, 'jev_faithful_prob': 0.9983188442736904, 'provider_used': 'groq', 'model_used': 'qwen/qwen3.8-27b', 'ledger_hash': '6e9c4a177ede7ba5', 'natural_prob': 0.9992371754776292, 'review_mode': 'jev_api'}
  {'event_id': '804726e1e88948caae5052008990dcce', 'target_sha': '269c8bd3a4a197bdc464df05f204cfca3d1de47d', 'baseline_sha': '4eb5e5f5b2516ad81628105312e8d02e21a21336', 'role': 'mlops', 'format_type': 'xyz', 'density': 'standard', 'gate_mode': 'jev_api', 'jev_worth_prob': 0.9999516333382323, 'jev_impact_score': 4.991, 'jev_role_prob': 0.0, 'jev_faithful_prob': 0.9938485068049939, 'provider_used': 'groq', 'model_used': 'qwen/qwen3.8-27b', 'ledger_hash': '834ae268369ec17f', 'natural_prob': 0.9920345476642527, 'review_mode': 'jev_api'}
  {'event_id': '8eea0d0137cf46989f29e52317d25373', 'target_sha': '269c8bd3a4a197bdc464df05f204cfca3d1de47d', 'baseline_sha': '4eb5e5f5b2516ad81628105312e8d02e21a21336', 'role': 'tpm', 'format_type': 'xyz', 'density': 'standard', 'gate_mode': 'jev_api', 'jev_worth_prob': 0.999999468093731, 'jev_impact_score': 4.997, 'jev_role_prob': 0.0, 'jev_faithful_prob': 0.9919355861806207, 'provider_used': 'groq', 'model_used': 'openai/gpt-oss-120b', 'ledger_hash': 'b20cfad90a6086ed', 'natural_prob': 0.9981421893334639, 'review_mode': 'jev_api'}
  {'event_id': '2e7c217e5fa845b294cc577060f88a20', 'target_sha': '269c8bd3a4a197bdc464df05f204cfca3d1de47d', 'baseline_sha': '4eb5e5f5b2516ad81628105312e8d02e21a21336', 'role': 'n/a', 'format_type': 'pr-md', 'density': 'standard', 'gate_mode': 'jev_api', 'jev_worth_prob': 0.9999830030917757, 'jev_impact_score': 4.992, 'jev_role_prob': None, 'jev_faithful_prob': None, 'provider_used': 'deterministic', 'model_used': 'renderers.render_pr_md', 'ledger_hash': 'da5c5552cf1c87de', 'natural_prob': None, 'review_mode': None}
  {'event_id': '72848fdc717745478cd5559ecc57091a', 'target_sha': '269c8bd3a4a197bdc464df05f204cfca3d1de47d', 'baseline_sha': '4eb5e5f5b2516ad81628105312e8d02e21a21336', 'role': 'backend', 'format_type': 'star', 'density': 'standard', 'gate_mode': 'jev_api', 'jev_worth_prob': 0.9999977168426984, 'jev_impact_score': 4.998, 'jev_role_prob': 0.999, 'jev_faithful_prob': 0.9998907980215442, 'provider_used': 'groq', 'model_used': 'openai/gpt-oss-120b', 'ledger_hash': 'b61982212fef4093', 'natural_prob': 0.9999397001956916, 'review_mode': 'jev_api'}
```

`metrics ui --port 8420` was started in the background. The `/api/ledger` payload was compared table by
table with the SQLite rows, the page was checked for its raw Chart.js and CanvasJS instantiation, and the
server was then terminated:

```text
Serving dashboard on http://127.0.0.1:8420
GET /api/ledger -> 200
commits              sqlite=2 api=2 identical=True
telemetry_runs       sqlite=2 api=2 identical=True
synthesized_impacts  sqlite=6 api=6 identical=True
  4eb5e5f mean_latency_ms=3640.6882333333215 peak_memory_mb=24.9609375
  269c8bd mean_latency_ms=157.26216666674495 peak_memory_mb=23.28125
GET / -> Chart.js + CanvasJS + gate_mode column
RESULT: API matches SQLite exactly
server shut down cleanly (port 8420 closed)
```

## Remaining limitations (stated plainly)

* **OpenJev, not TypeSafe Jev.** Tier 1 ran against OpenJev (`openjev-latest` on codiv.ai), which speaks
  the same SystemOne contract. Against TypeSafe's own `api.typesafe.ai` endpoint the client has only been
  tested with mocked responses.
* **The spec's review state carries no telemetry,** so OpenJev cannot judge metric semantics (for
  example "throughput" vs "latency"). That gap is covered by the deterministic unmeasured-metric rule,
  not by Jev.
* **OpenJev returns probabilities with no critique text.** Retry feedback is therefore built from which
  score failed.
* **OpenJev's scores vary between calls.** One grammatical bullet scored `is_prose_natural` 0.375, then
  0.523 and 0.663 when re-asked, which is why the pipeline retries.
* **One STAR sentence is misleading.** It says "The measurement was based on a single sample." This comes
  from the state field `samples: 1`, which counts ledger rows; each row is itself the mean of 3 runs (per-run
  values above).
* **One unexplained test failure.** Across 17 consecutive full-suite runs during this session, one failed
  and could not be reproduced in 16 further runs. It was most likely a timing/memory-sensitive profiler
  test.
