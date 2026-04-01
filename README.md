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

```powershell
python contracts/runner.py --contract generated_contracts/week3_extractions.yaml --data outputs/week3/extractions.jsonl --output validation_reports/week3_latest.json
python contracts/runner.py --contract generated_contracts/week5_events.yaml --data outputs/week5/events.jsonl --output validation_reports/week5_latest.json
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

```powershell
python contracts/attributor.py --input violation_log/violations.jsonl --output violation_log/violations_with_blame.jsonl
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
| `generated_contracts/` | Generated YAML + dbt |
| `outputs/` | JSONL inputs |
| `src/week1/handlers/` … | Stub files so Week 1 `code_refs.file` existence checks pass |

## DOMAIN_NOTES.md

Phase 0 write-up (backward/compatible changes, Bitol examples, lineage blame chain, production failure modes) lives in `DOMAIN_NOTES.md`.
