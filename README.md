# TRP Week 7 — Data Contract Enforcer

This repository implements the Week 7 “Data Contract Enforcer” pipeline: it auto-generates Bitol-style data contract YAML from your Weeks 1–5 outputs, validates datasets against those contracts, attributes violations back to upstream code using the Week 4 lineage graph, applies AI-specific contract extensions, and generates an Enforcer Report.

## 0) Prerequisites

Install Python dependencies:

```powershell
pip install -r requirements.txt
```

## 1) (Re)generate sample Week outputs (JSONL)

The repo includes seeded sample outputs for Weeks 1–5 so the validators have real data to run against.

```powershell
python scripts/seed_outputs.py
```

Expected result:
- `outputs/week3/extractions.jsonl` (55 records)
- `outputs/week5/events.jsonl` (55 records)
- `outputs/week2/verdicts.jsonl` (25 records with a few injected schema violations)

## 2) Contract generation (Phase 1)

Generate contracts + dbt equivalents:

```powershell
python contracts/generator.py --all
```

Expected outputs:
- `generated_contracts/week3_extractions.yaml`
- `generated_contracts/week5_events.yaml`
- `generated_contracts/week1_intent_records.yaml`
- `generated_contracts/week2_verdicts.yaml`
- `generated_contracts/week4_lineage.yaml`
- `generated_contracts/langsmith_traces.yaml`
- dbt counterparts:
  - `generated_contracts/week3_extractions_dbt.yml`
  - `generated_contracts/week5_events_dbt.yml`

Contracts also create schema snapshots under:
- `schema_snapshots/<contract-id>/<timestamp>/schema.yaml`

## 3) ValidationRunner (Phase 2)

Run validation for Week 3 extractions:

```powershell
python contracts/runner.py `
  --contract generated_contracts/week3_extractions.yaml `
  --data outputs/week3/extractions.jsonl `
  --output validation_reports/week3_latest.json
```

Expected result (example):
- Output JSON written to `validation_reports/week3_latest.json`
- The `week3.extracted_facts.confidence.range` check must FAIL (CRITICAL) due to injected 0–100 confidence drift.

Run validation for Week 5 events:

```powershell
python contracts/runner.py `
  --contract generated_contracts/week5_events.yaml `
  --data outputs/week5/events.jsonl `
  --output validation_reports/week5_latest.json
```

Expected result:
- Output JSON written to `validation_reports/week5_latest.json`

## 4) Seed violation log (required baseline)

Initial violations are stored in:
- `violation_log/violations.jsonl`

This includes:
- real Week 3 validation failures
- at least one intentionally injected violation (documented in the top comment of the file)

## 5) ViolationAttributor (Phase 2B)

Enrich each violation with a blame chain and blast radius:

```powershell
python contracts/attributor.py `
  --input violation_log/violations.jsonl `
  --output violation_log/violations_with_blame.jsonl
```

Expected outputs:
- `violation_log/violations_with_blame.jsonl`
- each record includes:
  - `blame_chain[]` with `commit_hash` (40 hex chars)
  - `blast_radius`

## 6) Schema evolution analysis (Phase 3)

Diff the last two schema snapshots for Week 3:

```powershell
python contracts/schema_analyzer.py `
  --contract-id week3-document-refinery-extractions `
  --since "7 days ago" `
  --output validation_reports/schema_evolution_week3.json
```

Expected result:
- `validation_reports/schema_evolution_week3.json`
- at least one breaking change must be classified.

## 7) AI extensions (Phase 4A)

Run embedding drift detection, prompt input validation, and structured LLM output enforcement:

```powershell
python contracts/ai_extensions.py
```

Expected outputs:
- `validation_reports/ai_metrics.json`
- `violation_log/violations.jsonl` is appended with `type = "llm_output_schema"` violations when Week 2 structured output fails.

## 8) Enforcer Report (Phase 4B)

Generate stakeholder report (JSON + PDF):

```powershell
python contracts/report_generator.py
```

Expected outputs:
- `enforcer_report/report_data.json`
- `enforcer_report/report_<YYYYMMDD>.pdf`

# Data Contract Enforcer

This project generates versioned data contracts from sample JSONL outputs, validates those contracts against data, attributes contract violations to likely upstream code, and produces an end-of-week report (JSON + PDF).

## What’s included

- `contracts/generator.py`: Generates YAML data contracts + schema snapshots (and dbt-style YAML stubs).
- `contracts/runner.py`: Runs contract checks over JSONL datasets and writes `validation_reports/*.json`.
- `contracts/ai_extensions.py`: Extra (offline) checks (embedding drift + prompt input checks + structured LLM output schema enforcement). Appends to `violation_log/violations.jsonl`.
- `contracts/attributor.py`: Enriches violations with a blame chain + blast radius, producing `violation_log/violations_with_blame.jsonl`.
- `contracts/report_generator.py`: Builds `enforcer_report/report_data.json` and `enforcer_report/report_*.pdf`.
- `contracts/schema_analyzer.py`: Compares schema snapshots and generates a migration impact report.

## Requirements

- Python 3.10+ recommended
- `pip install -r requirements.txt`

Optional:

- Set `USE_YDATA=1` to enable `ydata-profiling` in `contracts/generator.py` (only if installed).

## Quickstart (Windows / PowerShell)

1. Install dependencies:

   `pip install -r requirements.txt`

2. (Optional) Regenerate sample pipeline outputs:

   `python scripts/seed_outputs.py`

3. Generate contracts + schema snapshots:

   `python contracts/generator.py --all --output generated_contracts`

4. Validate Week 3 and Week 5 datasets:

   `python contracts/runner.py --contract generated_contracts/week3_extractions.yaml --data outputs/week3/extractions.jsonl --output validation_reports/week3_latest.json`

   `python contracts/runner.py --contract generated_contracts/week5_events.yaml --data outputs/week5/events.jsonl --output validation_reports/week5_latest.json`

5. Run AI extensions, then attribute violations:

   `python contracts/ai_extensions.py`

   `python contracts/attributor.py`

6. Generate the final report (JSON + PDF):

   `python contracts/report_generator.py`

After this, check:

- `enforcer_report/report_data.json`
- `enforcer_report/report_YYYYMMDD.pdf` (or similar)
- `violation_log/violations_with_blame.jsonl`

## Optional: Schema evolution report

Example (Week 3):

`python contracts/schema_analyzer.py --contract-id week3-document-refinery-extractions --since "7 days ago" --output validation_reports/schema_evolution_week3.json`

## Notes

- `contracts/attributor.py` will try to use `git` history to infer commit blame; if git isn’t available, it falls back to synthetic candidates based on file metadata.

