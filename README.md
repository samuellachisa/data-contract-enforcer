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

