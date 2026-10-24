# metrics.ai

A local-first developer telemetry, benchmarking and impact synthesis engine. `metrics` profiles real
baseline-vs-target runs of your code and diffs the commits' Python ASTs and cyclomatic complexity. It
computes every delta in Python, then turns them into Google XYZ resume bullets, STAR interview answers or
PR Markdown tables. A numeric guard rejects any number that was not actually measured.

## Install

```bash
pip install metrics-ai          # or, from a checkout: pip install -e .
```

## Quickstart

```bash
metrics init                                   # .metrics/ledger.db, config.toml, .gitignore safety (--hook for post-commit)
metrics auth --groq-key gsk_...                # or --hf-token / local Ollama; auto-discovers + locks the best model
git checkout <baseline>  && metrics wrap "python bench.py"
git checkout <target>    && metrics wrap "python bench.py"
metrics generate --format xyz --role auto      # xyz | star | pr-md ; roles auto|aiml|backend|mlops|tpm|all
metrics ui                                     # dashboard at http://127.0.0.1:8420
```

`metrics pytest [ARGS]` ingests `pytest-benchmark` results instead of wall-clock profiling.

## How it works

1. **Capture** (`collector.py`, `ast_diff.py`, `profiler.py`). Diffs the Python files between two commits
   and records functions added or removed, nested loops, calls moved out of loop bodies, batching calls
   (`executemany`, bulk APIs), caching decorators and async/concurrency changes. It also measures `radon`
   complexity and net LOC. `wrap` samples the process tree's RSS every 10 ms with `psutil`.
2. **Arithmetic.** Every percentage, ratio and speedup is computed in Python and rounded to 1 decimal.
   Each one goes into an `allowed_numbers` set.
3. **Triage** (`decision_gate.py`). Asks TypeSafe AI Jev (`is_resume_worthy`, `impact_tier`,
   `best_fit_role`) and halts below p=0.60 unless `--force` is passed. Without `JEV_API_KEY`, or if Jev
   errors, a deterministic offline gate (`offline_gate.py`) is used.
4. **Synthesis** (`synthesizer.py`, `schemas.py`). The LLM output is parsed into a Pydantic v2 model. Its
   validator rejects any number not in `allowed_numbers`, a bullet that breaks the XYZ structure, or one
   outside the density range. The error is fed back to the model for up to 3 retries.
5. **Faithfulness check.** A second gate confirms that the mechanisms named in `[Z]` appear in the AST
   diff. Below 0.75 the model is re-prompted.
6. **Model routing** (`model_router.py`). Lists live Groq, Hugging Face Router and Ollama models, drops
   non-text models, scores the rest by tier and probes the top 4 with a 4 s timeout. If none pass, it
   probes the next 4, skipping any provider that returned 401/402/403. The winner and fallbacks are
   locked into `config.toml`, and a 429 or 5xx fails over to the next model.

Environment variables (`GROQ_API_KEY`, `HF_TOKEN`, `JEV_API_KEY`) override keys stored in
`.metrics/config.toml`, and `metrics auth` warns when that happens. Keys are only written to config after
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
profiling of `collector.py`) on Windows 11 with Python 3.14.7. Commit A wrote each row with its own commit
and re-parsed the AST for every record. Commit B uses one `executemany()` transaction and an `lru_cache`'d
AST profile. Full terminal output and raw records are in [VERIFICATION.md](VERIFICATION.md).

| | Commit | `mean_latency_ms` (SQLite) | `peak_memory_mb` (SQLite) | run_id |
| :--- | :--- | ---: | ---: | :--- |
| A, baseline | `4eb5e5f` | 3435.1028666669663 | 24.32421875 | `e8adbda1c4de45d98d0e62f27f48e102` |
| B, target | `269c8bd` | 160.01733333329562 | 22.4140625 | `48b5087095b243b08f4276ac45691e0e` |

`metrics generate --format pr-md`:

| Metric | Baseline `4eb5e5f` | Target `269c8bd` | Delta (%) | Status |
| :--- | ---: | ---: | ---: | :--- |
| Mean latency (ms) | 3435.1 | 160.0 | -95.3% | ✅ improved |
| Peak memory RSS (MB) | 24.3 | 22.4 | -7.9% | ✅ improved |
| Throughput (items/s) | n/a | n/a | n/a | ➖ n/a |
| Avg cyclomatic complexity | 3.3 | 3.2 | -3.0% | ✅ improved |
| Speedup | 1.0x | 21.5x | — | 🚀 |

`metrics generate --format xyz --role backend` (one of four verified roles; served by
`groq/openai/gpt-oss-20b`, event `fab75badac484b11827e7cc437b294c3`):

```text
Accomplished 95.3% latency reduction as measured by mean latency dropping from 3435.1ms to 160ms, by doing batched insertions with executemany, lru_cache profiling, and hoisting calls out of loops.
├─ [X] 95.3% latency reduction
├─ [Y] mean latency dropping from 3435.1ms to 160ms
└─ [Z] batched insertions with executemany, lru_cache profiling, and hoisting calls out of loops
```

Gate values for this run came from the **offline** gate, since no `JEV_API_KEY` was configured:
`is_resume_worthy` p=0.930, `impact_tier` 5/5, faithfulness 0.97.

## Development

```bash
pip install -e .
pytest            # warnings are errors (filterwarnings = ["error"])
```
