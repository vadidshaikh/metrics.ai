# CLAUDE.md — Architectural & Execution Specification for `metrics.ai`

## 1. System Identity & Core Mission
- **Project Name:** `metrics.ai`
- **Package & Binary Name:** PyPI package `metrics-ai`, CLI executable `metrics`
- **Core Purpose:** A local-first developer telemetry, benchmarking, and empirical impact synthesis engine. It captures real baseline-vs-target runtime improvements, memory reductions, and AST structural code changes during development, evaluates them via TypeSafe AI's **Jev (`System One`)** decision model, and synthesizes hallucination-free **Google XYZ** resume bullets, **STAR** interview responses, and **PR telemetry tables** using dynamically discovered models from **Groq**, **Hugging Face Router**, or **Local Ollama**.
- **Coding & Modularity Rules:**
  - Keep every source file focused and modular (target 100–200 lines per file; never exceed 350 lines).
  - Do not use mock data or hardcoded fake percentages in any CLI output or README artifact.
  - For the local web dashboard (`metrics ui`), write raw **Chart.js** and **CanvasJS** instantiation code directly against HTML5 `<canvas>` elements without higher-level frontend wrapper libraries or React/Vue abstractions.

---

## 2. Target Repository Layout & Tech Stack

### 2.1 Directory Blueprint
- `pyproject.toml` — Build configuration (`hatchling`), dependencies (`typer`, `rich`, `pydantic>=2.7`, `gitpython`, `radon`, `psutil`, `httpx`, `tomli`/`tomli-w`, `fastapi`, `uvicorn`, `pytest`, `pytest-benchmark`), and script entry point mapping `metrics` to `metrics_ai.cli:app`.
- `action.yml` — Composite GitHub Action allowing CI workflows to run `metrics` and output Markdown delta tables on Pull Requests.
- `src/metrics_ai/`
  - `cli.py` — Typer CLI command definitions and Rich terminal formatting.
  - `config.py` — `.metrics/config.toml` management, API key storage, and `.gitignore` safety check.
  - `storage.py` — SQLite3 database connection manager, schema initializer, and query layer for `.metrics/ledger.db`.
  - `collector.py` — Git commit inspector (`GitPython`), Python `ast` structural diff extractor, `radon` complexity analyzer, and `psutil` / `pytest-benchmark` runtime profiler.
  - `decision_gate.py` — TypeSafe AI Jev (`jevmodel.net/v1/systemone`) client for pre-generation triage/routing and post-generation semantic verification, plus an offline deterministic fallback gate.
  - `model_router.py` — Live model enumeration, capability scoring, and micro-probe latency testing across Groq, Hugging Face Router, and Local Ollama.
  - `synthesizer.py` — Prompt assembly, Pydantic v2 structured output schemas, numeric hallucination validator, and LLM execution with automatic fallback retry.
  - `dashboard/server.py` & `dashboard/index.html` — FastAPI backend serving ledger JSON and a single-page HTML interface rendered with raw Chart.js and CanvasJS.
- `tests/` — Unit and integration test suite covering every module above.

---

## 3. Data Contracts & Local Persistence (`storage.py`)

Initialize `.metrics/ledger.db` in SQLite3 with three relational tables and strict column types:

| Table Name | Primary Key | Required Columns & Data Contract |
| :--- | :--- | :--- |
| **`commits`** | `sha` (TEXT) | `parent_sha`, `author`, `timestamp` (INT), `message`, `files_modified_json`, `ast_diff_json` (JSON object detailing added/removed functions, loop constructs, batching/pooling calls, and decorators), `cyclomatic_before` (REAL), `cyclomatic_after` (REAL), `loc_delta` (INT). |
| **`telemetry_runs`** | `run_id` (TEXT) | `commit_sha` (FK -> `commits.sha`), `command` (TEXT), `mean_latency_ms` (REAL), `peak_memory_mb` (REAL), `throughput_items_sec` (REAL), `exit_code` (INT), `raw_benchmark_json` (TEXT), `created_at` (INT). |
| **`synthesized_impacts`** | `event_id` (TEXT) | `baseline_sha`, `target_sha`, `role`, `format_type`, `density`, `jev_worth_prob` (REAL), `jev_impact_score` (REAL), `jev_role_prob` (REAL), `jev_faithful_prob` (REAL), `provider_used`, `model_used`, `output_text`, `structured_json`, `ledger_hash` (SHA-256 of input telemetry state + output), `created_at` (INT). |

---

## 4. Telemetry Capture & Pre-Computed Arithmetic (`collector.py`)

1. **Static AST & Complexity Capture:**
   - Compare Python files between `baseline_sha` (default `HEAD~1`) and `target_sha` (`HEAD`).
   - Parse both revisions into Abstract Syntax Trees using Python's built-in `ast` module. Identify structural shifts: removal of nested loops, replacement of sequential calls with bulk/batched operations, addition of caching decorators, async/concurrency shifts, or class/function modularization.
   - Compute average Cyclomatic Complexity before and after using `radon`, plus net Lines of Code (`loc_delta`).
2. **Dynamic Runtime Profiling:**
   - **Subprocess Mode (`metrics wrap "<cmd>"`):** Execute the target command, poll the process tree at 10 ms intervals via `psutil`, and record wall-clock execution latency (ms), peak Resident Set Size (RSS memory in MB), and exit code.
   - **Benchmark Mode (`metrics pytest`):** Execute `pytest` with `--benchmark-json`, parse the output artifact, and record mean execution latency, standard deviation, and operations per second (`throughput_items_sec`).
3. **Mandatory Python Arithmetic Rule:**
   - Never ask Jev or a generative LLM to calculate percentages or ratios.
   - Compute all exact deltas (`latency_delta_pct`, `memory_delta_pct`, `throughput_delta_pct`, `speedup_multiplier_x`, `complexity_delta_pct`) inside `collector.py` rounded to 1 decimal place. Store these exact floats in the state dictionary and populate an `allowed_numbers` set containing every baseline value, target value, and computed delta.

---

## 5. Jev "System One" Decision Gate (`decision_gate.py`)

Integrate TypeSafe AI's discriminative decision model via `POST https://jevmodel.net/v1/systemone` using Bearer token authentication (`JEV_API_KEY`) and default model identifier `typesafe/jev-1.13.0` (configurable in `config.toml`).

### 5.1 Pre-Generation Gate (Single Parallel Request)
Pass the compact telemetry state JSON (containing `ast_structural_diff`, `complexity_delta`, and pre-computed `telemetry_delta`) and evaluate three simultaneous typed questions:
1. **`is_resume_worthy` (`type: "noul"`):**
   - Evaluates whether the commit represents a genuine algorithmic, systems, or code-quality improvement backed by positive telemetry or complexity reduction.
   - Define explicit `criteria` separating `true` (substantive AST optimization with measurable telemetry/complexity gains) from `false` (typo fixes, comments, dead-code deletion without functional replacement, or performance regressions).
   - **Gate Rule:** If `probability < 0.60` (unless `--force` is passed on the CLI), log the run to SQLite and halt LLM synthesis with a clear terminal notice explaining why the commit did not pass the impact threshold.
2. **`impact_tier` (`type: "score"`):**
   - Evaluates engineering significance on a 5-point integer rubric (`"1"` = negligible <5% delta; `"2"` = minor cleanup 5–15%; `"3"` = solid module optimization 15–35%; `"4"` = major bottleneck elimination 35–75%; `"5"` = architectural/algorithmic breakthrough >75% latency reduction or >200% throughput gain).
3. **`best_fit_role` (`type: "choice"`):**
   - Evaluates probability distribution across `["aiml", "backend", "mlops", "tpm"]` with explicit domain `criteria` for each option.
   - **Routing Rule:** When `--role auto` is active, select every role from the returned `distribution` whose probability is `>= 0.25` (or the single top-scoring role if none exceed `0.25`).

### 5.2 Post-Generation Semantic Entailment Gate
After the generative LLM produces a bullet, send a second lightweight `Noul` query (`is_mechanically_faithful`) to Jev containing only `ast_structural_diff`, `files_modified`, and the candidate output text.
- **Verification Rule:** Confirm that every library, database, algorithm, and architectural mechanism claimed in the `[Z]` clause actually appears in the AST diff. If `probability < 0.75`, reject the generation and re-prompt the LLM with stricter instructions.

### 5.3 Offline Fallback Mode
If `JEV_API_KEY` is absent or the endpoint times out, automatically execute a local deterministic heuristic gate in Python (checking threshold percentages and file/AST keywords) so `metrics.ai` works 100% offline with local Ollama.

---

## 6. Dynamic Auto-Model Discovery & Routing (`model_router.py`)

Users must never be forced to look up or hardcode provider model IDs. When the user configures credentials via `metrics auth` (or environment variables `GROQ_API_KEY`, `HF_TOKEN`, or local Ollama), `model_router.py` must automatically discover, rank, probe, and lock the best model.

### 6.1 Live Provider Enumeration Endpoints
1. **Groq:** Query `GET https://api.groq.com/openai/v1/models` with `Authorization: Bearer <GROQ_API_KEY>`. Use `POST https://api.groq.com/openai/v1/chat/completions` for inference.
2. **Hugging Face Router:** Query `GET https://router.huggingface.co/v1/models` with `Authorization: Bearer <HF_TOKEN>` (never use the deprecated `api-inference.huggingface.co` domain). Inspect the returned provider metadata fields (`status == "live"`, `supports_structured_output`, `first_token_latency_ms`, `is_free`) when available. Use `POST https://router.huggingface.co/v1/chat/completions` for inference.
3. **Local Ollama:** Query `GET http://localhost:11434/api/tags` (or `/v1/models`). Use `POST http://localhost:11434/v1/chat/completions` for inference.

### 6.2 Capability Filtering, Ranking & Micro-Probing
1. **Exclusion Filter:** Strip any model ID matching non-text/non-code modalities (`whisper`, `tts`, `guard`, `embed`, `vision`, `audio`, `moderation`, `rerank`).
2. **Capability Scoring Rubric:** Score remaining model IDs by matching against ordered preference tiers optimized for code comprehension and structured JSON adherence:
   - Tier 1 (Score 95–100): `qwen3-coder*`, `qwen2.5-coder*`, `gpt-oss-120b`
   - Tier 2 (Score 88–94): `qwen3-32b`, `qwen3-235b`, `deepseek-v3*`, `deepseek-v4*`, `deepseek-r1*`, `llama-3.3-70b*`, `llama-4-*`
   - Tier 3 (Score 75–87): `kimi-k2*`, `glm-4*`, `glm-5*`, `gpt-oss-20b`, `llama-3.1-8b*`, `gemma-4*`
   - Tier 4 (Score 40): Any other live instruct/chat model.
   - Bonus (+5 points on HF Router): Models explicitly flagging `supports_structured_output == true` and `status == "live"`.
3. **Live Micro-Probe:** Take the top 4 ranked models and send a minimal 15-token JSON completion ping (`{"ok": true}`) with a 4-second timeout.
4. **Persistence & Failover:** Save the highest-scoring model that passes the probe as `active_model` in `.metrics/config.toml`, and store the remaining passing models in `fallback_models`. During `metrics generate`, if `active_model` returns HTTP `429` (rate limit) or `5xx`, seamlessly fail over to the next model in `fallback_models`.

---

## 7. Synthesis Engine & Hallucination Guard (`synthesizer.py` & `cli.py`)

### 7.1 Output Formats (`--format`), Roles (`--role`), and Densities (`--density`)
- **Formats:**
  - `xyz` — Strict Google XYZ structure: *"Accomplished [X] as measured by [Y], by doing [Z]"*. Must output both the cohesive bullet and the visual tree breakdown (`├─ [X]`, `├─ [Y]`, `└─ [Z]`).
  - `star` — Behavioral interview breakdown (`[SITUATION]`, `[TASK]`, `[ACTION]`, `[RESULT]`) grounded in exact file names, AST diffs, and benchmark deltas.
  - `pr-md` — Raw Markdown table comparing Baseline SHA, Target SHA, Delta (%), and Status emojis, followed by a concise AST summary ready to paste into a GitHub Pull Request.
- **Roles:** `auto` (driven by Jev's probability distribution), `aiml`, `backend`, `mlops`, `tpm`, or `all`.
- **Densities:** `ultra-compact` (<115 characters), `standard` (150–210 characters), `detailed` (220–280 characters).

### 7.2 Numeric Hallucination Blocker (Pydantic v2)
- Define a strict Pydantic v2 response model requiring separate fields for `x_accomplishment`, `y_measurement`, `z_mechanism`, `combined_xyz_bullet`, and `cited_numbers`.
- Implement a `@model_validator(mode="after")` that extracts every integer and floating-point number from `combined_xyz_bullet` via regex and verifies that every number exists inside the pre-computed `allowed_numbers` set passed via Pydantic validation context.
- If the LLM rounds a number (e.g., writing `83%` instead of `82.5%`) or invents a metric, raise a `ValidationError`, append the exact error message to the conversation history, and retry up to 3 times.

### 7.3 CLI Commands (`cli.py`)
Implement the following subcommands with clean Rich tables and panels:
- `metrics init` — Initializes `.metrics/`, creates SQLite tables, creates default `config.toml`, adds `.metrics/` to `.gitignore`, and installs an optional fast `.git/hooks/post-commit` script.
- `metrics auth` — Accepts `--groq-key`, `--hf-token`, `--jev-key`, and `--prefer [groq|hf|ollama]`. Immediately runs `model_router` discovery and displays a Rich table of discovered models, scores, probe latencies, and the locked winner.
- `metrics wrap "<cmd>"` — Profiles an arbitrary command and saves telemetry linked to `HEAD`.
- `metrics pytest [ARGS]` — Runs `pytest-benchmark` and ingests telemetry linked to `HEAD`.
- `metrics generate` — Supports `--commit`, `--baseline`, `--format`, `--role`, `--density`, and `--force`. Runs the 3-stage pipeline (Jev Triage -> Auto-Routed LLM Synthesis + Pydantic Numeric Guard -> Jev Faithfulness Check) and persists verified outputs to `ledger.db`.
- `metrics ui [--port 8420]` — Launches the local FastAPI server and serves `dashboard/index.html`.

---

## 8. Mandatory Live Dogfooding & Empirical Verification Gate

**CRITICAL RULE FOR CLAUDE CODE:** You are NOT finished when unit tests pass. Before completing the build, you must dogfood `metrics.ai` on its own repository to produce real, verifiable hardware telemetry and commit those real outputs into the project documentation.

Execute this exact 6-step verification protocol:

1. **Automated Test Suite Gate:**
   - Run `pytest` across all unit tests (`test_collector.py`, `test_decision_gate.py`, `test_model_router.py`, `test_synthesizer.py`). All tests must pass with zero warnings or errors.
2. **Dogfood Commit A (Baseline Commit):**
   - Create a real benchmark script or pytest benchmark inside the repo (`benchmarks/bench_ledger_ingest.py`) that inserts 500 telemetry records and parses a sample Python file AST using an intentionally unoptimized sequential approach (e.g., individual SQLite row commits inside a loop and repeated AST parsing).
   - Commit this baseline state to Git (`git commit -m "perf(baseline): sequential ledger ingestion and AST parsing"`).
   - Run `metrics init` and profile the baseline using `metrics wrap "python benchmarks/bench_ledger_ingest.py"` (or `metrics pytest`). Verify that the baseline metrics exist in `.metrics/ledger.db`.
3. **Dogfood Commit B (Target Optimization Commit):**
   - Refactor the benchmark/storage path to use batched `executemany()` transactions inside a single context manager and cached AST parsing.
   - Commit the optimization (`git commit -m "perf(storage): batch sqlite executemany and optimize ast diff extraction"`).
   - Re-run the exact same profiler command (`metrics wrap "python benchmarks/bench_ledger_ingest.py"`).
4. **Live End-to-End Synthesis Run:**
   - Run `metrics generate --format xyz --role all` and `metrics generate --format pr-md` against the real Commit A -> Commit B delta stored in `.metrics/ledger.db`.
   - Verify that the Pydantic numeric validator passes using the actual millisecond and memory numbers measured on the machine.
5. **Dashboard Endpoint Verification:**
   - Start the FastAPI server in the background, query `GET http://127.0.0.1:8420/api/ledger` via `httpx` or `curl`, verify it returns the real Commit A and Commit B telemetry JSON, and shut down the server cleanly.
6. **Generate Real Proof Artifacts (`VERIFICATION.md` & `README.md`):**
   - Export the real terminal output, real commit SHAs, real benchmark latencies (`ms`), real memory measurements (`MB`), and generated XYZ/PR artifacts into `VERIFICATION.md` and the "Live Dogfooded Example" section of `README.md`. Every number in `README.md` must match the actual SQLite record captured during Step 2 and Step 3.