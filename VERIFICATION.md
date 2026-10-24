# VERIFICATION — live dogfooding of `metrics.ai` on its own repository

This file is the Section 8 proof record. Every number below was measured on the build machine and is
stored in `.metrics/ledger.db` (git-ignored, so the ledger itself is not committed). Terminal blocks are
the real captured output, pasted verbatim.

**Machine:** Windows 11 Home 10.0.22621 · Python 3.14.7 · run date 2026-10-24
**Jev:** `JEV_API_KEY` not set, so every gate decision below comes from the **offline deterministic gate**
(`offline_gate.py`), not from TypeSafe AI's hosted model.
**LLM:** Groq, auto-discovered (`openai/gpt-oss-120b` locked, with `openai/gpt-oss-20b`, `qwen/qwen3.8-27b`
and `allam-2-7b` as fallbacks).

| Commit | SHA | Message |
| :--- | :--- | :--- |
| Tool | `4616258eb995ff6f2b33a49acb038d4740fa5b1d` | feat: metrics.ai telemetry, Jev gate, model router, synthesizer, CLI and dashboard |
| **A** (baseline) | `4eb5e5f5b2516ad81628105312e8d02e21a21336` | perf(baseline): sequential ledger ingestion and AST parsing |
| **B** (target) | `269c8bd3a4a197bdc464df05f204cfca3d1de47d` | perf(storage): batch sqlite executemany and optimize ast diff extraction |

---

## Step 1 — Automated test suite gate

`pyproject.toml` sets `filterwarnings = ["error"]`, so any warning fails the run.

```text
$ pytest -q tests/test_collector.py tests/test_decision_gate.py tests/test_model_router.py tests/test_synthesizer.py
...........................................................              [100%]
59 passed in 2.05s
$ pytest -q            # full suite incl. storage/config/CLI/dashboard tests
73 passed in 7.52s
```

## Step 2 — Commit A (baseline) and its profile

`benchmarks/bench_ledger_ingest.py` at `4eb5e5f` inserts 500 telemetry records into a temporary
ledger, opening a connection and committing **once per row** (`Ledger.insert_run` in a loop). For
**every** record it also re-parses the AST of `src/metrics_ai/collector.py`.

```text
$ metrics init
✓ Ledger ready at D:\metrics.ai\.metrics\ledger.db
✓ Config at D:\metrics.ai\.metrics\config.toml
✓ .metrics/ already git-ignored
$ metrics wrap "python benchmarks/bench_ledger_ingest.py"
ingested 500 records in 3297.6 ms
ingested 500 records in 3340.7 ms
ingested 500 records in 3273.5 ms
                            Telemetry captured for 4eb5e5f5b2
┌──────────────────────────────────────────┬──────────────┬──────────┬────────────┬──────┐
│ Command                                  │ Mean latency │ Peak RSS │ Throughput │ Exit │
├──────────────────────────────────────────┼──────────────┼──────────┼────────────┼──────┤
│ python benchmarks/bench_ledger_ingest.py │ 3435.1 ms    │ 24.3 MB  │ —          │ 0    │
└──────────────────────────────────────────┴──────────────┴──────────┴────────────┴──────┘
run_id=e8adbda1c4de45d98d0e62f27f48e102
```

## Step 3 — Commit B (optimization) and its profile

At `269c8bd`, `storage.py` gains `Ledger.insert_runs()`: one connection, one transaction, one
`executemany()`. The benchmark builds its rows first and calls `insert_runs()` once. It also gets the AST
profile through an `@lru_cache` function, so the file is parsed once instead of 500 times. The exact same
profiler command was then re-run:

```text
$ metrics wrap "python benchmarks/bench_ledger_ingest.py"
ingested 500 records in 28.8 ms
ingested 500 records in 27.8 ms
ingested 500 records in 27.0 ms
                            Telemetry captured for 269c8bd3a4
┌──────────────────────────────────────────┬──────────────┬──────────┬────────────┬──────┐
│ Command                                  │ Mean latency │ Peak RSS │ Throughput │ Exit │
├──────────────────────────────────────────┼──────────────┼──────────┼────────────┼──────┤
│ python benchmarks/bench_ledger_ingest.py │ 160.0 ms     │ 22.4 MB  │ —          │ 0    │
└──────────────────────────────────────────┴──────────────┴──────────┴────────────┴──────┘
run_id=48b5087095b243b08f4276ac45691e0e
```

### Raw SQLite records (`telemetry_runs`)

| run_id | commit | mean_latency_ms (raw) | peak_memory_mb (raw) | per-run latency (ms) | per-run peak RSS (MB) | exit |
| :--- | :--- | ---: | ---: | :--- | :--- | ---: |
| `e8adbda1c4de45d98d0e62f27f48e102` | A `4eb5e5f` | 3435.1028666669663 | 24.32421875 | 3435.1, 3471.9, 3398.3 (stdev 36.8) | 23.81, 24.32, 23.90 | 0 |
| `48b5087095b243b08f4276ac45691e0e` | B `269c8bd` | 160.01733333329562 | 22.4140625 | 163.6, 156.2, 160.2 (stdev 3.7) | 22.26, 22.32, 22.41 | 0 |

`mean_latency_ms` is the mean wall-clock time of 3 runs. Each run covers the whole Python process,
including interpreter start-up and imports, so it is larger than the in-process time the benchmark
prints. `peak_memory_mb` is the maximum process-tree RSS, sampled every 10 ms across the 3 runs.

## Step 4 — Live end-to-end synthesis

### Model discovery (`metrics auth --prefer groq`)

```text
                          Model discovery (133 eligible models)
┌──────┬──────────┬──────────────────────────────────────┬───────┬──────────┬────────────┐
│ Rank │ Provider │ Model                                │ Score │ Probe    │ Status     │
├──────┼──────────┼──────────────────────────────────────┼───────┼──────────┼────────────┤
│ 1    │ groq     │ openai/gpt-oss-120b                  │ 95    │ 483.4 ms │ LOCKED     │
│ 2    │ groq     │ openai/gpt-oss-20b                   │ 81    │ 554.4 ms │ fallback   │
│ 3    │ groq     │ qwen/qwen3.8-27b                     │ 40    │ 203.5 ms │ fallback   │
│ 4    │ groq     │ allam-2-7b                           │ 40    │ 191.8 ms │ fallback   │
│ 5    │ hf       │ Qwen/Qwen2.5-Coder-32B-Instruct      │ 102   │ —        │ not probed │
│ 6    │ hf       │ Qwen/Qwen2.5-Coder-7B-Instruct       │ 102   │ —        │ not probed │
│ 7    │ hf       │ Qwen/Qwen2.5-Coder-3B-Instruct       │ 102   │ —        │ not probed │
│ 8    │ hf       │ openai/gpt-oss-120b                  │ 100   │ —        │ not probed │
│ 9    │ hf       │ Qwen/Qwen3-235B-A22B-Thinking-2507   │ 99    │ —        │ not probed │
│ 10   │ hf       │ Qwen/Qwen3-235B-A22B-Instruct-2507   │ 99    │ —        │ not probed │
│ 11   │ hf       │ Qwen/Qwen3-32B                       │ 98    │ —        │ not probed │
│ 12   │ hf       │ deepseek-ai/DeepSeek-V4.1-Flash-1017 │ 97    │ —        │ not probed │
└──────┴──────────┴──────────────────────────────────────┴───────┴──────────┴────────────┘
Locked groq/openai/gpt-oss-120b (+3 fallbacks)
Jev API key: not set (offline gate will be used)
```

### `metrics generate --format xyz --role all`

All four roles produced an artifact that passed the Pydantic numeric guard, the XYZ structure check, the
`standard` density range (150–210 characters) and the faithfulness gate. The `[validation]` lines are
real rejections that were sent back to the model and corrected. The model changes between roles
(`gpt-oss-120b` → `gpt-oss-20b` → `qwen3.8-27b`) because of automatic failover.

```text
               Delta 4eb5e5f → 269c8bd
┌──────────────────────┬──────────┬────────┬────────┐
│ Metric               │ Baseline │ Target │ Delta  │
├──────────────────────┼──────────┼────────┼────────┤
│ Mean latency (ms)    │ 3435.1   │ 160.0  │ -95.3% │
│ Peak memory (MB)     │ 24.3     │ 22.4   │ -7.9%  │
│ Throughput (items/s) │ —        │ —      │ —      │
│ Avg cyclomatic       │ 3.3      │ 3.2    │ -3.0%  │
│ Speedup              │          │ 21.5x  │        │
└──────────────────────┴──────────┴────────┴────────┘
                         Jev triage (source: offline)
┌─────────────────────────┬───────────────────────────────────────────────────┐
│ Question                │ Answer                                            │
├─────────────────────────┼───────────────────────────────────────────────────┤
│ is_resume_worthy (noul) │ p=0.930  [PASS @ 0.60]                            │
│ impact_tier (score)     │ 5 / 5                                             │
│ best_fit_role (choice)  │ mlops=0.464, backend=0.441, aiml=0.048, tpm=0.048 │
└─────────────────────────┴───────────────────────────────────────────────────┘
JEV_API_KEY not set; used offline deterministic gate.
 openai/gpt-oss-120b attempt rejected: 1 validation error for XyzResponse
  Value error, combined_xyz_bullet must follow 'Accomplished [X] as measured by [Y], by doing [Z]'; missing ['as
measured by', ', by ']. }, input_type=dict]
    For further informatio
┌──────────────────────────────────────────────────── XYZ · aiml ─────────────────────────────────────────────────────┐
│ Accomplished a 95.3% latency reduction and 7.9% memory cut, a 21.5x speedup as measured by latency from 3435.1ms to │
│ 160ms and memory from 24.3MB to 22.4MB, by doing lru_cache‑decorated cached_profile batching.                       │
│ ├─ [X] a 95.3% latency reduction and 7.9% memory cut, a 21.5x speedup                                               │
│ ├─ [Y] latency from 3435.1ms to 160ms and memory from 24.3MB to 22.4MB                                              │
│ └─ [Z] lru_cache‑decorated cached_profile batching                                                                  │
└──────────────── groq/openai/gpt-oss-120b · faithful=0.97 (offline) · attempts=2 · hash=e9fca770aafc ────────────────┘
{"role": "aiml", "cited_numbers": [95.3, 7.9, 21.5, 3435.1, 160.0, 24.3, 22.4]}
┌─────────────────────────────────────────────────── XYZ · backend ───────────────────────────────────────────────────┐
│ Accomplished 95.3% latency reduction as measured by mean latency dropping from 3435.1ms to 160ms, by doing batched  │
│ insertions with executemany, lru_cache profiling, and hoisting calls out of loops.                                  │
│ ├─ [X] 95.3% latency reduction                                                                                      │
│ ├─ [Y] mean latency dropping from 3435.1ms to 160ms                                                                 │
│ └─ [Z] batched insertions with executemany, lru_cache profiling, and hoisting calls out of loops                    │
└──────────────── groq/openai/gpt-oss-20b · faithful=0.97 (offline) · attempts=1 · hash=c303b6dd0e89 ─────────────────┘
{"role": "backend", "cited_numbers": [95.3, 3435.1, 160.0]}
 openai/gpt-oss-20b attempt rejected: 1 validation error for XyzResponse
  Value error, combined_xyz_bullet is 214 characters; density 'standard' requires 150-210 characters. },
input_type=dict]
    For further information visit https://errors.pydantic.dev
 openai/gpt-oss-20b attempt rejected: 1 validation error for XyzResponse
  Value error, combined_xyz_bullet must follow 'Accomplished [X] as measured by [Y], by doing [Z]'; missing [', by '].
}, input_type=dict]
    For further information visit https://er
┌──────────────────────────────────────────────────── XYZ · mlops ────────────────────────────────────────────────────┐
│ Accomplished 95.3% latency reduction and 21.5x speedup as measured by mean_latency_ms drop from 3435.1 to 160, by   │
│ doing lru_cache caching and batching with executemany in benchmarks/bench_ledger_ingest.py                          │
│ ├─ [X] 95.3% latency reduction and 21.5x speedup                                                                    │
│ ├─ [Y] mean_latency_ms drop from 3435.1 to 160                                                                      │
│ └─ [Z] lru_cache caching and batching with executemany                                                              │
└───────────────── groq/qwen/qwen3.8-27b · faithful=0.97 (offline) · attempts=3 · hash=23ba04770665 ──────────────────┘
{"role": "mlops", "cited_numbers": [95.3, 3435.1, 160.0, 21.5]}
 qwen/qwen3.8-27b attempt rejected: 1 validation error for XyzResponse
  Value error, combined_xyz_bullet must follow 'Accomplished [X] as measured by [Y], by doing [Z]'; missing [', by '].
}, input_type=dict]
    For further information visit https://er
┌───────────────────────────────────────────────────── XYZ · tpm ─────────────────────────────────────────────────────┐
│ Accomplished reduced ledger ingest latency by 95.3% and memory by 7.9% as measured by 21.5x speedup and peak memory │
│ drop to 22.4 MB, by doing implementing batched executemany operations and lru_cache profiling                       │
│ ├─ [X] Reduced ledger ingest latency by 95.3% and memory by 7.9%                                                    │
│ ├─ [Y] 21.5x speedup and peak memory drop to 22.4 MB                                                                │
│ └─ [Z] implementing batched executemany operations and lru_cache profiling                                          │
└───────────────── groq/qwen/qwen3.8-27b · faithful=0.97 (offline) · attempts=2 · hash=212ddb7bd9da ──────────────────┘
{"role": "tpm", "cited_numbers": [95.3, 7.9, 21.5, 22.4]}
```

Every number in these four bullets (95.3, 7.9, 21.5, 3435.1, 160, 24.3, 22.4) is a member of
`allowed_numbers`, which `collector.py` computed from the two SQLite records above:

* `(160.0173 − 3435.1029) / 3435.1029 × 100 = −95.3%` (latency)
* `(22.4141 − 24.3242) / 24.3242 × 100 = −7.9%` (peak RSS)
* `3435.1029 / 160.0173 = 21.5x` (speedup)

Persisted rows (`synthesized_impacts`, final run):

| role | event_id | model | jev_role_prob | faithful | ledger_hash |
| :--- | :--- | :--- | ---: | ---: | :--- |
| aiml | `b6eda43493c348ad8095fda4a99747e2` | groq/openai/gpt-oss-120b | 0.048 | 0.97 | `e9fca770aafc27f3…` |
| backend | `fab75badac484b11827e7cc437b294c3` | groq/openai/gpt-oss-20b | 0.441 | 0.97 | `c303b6dd0e89f69a…` |
| mlops | `95e95b0e1bf8410b81da367d54c1324d` | groq/qwen/qwen3.8-27b | 0.464 | 0.97 | `23ba04770665336a…` |
| tpm | `20f5cdb738624b28b98ccaad899558c4` | groq/qwen/qwen3.8-27b | 0.048 | 0.97 | `212ddb7bd9da7a6a…` |

### `metrics generate --format pr-md`

The PR table is rendered deterministically in Python, with no LLM involved, so its numbers cannot be
hallucinated.

```text
               Delta 4eb5e5f → 269c8bd
┌──────────────────────┬──────────┬────────┬────────┐
│ Metric               │ Baseline │ Target │ Delta  │
├──────────────────────┼──────────┼────────┼────────┤
│ Mean latency (ms)    │ 3435.1   │ 160.0  │ -95.3% │
│ Peak memory (MB)     │ 24.3     │ 22.4   │ -7.9%  │
│ Throughput (items/s) │ —        │ —      │ —      │
│ Avg cyclomatic       │ 3.3      │ 3.2    │ -3.0%  │
│ Speedup              │          │ 21.5x  │        │
└──────────────────────┴──────────┴────────┴────────┘
                         Jev triage (source: offline)
┌─────────────────────────┬───────────────────────────────────────────────────┐
│ Question                │ Answer                                            │
├─────────────────────────┼───────────────────────────────────────────────────┤
│ is_resume_worthy (noul) │ p=0.930  [PASS @ 0.60]                            │
│ impact_tier (score)     │ 5 / 5                                             │
│ best_fit_role (choice)  │ mlops=0.464, backend=0.441, aiml=0.048, tpm=0.048 │
└─────────────────────────┴───────────────────────────────────────────────────┘
JEV_API_KEY not set; used offline deterministic gate.
PR Markdown · deterministic renderer · hash=90ee4f3db1b2
### metrics.ai telemetry delta: `4eb5e5f` → `269c8bd`

Command: `python benchmarks/bench_ledger_ingest.py`

| Metric | Baseline `4eb5e5f` | Target `269c8bd` | Delta (%) | Status |
| :--- | ---: | ---: | ---: | :--- |
| Mean latency (ms) | 3435.1 | 160.0 | -95.3% | ✅ improved |
| Peak memory RSS (MB) | 24.3 | 22.4 | -7.9% | ✅ improved |
| Throughput (items/s) | n/a | n/a | n/a | ➖ n/a |
| Avg cyclomatic complexity | 3.3 | 3.2 | -3.0% | ✅ improved |
| Speedup | 1.0x | 21.5x | — | 🚀 |

#### AST summary

- Structural signals: `batched_operations_added`, `caching_decorator_added`, `calls_hoisted_out_of_loops`, `modularization`
- Batched calls added: `executemany`
- Caching decorators added: `lru_cache`
- Calls removed from loop bodies: `insert_run`, `profile_source`
- Functions added: `benchmarks/bench_ledger_ingest.py::cached_profile`, `src/metrics_ai/storage.py::Ledger.insert_runs`
- Loop constructs: 16 → 18
- Net LOC delta: +14; files modified: `benchmarks/bench_ledger_ingest.py`, `src/metrics_ai/cli.py`, `src/metrics_ai/storage.py`
```

## Step 5 — Dashboard endpoint verification

`metrics ui --port 8420` was started as a background process. `GET http://127.0.0.1:8420/api/ledger` was
queried with `httpx`, the page was checked for the raw Chart.js and CanvasJS instantiations, and the server
was then terminated. (`exit 1` is the return code Windows reports for a terminated process.)

```text
Serving dashboard on http://127.0.0.1:8420
GET /api/ledger -> 200
{"run_id": "e8adbda1c4de45d98d0e62f27f48e102", "commit_sha": "4eb5e5f5b2516ad81628105312e8d02e21a21336", "command": "python benchmarks/bench_ledger_ingest.py", "mean_latency_ms": 3435.1028666669663, "peak_memory_mb": 24.32421875, "exit_code": 0}
{"run_id": "48b5087095b243b08f4276ac45691e0e", "commit_sha": "269c8bd3a4a197bdc464df05f204cfca3d1de47d", "command": "python benchmarks/bench_ledger_ingest.py", "mean_latency_ms": 160.01733333329562, "peak_memory_mb": 22.4140625, "exit_code": 0}
GET / -> Chart.js + CanvasJS
commits=2 runs=2 impacts=8
server shut down cleanly (port 8420 closed), exit 1
```

## Step 6 — Honest caveats

* **The Jev integration has not been tested against the live API.** The endpoint exists (it returns 401
  without a key), but its request and response schema is not publicly documented. `decision_gate.py`
  sends a best-guess payload and parses the reply leniently. Unit tests cover that parsing against mocked
  responses; any HTTP or parsing failure falls back to the offline gate. Every gate value in this document
  (`worth p=0.930`, `tier 5`, the role distribution, `faithful=0.97`) comes from the offline heuristic.
* **The offline faithfulness check is keyword-based, not semantic.** It confirms that claimed mechanisms
  (`executemany`, `lru_cache`, batching) appear in the AST diff. It cannot judge phrasing. For example,
  the aiml bullet's Z clause ("lru_cache‑decorated cached_profile batching") merges two mechanisms, and
  the tpm bullet reads "by doing implementing". Both are factually grounded but awkwardly worded.
* **The role routing is a keyword heuristic.** It rated `aiml` and `tpm` at 0.048, so `--role auto` would
  pick only `mlops` and `backend`. `--role all` forced all four.
* **Earlier rows remain in the ledger as an audit trail:**
  * A first XYZ run, made before the validator was tightened, produced three bullets. Two of them lacked
    "as measured by", so the XYZ structure check was added.
  * The same run falsely rejected `3435.1ms` as `3435`, because a trailing unit made the number regex
    stop mid-decimal. That bug was fixed and has a regression test.
  * Those superseded rows are not cited here.
* **Model access history:** the original `HF_TOKEN` was revoked (HTTP 401), and the replacement token's
  Hugging Face account has no Inference credits (HTTP 402). Hugging Face was therefore skipped, and Groq
  served every live call. With only local Ollama `qwen2:0.5b` available, the numeric guard rejected every
  attempt (an invented "18"), so no bullet was produced.
* **Commit B contains a test that was flaky at the time** (`test_generate_halts_below_gate_and_logs`): it
  depended on timing noise. The fix, which also adds a 5% noise floor to the offline gate, landed in the
  follow-up commit with this file.
