# Video demo script (max ~6 minutes)

Work from the **repository root**: `Week 7 (Data Contract Enforcer)`.

On Windows, if `python` is not on PATH, use **`py -3`** instead of `python` everywhere below.

Some scripts must be run as **modules** so `import contracts` resolves (see Step 4 and Step 6).

---

## What to say in one breath (optional intro)

This project is a **data contract enforcer**: it turns pipeline JSONL into explicit **YAML contracts**, **validates** data against them, **attributes** failures using lineage and git, measures **schema evolution**, runs **AI-specific checks**, and produces a **health report** (`report_data.json` + optional PDF).

---

## Before recording

1. Open a terminal at the repo root and an editor (or split view).
2. Ensure the violated dataset exists (scales confidence 0–1 → 0–100):

   ```powershell
   py -3 create_violation.py
   ```

   Output: `outputs/week3/extractions_violated.jsonl`

---

## Minutes 1–3

### Step 1 — Contract generation

**Narration:** Profile real Week 3 extractions and emit a Bitol-style data contract.

**Command:**

```powershell
py -3 contracts/generator.py --source outputs/week3/extractions.jsonl --output generated_contracts
```

**Show on screen:**

- File: `generated_contracts/week3_extractions.yaml`
- Point out **at least eight distinct rules** (schema / terms), for example:
  - `doc_id`: UUID, required  
  - `source_hash`: pattern `^[a-f0-9]{64}$` (64-char hex, SHA-256 style)  
  - `extracted_facts`: `minItems: 1`, required  
  - **`extracted_facts.items.properties.confidence`**: `minimum: 0.0`, `maximum: 1.0` — the **confidence range clause**  
  - `entities` / entity `type` enum  
  - `extraction_model`: pattern for `claude-*` / `gpt-*`  
  - `processing_time_ms`: positive integer  
  - `token_count`: nested required object  

Scroll slowly so the **confidence** block is clearly visible.

---

### Step 2 — Violation detection

**Narration:** Run the validator against data that breaks the contract (confidence scaled to 0–100).

**Command:**

```powershell
py -3 contracts/runner.py --contract generated_contracts/week3_extractions.yaml --data outputs/week3/extractions_violated.jsonl --output validation_reports/demo_violated.json
```

**Show on screen:**

- File: `validation_reports/demo_violated.json`
- Find the result with `"check_id": "week3.extracted_facts.confidence.range"`.
- Call out:
  - **`"status": "FAIL"`**
  - **`"severity"`** (e.g. `CRITICAL`)
  - **`"records_failing"`** (count of failing records)

---

### Step 3 — Blame chain

**Narration:** Feed the validation JSON into the attributor to get lineage traversal, git-linked blame, and downstream blast radius.

**Command:**

```powershell
py -3 contracts/attributor.py --violation validation_reports/demo_violated.json --output violation_log/demo_blame.jsonl
```

**Show on screen:**

- Terminal: first JSON line printed by the script, **or** open `violation_log/demo_blame.jsonl`.
- Explicitly mention:
  - **Lineage / traversal:** `attribution_context`, `blast_radius.lineage_enrichment` (forward-reachable files/pipelines)
  - **Commit:** `blame_chain[0].commit_hash`
  - **Author:** `blame_chain[0].author`
  - **Blast radius:** `blast_radius.affected_nodes`, `affected_pipelines`, `estimated_records`, `subscribers`

---

## Minutes 4–6

### Step 4 — Schema evolution

**Narration:** Diff two schema snapshots for the Week 3 contract; show breaking vs compatible classification and the migration impact report.

**Command** (use **module** form — avoids `ModuleNotFoundError: contracts` on some setups):

```powershell
py -3 -m contracts.schema_analyzer --contract-id week3-document-refinery-extractions --since "365 days ago" --output validation_reports/schema_evolution_demo.json
```

**Show on screen:**

- `validation_reports/schema_evolution_demo.json` — e.g. `diff.compatibility_verdict`, `diff.breaking_changes`
- A second file written under `validation_reports/` named like `migration_impact_week3-document-refinery-extractions_<timestamp>.json` — `migration_impact`, `migration_checklist`, `rollback_plan` (or `per_consumer_failure_modes`)

**Optional — pin two snapshot folders explicitly:**

```powershell
py -3 -m contracts.schema_analyzer --contract-id week3-document-refinery-extractions --snapshot-a schema_snapshots/week3-document-refinery-extractions/<older_folder> --snapshot-b schema_snapshots/week3-document-refinery-extractions/<newer_folder> --output validation_reports/schema_evolution_demo.json
```

---

### Step 5 — AI extensions

**Narration:** On real Week 3 extraction text, show embedding drift score, prompt input validation, and LLM output schema violation rate (Week 2 verdicts).

**Command:**

```powershell
py -3 contracts/ai_extensions.py --extractions outputs/week3/extractions.jsonl --output validation_reports/ai_metrics.json
```

**Show on screen:**

- `validation_reports/ai_metrics.json`
  - **`embedding_drift`:** `drift_score`, `status`, `threshold`
  - **`prompt_input_validation`:** `status`, `quarantined_count`
  - **LLM output schema:** `schema_violations`, `violation_rate`, `status`

If all checks are PASS, state that clearly and still read the numeric fields. Optional tuning via `.env` / env vars (see `contracts/ai_extensions.py` docstring, e.g. `CONTRACT_AI_EMBEDDING_DRIFT_THRESHOLD`).

---

### Step 6 — Enforcer report

**Narration:** Run the report generator end-to-end; show consolidated JSON with data health score and top violations in plain language.

**Command** (module form):

```powershell
py -3 -m contracts.report_generator
```

**Show on screen:**

- `enforcer_report/report_data.json`
  - **`data_health_score`**
  - **`top_violations_plain_language`** — read the **first three** entries aloud (if fewer than three exist, say how many there are)
- Optionally mention `enforcer_report/report_YYYYMMDD.pdf` if present.

To point the report at specific inputs, see `py -3 -m contracts.report_generator --help` (`--week3-report`, `--ai-metrics`, `--violations`, `--migration-report`, etc.).

---

## Timing cheat sheet

| Block   | Steps   | Focus |
|--------|---------|--------|
| 1–2 min | 1 + start 2 | Generator + runner + FAIL JSON |
| 2–3 min | finish 2 + 3 | Severity, `records_failing`, attributor |
| 4–5 min | 4 + 5 | Schema diff + `ai_metrics.json` |
| 5–6 min | 6 | `report_data.json` health + top violations |

---

## Windows / import note

If `py -3 contracts/schema_analyzer.py` or `py -3 contracts/report_generator.py` fails with **`No module named 'contracts'`**, always use:

- `py -3 -m contracts.schema_analyzer ...`
- `py -3 -m contracts.report_generator ...`

from the **repo root**.
