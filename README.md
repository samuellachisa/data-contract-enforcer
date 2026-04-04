# TRP Week 7 — Data Contract Enforcer

Auto-generates Bitol-style contracts from Weeks 1–5 JSONL outputs, validates them, attributes failures using the Week 4 lineage graph (+ `git` log/blame), runs AI-specific checks (embeddings, prompt inputs, LLM JSON, LangSmith traces), diffs schema snapshots, and produces `enforcer_report/report_data.json` plus a PDF.

## Prerequisites

```powershell
cd data-contract-enforcer
pip install -r requirements.txt
```

Optional environment variables:

| Variable | Effect |
|----------|--------|
| `USE_YDATA=1` | Enable ydata-profiling in `contracts/generator.py` |
| `ANTHROPIC_API_KEY` | Real LLM annotations in generator (Anthropic) |
| `OPENAI_API_KEY` | Real LLM annotations (OpenAI) or `text-embedding-3-small` drift |
| `CONTRACT_LLM_OFF=1` | Force stub LLM annotations (offline) |
| `EMBEDDING_OFF=1` | Skip OpenAI embeddings; use hashing fallback |

## End-to-end (fresh clone)

**1. Sample data (55+ rows Week 3 / Week 5, traces, lineage with `table::doc:{doc_id}` nodes)**

```powershell
python scripts/seed_outputs.py
```

**2. Generate contracts + schema snapshots + dbt YAML**

```powershell
python contracts/generator.py --all --output generated_contracts
```

Evaluators may run: `python contracts/generator.py --source outputs/week3/extractions.jsonl --output generated_contracts/`

Generated dbt schema YAML mirrors the Bitol contracts with **multiple models**, **`relationships`** (FK-style), **`accepted_values`** (enums), and **singular tests** under `generated_contracts/dbt_tests/singular/` (confidence 0–1, temporal order, payload.bytes).

**3. ValidationRunner — Week 3 / Week 5 (required evaluator paths)**

`--mode` controls exit codes after the report is written (default **AUDIT** = always exit 0):

- **AUDIT** — run all checks, never fail the process.
- **WARN** — exit **1** if any check has `status=FAIL` and `severity=CRITICAL`.
- **ENFORCE** — exit **1** if any check has `status=FAIL` and `severity` is **CRITICAL** or **HIGH**.

```powershell
python contracts/runner.py --contract generated_contracts/week3_extractions.yaml --data outputs/week3/extractions.jsonl --output validation_reports/week3_latest.json --mode AUDIT
python contracts/runner.py --contract generated_contracts/week5_events.yaml --data outputs/week5/events.jsonl --output validation_reports/week5_latest.json --mode AUDIT
```

**Other single-file contracts**

```powershell
python contracts/runner.py --contract generated_contracts/week1_intent_records.yaml --data outputs/week1/intent_records.jsonl --output validation_reports/week1_latest.json
python contracts/runner.py --contract generated_contracts/week2_verdicts.yaml --data outputs/week2/verdicts.jsonl --output validation_reports/week2_latest.json
python contracts/runner.py --contract generated_contracts/week4_lineage.yaml --data outputs/week4/lineage_snapshots.jsonl --output validation_reports/week4_latest.json
python contracts/runner.py --contract generated_contracts/langsmith_traces.yaml --data outputs/traces/runs.jsonl --output validation_reports/langsmith_latest.json
```

**Cross-system contracts (Week 1 → Week 2, Week 3 → Week 4)**

```powershell
python contracts/runner.py --cross-dependencies --output validation_reports/cross_latest.json
```

**4. AI extensions** (`validation_reports/ai_metrics.json`; may append `violation_log/violations.jsonl`)

```powershell
python contracts/ai_extensions.py
```

**5. Violation attributor** (`violation_log/violations_with_blame.jsonl`)

Blast radius: **`contract_registry/subscriptions.yaml`** is queried first (subscriber list + matched `breaking_fields`). Week 4 lineage forward reachability adds `lineage_enrichment` and `contamination_depth`.

```powershell
python contracts/attributor.py --input violation_log/violations.jsonl --output violation_log/violations_with_blame.jsonl
```

**5b. Deduplicate `violations.jsonl` (after repeated `ai_extensions` runs)**

```powershell
python scripts/refresh_submission_artifacts.py
```

**6. Schema evolution**

```powershell
python contracts/schema_analyzer.py --contract-id week3-document-refinery-extractions --since "7 days ago" --output validation_reports/schema_evolution_week3.json
```

Also writes `validation_reports/migration_impact_<contract_id>_<timestamp>.json`.

**7. Enforcer report**

```powershell
python contracts/report_generator.py
```

Outputs: `enforcer_report/report_data.json`, `enforcer_report/report_<YYYYMMDD>.pdf`.

## Contract registry (Tier 1)

Manual file: **`contract_registry/subscriptions.yaml`** — who subscribes to each `contract_id`, which fields they consume, and which `breaking_fields` trigger blast-radius alerts. Loader: `contracts/registry.py`.

## Tests and CI

```powershell
pytest -q
```

GitHub Actions (`.github/workflows/ci.yml`) runs `generator --all`, ValidationRunner on Week 3 and Week 5 with `--mode AUDIT`, then `pytest`.

## Repository layout

| Path | Role |
|------|------|
| `contracts/generator.py` | ContractGenerator |
| `contracts/runner.py` | ValidationRunner |
| `contracts/attributor.py` | ViolationAttributor |
| `contracts/schema_analyzer.py` | SchemaEvolutionAnalyzer |
| `contracts/ai_extensions.py` | AI Contract Extensions |
| `contracts/report_generator.py` | ReportGenerator |
| `contracts/validation_checks.py` | Week 1/2/4, LangSmith, cross-system checks |
| `contracts/violation_record.py` | Week 8–compatible violation shape helper |
| `contracts/registry.py` | Load `contract_registry/subscriptions.yaml` |
| `contract_registry/subscriptions.yaml` | Subscriber registry (blast radius) |
| `scripts/refresh_submission_artifacts.py` | Dedupe `violation_log/violations.jsonl` |
| `generated_contracts/` | Generated YAML + dbt |
| `outputs/` | JSONL inputs |
| `src/week1/handlers/` … | Stub files so Week 1 `code_refs.file` existence checks pass |

## DOMAIN_NOTES.md

Phase 0 write-up (backward/compatible changes, Bitol examples, trust boundary Q3, lineage blame chain, production failure modes) lives in `DOMAIN_NOTES.md`.

---

## Practitioner manual — full pipeline (Wednesday + Saturday checklist)

Run from this repository root (the folder that contains `contracts/` and `outputs/`).

**0. Prerequisites**

```powershell
pip install -r requirements.txt
python scripts/seed_outputs.py
```

Verify inputs exist: `outputs/week3/extractions.jsonl`, `outputs/week4/lineage_snapshots.jsonl`, `outputs/week5/events.jsonl`, `outputs/traces/runs.jsonl`.

**1. Registry**

File: `contract_registry/subscriptions.yaml` (≥4 subscriptions, `breaking_fields` on each). Quick check:

```powershell
Select-String -Path contract_registry/subscriptions.yaml -Pattern subscriber_id
```

**2. Generate contracts (Week 3 + Week 5)**

This repo uses contract ids `week3-document-refinery-extractions` and `week5-event-sourcing-events` (equivalent to the manual’s Week 5 event contract).

```powershell
python contracts/generator.py --source outputs/week3/extractions.jsonl --contract-id week3-document-refinery-extractions --lineage outputs/week4/lineage_snapshots.jsonl --registry contract_registry/subscriptions.yaml --output generated_contracts/
python contracts/generator.py --source outputs/week5/events.jsonl --contract-id week5-event-sourcing-events --lineage outputs/week4/lineage_snapshots.jsonl --registry contract_registry/subscriptions.yaml --output generated_contracts/
```

Expect: `generated_contracts/week3_extractions.yaml`, `generated_contracts/week5_events.yaml`, matching `*_dbt.yml`, and new folders under `schema_snapshots/<contract-id>/`.

**3. Baseline validation (clean data, AUDIT)**

```powershell
python contracts/runner.py --contract generated_contracts/week3_extractions.yaml --data outputs/week3/extractions.jsonl --mode AUDIT --output validation_reports/clean.json
```

Expect: `validation_reports/clean.json` with failures appropriate to your data; `schema_snapshots/baselines.json` updated when the runner records numeric baselines.

**4. Inject scale violation + ENFORCE run**

```powershell
python create_violation.py
python contracts/runner.py --contract generated_contracts/week3_extractions.yaml --data outputs/week3/extractions_violated.jsonl --mode ENFORCE --output validation_reports/violated.json
```

Expect: `validation_reports/violated.json` includes **FAIL** for confidence **range** and **statistical_drift** (if baselines were established from the clean run).

**5. Violation attributor (registry-first; manual-compatible flags)**

`--violation` points at a ValidationRunner JSON report; only **FAIL/ERROR** `results[]` rows are converted and attributed (handy for the injected run). It does **not** merge the full historical `violations.jsonl`.

```powershell
python contracts/attributor.py --violation validation_reports/violated.json --lineage outputs/week4/lineage_snapshots.jsonl --registry contract_registry/subscriptions.yaml --output violation_log/violations_with_blame.jsonl
```

Or enrich the existing log:

```powershell
python contracts/attributor.py --input violation_log/violations.jsonl --lineage outputs/week4/lineage_snapshots.jsonl --registry contract_registry/subscriptions.yaml --output violation_log/violations_with_blame.jsonl
```

**6. Schema evolution (two snapshots under `schema_snapshots/<contract-id>/`)**

```powershell
python contracts/schema_analyzer.py --contract-id week3-document-refinery-extractions --output validation_reports/schema_evolution.json
```

**7. AI extensions**

```powershell
python contracts/ai_extensions.py --extractions outputs/week3/extractions.jsonl --verdicts outputs/week2/verdicts.jsonl --output validation_reports/ai_metrics.json --also-write-ai-extensions-name
```

**8. Enforcer report**

```powershell
python contracts/report_generator.py
```

Open `enforcer_report/report_data.json`. Verify `data_health_score` is between 0 and 100, `recommended_actions` reference real paths under this repository (e.g. `contracts/runner.py`, `src/week3/extractor.py`), and `top_violations_plain_language` / `violations_by_severity` align with `validation_reports/*.json`.

**9. Tests**

```powershell
pytest -q
```
