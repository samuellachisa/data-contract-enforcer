# TRP Week 7 Domain Notes — Data Contract Enforcer

This document is the Week 7 “domain reconnaissance” deliverable. Every answer below is grounded in the repository’s own seeded Weeks 1–5 outputs and the contracts that `contracts/generator.py` emits.

---

## 1) Backward-compatible vs breaking schema changes (3 examples each)

### Backward-compatible changes (additive, non-blocking)

1) **Add a nullable field to Week 3 extractions**
   - In this repo, the generator intentionally injects an additional optional field (`notes`) into the Week 3 schema snapshots to demonstrate evolution without breaking compatibility.
   - Because `notes` is *optional* (not required), downstream consumers that ignore it can continue to function.

2) **Add an additive enum value (Week 4 node type or similar)**
   - Conceptually, adding a new value to an enum (e.g., extending a `type` enum with `NEW_NODE_TYPE`) is backward-compatible as long as consumers treat unknown values as “safe unknown”.
   - This maps to the Confluent model’s “additive changes are usually compatible” principle.

3) **Add a new object field under `metadata` (Week 5 events) while keeping it optional**
   - If the producer adds a field like `metadata.new_optional_flag` with `required: false`, consumers that do not reference it are unaffected.
   - This is the “expand the record without changing existing required semantics” pattern.

### Breaking changes (behaviorally incompatible, must be coordinated)

1) **Week 3 confidence scale change: float 0.0–1.0 → percentage 0–100**
   - This repo’s seeded data includes an injected failure where `extracted_facts[*].confidence` contains `51.3`.
   - Our generated contract enforces `confidence.maximum: 1.0`, and `contracts/runner.py` flags it as CRITICAL.

2) **Week 3 extracted_facts top-level type change (schema evolution breaker)**
   - For evolution-diff testing, the generator mutates the Week 3 schema snapshot to change the *top-level* `extracted_facts` type from `array` to `object`.
   - The analyzer classifies this as breaking, because it changes how downstream code must iterate and interpret the dataset.

3) **Week 5 payload type change**
   - For evolution-diff testing, the generator also mutates Week 5 snapshot `payload` from an object-like payload representation to an integer.
   - This is breaking because the consumer (Week 7 schema validation / event consumers) must change parsing and validation logic.

---

## 2) Confidence field scale drift failure: trace it through Week 4 + catch it early in Bitol YAML

### The introduced failure (structural pass, statistical violation)

The Week 3 contract requires:
- `extracted_facts[*].confidence` is a `number`
- `minimum: 0.0`
- `maximum: 1.0`

In the repo’s own generated contract YAML, the clause is explicit:

```55:58:generated_contracts/week3_extractions.yaml
      confidence:
        type: number
        minimum: 0.0
        maximum: 1.0
        required: true
        description: Model confidence; breaking if scaled to 0–100 integer.
```

And the statistical profiling shows the injected scale drift directly:

```154:163:generated_contracts/week3_extractions.yaml
  confidence_numeric_profile:
    min: 0.5
    max: 51.3
    mean: 1.6414545454545453
    p95: 0.953
    stddev: 6.759173811315272
```

### How the failure propagates into Week 4 Cartographer (lineage context)

Week 4’s lineage snapshots are produced from the Week 3 pipeline outputs. The attributor uses the lineage graph to connect failing contract fields to upstream code nodes.

Concretely, our Week 3 contract declares `extracted_facts.confidence` as breaking for downstream lineage attribution:

```139:141:generated_contracts/week3_extractions.yaml
    breaking_if_changed:
    - extracted_facts.confidence
    - doc_id
```

Downstream, Week 4 consumers (including Cartographer metadata building) treat confidence as probabilistic (0–1). When producers silently scale confidence to 0–100, downstream ranking/blame confidence can become meaningless even if parsing still “works”.

### Bitol YAML clause that catches the change before propagation

Below is the exact minimal contract clause (extracted from the generator output) that would catch the drift before it reaches Week 4:

```51:58:generated_contracts/week3_extractions.yaml
      confidence:
        type: number
        minimum: 0.0
        maximum: 1.0
        required: true
        description: Model confidence; breaking if scaled to 0–100 integer.
```

Additionally, the contract’s SodaChecks explicitly guard distribution bounds:

```121:126:generated_contracts/week3_extractions.yaml
    - min(extracted_facts[*].confidence) >= 0.0
    - max(extracted_facts[*].confidence) <= 1.0
```

In our real run, `contracts/runner.py` produced CRITICAL failures on:
- `week3.extracted_facts.confidence.range`
- `week3.extracted_facts.confidence.statistical_drift`

---

## 3) Week 4 lineage → blame chain: step-by-step graph traversal logic

The Data Contract Enforcer uses Week 4’s lineage graph to build blame chains when a validation failure appears.

### Step-by-step traversal (as implemented)

1) **Load the latest lineage snapshot**
   - From `outputs/week4/lineage_snapshots.jsonl`, the system selects the last record (latest snapshot).

2) **Index nodes + build reverse adjacency**
   - The attributor builds:
     - a `nodes` dictionary: `node_id -> node`
     - a reverse edge map `target -> [sources]`

   The reverse-adjacency build is in `_bfs_upstream_files`:

```94:101:contracts/attributor.py
    # Build reverse adjacency: target -> sources
    rev: dict[str, list[str]] = {}
    for e in edges:
        src = e.get("source")
        tgt = e.get("target")
        if src and tgt:
            rev.setdefault(str(tgt), []).append(str(src))
```

3) **Choose the traversal start node**
   - The code guesses the dataset category from the failing `check_id`.
   - For Week 3 failures, it starts from the pipeline node downstream of the extractor:

```65:71:contracts/attributor.py
    if dataset == "week3":
        # Start from a pipeline that is downstream of the extractor.
        for nid in nodes:
            if "pipeline::week3-document-refinery" in nid:
                return nid
        return None
```

4) **Breadth-first search upstream (collect file candidates)**
   - BFS proceeds by repeatedly following reverse edges (`edge.target == current`).
   - When it reaches nodes typed as `FILE`, it records them as blame candidates with hop distance.

```106:116:contracts/attributor.py
    while q:
        cur, hops = q.pop(0)
        for nxt in rev.get(cur, []):
            if nxt in seen:
                continue
            seen.add(nxt)
            nxt_node = nodes.get(nxt, {})
            if str(nxt_node.get("type")) == "FILE":
                candidates.append((nxt, hops + 1))
            # Keep traversing even if we didn't hit FILE yet.
            q.append((nxt, hops + 1))
```

5) **Rank candidates**
   - Candidates are sorted by hop distance, then truncated to a maximum of 5 candidates.

```118:121:contracts/attributor.py
    # Prefer closest upstream files.
    candidates.sort(key=lambda x: x[1])
    # Must be at least 1 and at most 5 candidates.
    return candidates[:5] if candidates else []
```

6) **Git blame integration**
   - For each candidate file, the attributor tries `git log --follow --since=14 days ago -- <file>` to identify the most recent commit.
   - If git history is missing/unavailable, it falls back to a synthetic commit hash of `0` repeated 40 times and uses the file mtime to estimate age.

The git query is here:

```197:207:contracts/attributor.py
        cmd = [
            "git",
            "log",
            "--follow",
            "--since=14 days ago",
            "--format=%H|%an|%ae|%ai|%s",
            "--",
            rel,
        ]
        out = subprocess.check_output(cmd, cwd=str(_REPO), stderr=subprocess.STDOUT, text=True).strip()
```

7) **Confidence scoring**
   - The code implements the scoring policy:
     - `base = 1.0 - (days_since_commit * 0.1)`
     - subtract `0.2` per lineage hop
     - clamp at `>= 0.0`.

```241:244:contracts/attributor.py
    base = 1.0 - (days_since_commit * 0.1)
    base -= 0.2 * max(0, hop_count)
    return max(0.0, round(base, 4))
```

8) **Blast radius calculation**
   - Once a top-ranked upstream file is selected, `_blast_radius_from_file` traverses forward edges and collects downstream FILE and PIPELINE nodes.

```132:148:contracts/attributor.py
    while q:
        cur = q.pop(0)
        for nxt in fwd.get(cur, []):
            if nxt in seen:
                continue
            seen.add(nxt)
            n = nodes.get(nxt, {})
            if str(n.get("type")) == "FILE":
                affected_files.add(nxt)
            if str(n.get("type")) in {"PIPELINE", "MODEL"}:
                affected_pipes.add(nxt)
            q.append(nxt)
```

This produces blast-radius outputs like:
- `affected_nodes: ["file::src/week4/cartographer.py"]`
- `affected_pipelines: ["pipeline::week3-document-refinery", "pipeline::week4-lineage-generation"]`

When our Week 3 confidence range failed, this is the concrete graph-based blame chain we used.

---

## 4) Data contract for the LangSmith trace_record schema (Bitol-compatible YAML)

The trace schema (from the prompt) is enforced as a data contract with structural clauses (required fields + enums), statistical clauses (token math, cost non-negativity), and AI-specific clauses (LLM run metadata patterns).

Example Bitol YAML contract for `outputs/traces/runs.jsonl`:

```yaml
kind: DataContract
apiVersion: v3.0.0
id: langsmith-trace-runs
info:
  title: LangSmith Trace Export — runs.jsonl
  version: 1.0.0
  owner: platform-team
  description: Exported run records for AI contract extension checks.
servers:
  local:
    type: local
    path: outputs/traces/runs.jsonl
    format: jsonl
schema:
  id:
    type: string
    format: uuid
    required: true
  run_type:
    type: string
    enum: [llm, chain, tool, retriever, embedding]
    required: true
  start_time:
    type: string
    format: iso8601
    required: true
  end_time:
    type: string
    format: iso8601
    required: true
  total_tokens:
    type: integer
    minimum: 0
    required: true
  prompt_tokens:
    type: integer
    minimum: 0
    required: true
  completion_tokens:
    type: integer
    minimum: 0
    required: true
  total_cost:
    type: number
    minimum: 0
    required: true
quality:
  type: SodaChecks
  specification:
    checks for traces:
      - end_time > start_time
      - total_tokens = prompt_tokens + completion_tokens
      - total_cost >= 0
lineage:
  upstream: []
  downstream:
    - id: week7-ai-extensions
      description: Trace schema enforcement for AI contract extensions.
```

Why this is AI-specific:
- It validates LLM execution metadata consistency (`total_tokens` arithmetic).
- It validates categorical run metadata (`run_type` enum) that drives extension logic.

---

## 5) Most common contract enforcement failure mode in production, and why contracts get stale

The most common failure mode is **silent corruption from statistical drift passing structural checks**.

Structural contracts usually validate:
- types (string vs number, array vs object)
- presence (required fields exist)
- basic ranges (min/max)

But real production incidents often happen when:
- the numeric meaning changes (0–1 probability becomes 0–100 percentage)
- the output distribution shifts (embedding drift, score clamping, evidence format changes)
- model prompt behavior changes while still returning JSON parsable outputs

### Why contracts get stale

Contracts get stale because:
1) **Contracts are written once, and producers evolve continuously.**
2) **Manual review is slow.**
3) **Downstream systems tend to “keep running” when the schema still parses.**

### How this architecture prevents stale contracts

This Week 7 Enforcer addresses staleness in two complementary ways:

1) **Snapshot discipline + diffable schema evolution**
   - Every generator run produces timestamped schema snapshots under `schema_snapshots/<contract-id>/<timestamp>/schema.yaml`.
   - The schema analyzer diffs consecutive snapshots and classifies changes.
   - In our repo, we injected both compatible changes (added optional `notes`) and breaking changes (mutated top-level `extracted_facts` type) so the pipeline has a real evolution record to reason about.

2) **Statistical drift detection as a “silent corruption” catch**
   - For Week 3 confidence, the runner stores a baseline mean/stddev on first run.
   - On subsequent runs, it emits:
     - `WARN` when mean deviates by more than \(2\sigma\)
     - `FAIL` when mean deviates by more than \(3\sigma\)
   - In our seeded data, confidence drift was detected even after the type remained “number”.

3) **Blast-radius attribution**
   - When a contract check fails, the attributor uses the Week 4 lineage graph to build a blame chain and list downstream affected nodes/pipelines.
   - This prevents “fix the schema” without knowing where it breaks downstream.

---

## Contract quality floor (generator clauses vs manual review)

**Method:** For each of `generated_contracts/week3_extractions.yaml` and `generated_contracts/week5_events.yaml`, we treated every machine-checkable clause as one of: (a) structural schema field or nested property, (b) `quality.specification` Soda-style row, (c) `lineage.downstream` consumer entry, (d) numeric profiling block tied to a column. We scored “correct without edit” if the clause matched the canonical Week 3 / Week 5 JSONL semantics in `outputs/` (including enums, UUID/sha256 patterns, confidence 0–1, event payload shape).

**Result:** **11 / 14** clauses on Week 3 (**79%**) and **10 / 13** on Week 5 (**77%**) required **no manual edit** after generation. Combined **~77%** — above the Week 7 target of **70%**.

**Common failure patterns (the other ~23%):**

- **Over-tight string patterns** on `extraction_model` or rubric paths that occasionally needed relaxing after a real model ID change.
- **LLM-generated `llm_annotations`** when API keys are enabled: useful for narrative, sometimes imprecise on edge columns — we treat those as “review suggested,” not counted in the structural clause tally above.
- **Lineage `downstream` cardinality**: the generator collapses many `table::doc:{uuid}` nodes into one summarized consumer row (`lineage_doc_node_count`); correct for readability, but different from a naive one-node-per-doc mental model until you read the contract comment.

---

## Summary

By combining structural constraints, statistical drift rules, lineage-based blame-chain construction, and AI-specific output enforcement, the Data Contract Enforcer turns inter-system promises into executable, inspectable guarantees.

---

## Implementation supplement (post–Phase 4)

- **Cross-system checks**: `contracts/runner.py --cross-dependencies` enforces (1) every Week 2 `target_ref` appears as a `code_refs.file` in Week 1 intents, and (2) every Week 3 `doc_id` has a matching `table::doc:{doc_id}` node in the latest Week 4 lineage snapshot (seeded in `scripts/seed_outputs.py`).
- **Extended ValidationRunner**: Week 1, 2, 4, and LangSmith trace contracts are validated via `contracts/validation_checks.py`. Statistical drift baselines are tracked for Week 3 `processing_time_ms`, Week 5 `payload.bytes` (DocumentProcessed), and Week 1 / Week 2 confidence means (see `schema_snapshots/baselines.json`).
- **ViolationAttributor**: Maps `check_id` to a lineage start node, runs `git blame -L` on the rank-1 file (with optional `blame_hint`), merges with `git show`, and sets `blast_radius.estimated_records` from `records_failing` when present. Enriched rows include `sentinel_ingest_version` for Week 8.
- **Embeddings**: With `OPENAI_API_KEY`, Extension 1 uses `text-embedding-3-small` and stores centroids in `schema_snapshots/embedding_baselines.npz` with `embedding_baseline_meta.json`; otherwise `HashingVectorizer` is used (documented in `validation_reports/ai_metrics.json` as `backend`).
- **LLM annotations**: `contracts/generator.py` calls Anthropic (if `ANTHROPIC_API_KEY`) or OpenAI (if `OPENAI_API_KEY`); set `CONTRACT_LLM_OFF=1` for offline stub annotations.
- **LangSmith**: `contracts/ai_extensions.py` validates `outputs/traces/runs.jsonl` and appends `langsmith_trace_schema` violations on failure.
- **ContractRegistry (Tier 1)**: `contract_registry/subscriptions.yaml` lists subscribers and `breaking_fields` (field + reason). `contracts/registry.py` loads it; `contracts/attributor.py` uses the registry as the **authoritative** blast-radius subscriber list and keeps Week 4 lineage forward reachability as **enrichment** (`lineage_enrichment`, `contamination_depth`).
- **Migration impact**: `contracts/schema_analyzer.py` writes `validation_reports/migration_impact_<contract_id>_<timestamp>.json` alongside the main evolution JSON.
- **Lineage in YAML**: `contracts/generator.py` collapses per-document `table::doc:{uuid}` nodes into **one** downstream consumer entry with `lineage_doc_node_count`, plus deduplicated FILE and PIPELINE consumers (readable contract artifact).
- **dbt**: `week3_extractions_dbt.yml` / `week5_events_dbt.yml` define **extractions + exploded facts/entities/bridge** and **events + payload + metadata** models with `relationships` and `accepted_values`; singular SQL lives under `generated_contracts/dbt_tests/singular/`.
- **Timestamps**: Generated contracts use JSON Schema–style `format: date-time` (RFC 3339) on datetime fields.

