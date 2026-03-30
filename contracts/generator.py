#!/usr/bin/env python3
"""
ContractGenerator: profiles JSONL outputs, injects lineage context, emits Bitol YAML + dbt schema.yml.
Evaluators: python contracts/generator.py --source outputs/week3/extractions.jsonl --output generated_contracts/
"""
from __future__ import annotations

import argparse
import sys
import json
import os
import re
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from contracts.common import (
    iso_now,
    jsonl_snapshot_id,
    load_jsonl,
    repo_root,
    write_yaml,
)

try:
    from ydata_profiling import ProfileReport

    _HAS_YDATA = True
except Exception:
    _HAS_YDATA = False


def _flatten_extractions_for_profile(rows: list[dict]) -> pd.DataFrame:
    flat = []
    for r in rows:
        for fact in r.get("extracted_facts") or []:
            flat.append(
                {
                    "doc_id": r.get("doc_id"),
                    "source_hash": r.get("source_hash"),
                    "fact_confidence": fact.get("confidence"),
                    "fact_id": fact.get("fact_id"),
                    "entity_count": len(fact.get("entity_refs") or []),
                }
            )
    return pd.DataFrame(flat)


def _ydata_profile_summary(df: pd.DataFrame) -> dict[str, Any]:
    if df.empty or not _HAS_YDATA or os.environ.get("USE_YDATA") != "1":
        desc = df.describe(include="all").to_dict() if not df.empty else {}
        return {"engine": "pandas-fallback", "describe": desc, "columns": list(df.columns)}
    sample = df.head(min(200, len(df)))
    try:
        report = ProfileReport(sample, minimal=True, title="contract_profile", explorative=False)
        desc = report.get_description()
        return {"engine": "ydata-profiling", "variables": desc.get("variables", {})}
    except Exception as exc:  # pragma: no cover
        return {"engine": "ydata-failed", "error": str(exc), "columns": list(df.columns)}


def _numeric_stats(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    arr = np.array(values, dtype=float)
    return {
        "min": float(np.min(arr)),
        "max": float(np.max(arr)),
        "mean": float(np.mean(arr)),
        "p25": float(np.percentile(arr, 25)),
        "p50": float(np.percentile(arr, 50)),
        "p75": float(np.percentile(arr, 75)),
        "p95": float(np.percentile(arr, 95)),
        "p99": float(np.percentile(arr, 99)),
        "stddev": float(np.std(arr)),
    }


def _load_latest_lineage(root: Path) -> dict[str, Any] | None:
    path = root / "outputs" / "week4" / "lineage_snapshots.jsonl"
    if not path.exists():
        return None
    rows = load_jsonl(path)
    return rows[-1] if rows else None


def _downstream_for_dataset(lineage: dict[str, Any] | None, dataset_hint: str) -> list[dict[str, Any]]:
    if not lineage:
        return []
    nodes = {n["node_id"]: n for n in lineage.get("nodes", [])}
    out: list[dict[str, Any]] = []
    for e in lineage.get("edges", []):
        src = e.get("source", "")
        tgt = e.get("target", "")
        if dataset_hint in src or dataset_hint in tgt:
            tn = nodes.get(tgt, {})
            label = tn.get("label", tgt)
            out.append(
                {
                    "id": label,
                    "description": f"Lineage edge {e.get('relationship')} from {src} to {tgt}",
                    "fields_consumed": ["doc_id", "extracted_facts", "extraction_model"]
                    if "week3" in dataset_hint
                    else ["event_type", "payload", "sequence_number", "aggregate_id"],
                    "breaking_if_changed": ["extracted_facts.confidence", "doc_id"]
                    if "week3" in dataset_hint
                    else ["payload", "event_type"],
                }
            )
    return out


def _llm_annotations_stub(column: str, table: str, samples: list[Any], neighbors: list[str]) -> dict[str, Any]:
    return {
        "column": column,
        "table": table,
        "description": f"Inferred business column `{column}` adjacent to {neighbors}.",
        "business_rule": f"values for `{column}` should remain consistent with historical profile",
        "cross_column_relationship": "See contract lineage.downstream for consumer fields.",
        "samples": [str(s) for s in samples[:5]],
    }


def _maybe_llm_annotate(column: str, table: str, samples: list[Any], neighbors: list[str]) -> dict[str, Any]:
    key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not key:
        return _llm_annotations_stub(column, table, samples, neighbors)
    # Optional: real LLM call omitted to keep default runs offline; extend with your provider.
    return _llm_annotations_stub(column, table, samples, neighbors)


def build_week3_contract(rows: list[dict], root: Path) -> dict[str, Any]:
    lineage = _load_latest_lineage(root)
    df_flat = _flatten_extractions_for_profile(rows)
    profile = _ydata_profile_summary(df_flat)
    confidences = []
    for r in rows:
        for f in r.get("extracted_facts") or []:
            c = f.get("confidence")
            if isinstance(c, (int, float)):
                confidences.append(float(c))
    conf_stats = _numeric_stats(confidences)
    proc_ms = [r.get("processing_time_ms") for r in rows if isinstance(r.get("processing_time_ms"), int)]

    downstream = _downstream_for_dataset(lineage, "week3") or _downstream_for_dataset(
        lineage, "pipeline::week3"
    )
    if not downstream:
        downstream = [
            {
                "id": "week4-cartographer",
                "description": "Cartographer ingests doc_id and extracted_facts as node metadata",
                "fields_consumed": ["doc_id", "extracted_facts", "extraction_model"],
                "breaking_if_changed": ["extracted_facts.confidence", "doc_id"],
            }
        ]

    llm_block = _maybe_llm_annotate(
        "extracted_facts.confidence",
        "extractions",
        [str(x) for x in confidences[:20]],
        ["doc_id", "source_hash", "extraction_model"],
    )

    contract: dict[str, Any] = {
        "kind": "DataContract",
        "apiVersion": "v3.0.0",
        "id": "week3-document-refinery-extractions",
        "info": {
            "title": "Week 3 Document Refinery — Extraction Records",
            "version": "1.0.0",
            "owner": "week3-team",
            "description": "One record per processed document with extracted facts and entities.",
        },
        "servers": {
            "local": {
                "type": "local",
                "path": "outputs/week3/extractions.jsonl",
                "format": "jsonl",
            }
        },
        "terms": {
            "usage": "Internal inter-system data contract. Do not publish.",
            "limitations": "confidence must remain in 0.0–1.0 float range (not 0–100 int).",
        },
        "schema": {
            "doc_id": {
                "type": "string",
                "format": "uuid",
                "required": True,
                "unique": True,
                "description": "Primary key. UUIDv4 per document extraction run.",
            },
            "source_path": {
                "type": "string",
                "required": True,
                "minLength": 1,
                "description": "Absolute path or https URL of source document.",
            },
            "source_hash": {
                "type": "string",
                "pattern": "^[a-f0-9]{64}$",
                "required": True,
                "description": "SHA-256 of the source file.",
            },
            "extracted_facts": {
                "type": "array",
                "minItems": 1,
                "required": True,
                "items": {
                    "fact_id": {"type": "string", "format": "uuid", "required": True, "unique": True},
                    "text": {"type": "string", "required": True, "minLength": 1},
                    "entity_refs": {"type": "array", "items": {"type": "string", "format": "uuid"}},
                    "confidence": {
                        "type": "number",
                        "minimum": 0.0,
                        "maximum": 1.0,
                        "required": True,
                        "description": "Model confidence; breaking if scaled to 0–100 integer.",
                    },
                    "page_ref": {"type": "integer", "required": False, "nullable": True},
                    "source_excerpt": {"type": "string", "required": True},
                },
            },
            "entities": {
                "type": "array",
                "required": True,
                "items": {
                    "entity_id": {"type": "string", "format": "uuid", "required": True},
                    "name": {"type": "string", "required": True},
                    "type": {
                        "type": "string",
                        "enum": ["PERSON", "ORG", "LOCATION", "DATE", "AMOUNT", "OTHER"],
                        "required": True,
                    },
                    "canonical_value": {"type": "string", "required": True},
                },
            },
            "extraction_model": {
                "type": "string",
                "required": True,
                "pattern": "^(claude|gpt)-",
                "description": "Model identifier; must match claude-* or gpt-*.",
            },
            "processing_time_ms": {
                "type": "integer",
                "minimum": 1,
                "required": True,
                "description": "Wall-clock processing time in milliseconds; must be positive.",
            },
            "token_count": {
                "type": "object",
                "required": True,
                "properties": {
                    "input": {"type": "integer", "minimum": 0},
                    "output": {"type": "integer", "minimum": 0},
                },
            },
            "extracted_at": {
                "type": "string",
                "format": "iso8601",
                "required": True,
                "description": "Extraction completion timestamp (UTC Z).",
            },
        },
        "quality": {
            "type": "SodaChecks",
            "specification": {
                "checks for extractions": [
                    "missing_count(doc_id) = 0",
                    "duplicate_count(doc_id) = 0",
                    "min(fact_confidence) >= 0.0",
                    "max(fact_confidence) <= 1.0",
                    "row_count >= 1",
                ]
            },
        },
        "lineage": {"upstream": [], "downstream": downstream},
        "profiling": {
            "structural_engine": profile.get("engine"),
            "flat_row_count": int(len(df_flat)),
            "confidence_numeric_profile": conf_stats,
            "processing_time_ms_stats": _numeric_stats([float(x) for x in proc_ms]) if proc_ms else {},
        },
        "llm_annotations": llm_block,
    }
    return contract


def _write_week3_dbt(out_dir: Path) -> None:
    yml = {
        "version": 2,
        "models": [
            {
                "name": "extractions",
                "description": "Week 3 extraction records (JSONL ingested as external table).",
                "columns": [
                    {"name": "doc_id", "tests": ["not_null", "unique"]},
                    {"name": "source_hash", "tests": ["not_null"]},
                    {"name": "extraction_model", "tests": ["not_null"]},
                    {"name": "processing_time_ms", "tests": ["not_null"]},
                ],
            }
        ],
    }
    path = out_dir / "week3_extractions_dbt.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.dump(yml, f, default_flow_style=False, sort_keys=False, allow_unicode=True)


def build_week5_contract(rows: list[dict], root: Path) -> dict[str, Any]:
    lineage = _load_latest_lineage(root)
    downstream = _downstream_for_dataset(lineage, "week5")
    if not downstream:
        downstream = [
            {
                "id": "week7-contract-enforcer",
                "description": "Week 7 validates event payloads and ordering",
                "fields_consumed": ["event_type", "payload", "sequence_number", "aggregate_id"],
                "breaking_if_changed": ["payload", "event_type"],
            }
        ]
    contract: dict[str, Any] = {
        "kind": "DataContract",
        "apiVersion": "v3.0.0",
        "id": "week5-event-sourcing-events",
        "info": {
            "title": "Week 5 Event Sourcing — Event Records",
            "version": "1.0.0",
            "owner": "week5-team",
            "description": "Append-only event log per aggregate with JSON payload.",
        },
        "servers": {
            "local": {
                "type": "local",
                "path": "outputs/week5/events.jsonl",
                "format": "jsonl",
            }
        },
        "terms": {"usage": "Internal platform event contract.", "limitations": "PascalCase event_type registered in registry."},
        "schema": {
            "event_id": {"type": "string", "format": "uuid", "required": True, "unique": True},
            "event_type": {
                "type": "string",
                "required": True,
                "pattern": "^[A-Z][a-zA-Z0-9]*$",
                "description": "PascalCase event type registered in event schema registry.",
            },
            "aggregate_id": {"type": "string", "format": "uuid", "required": True},
            "aggregate_type": {"type": "string", "required": True, "pattern": "^[A-Z][a-zA-Z0-9]*$"},
            "sequence_number": {
                "type": "integer",
                "minimum": 0,
                "required": True,
                "description": "Monotonic per aggregate_id, no gaps or duplicates.",
            },
            "payload": {"type": "object", "required": True, "description": "Must validate against event_type JSON Schema."},
            "metadata": {
                "type": "object",
                "required": True,
                "properties": {
                    "causation_id": {"type": ["string", "null"], "format": "uuid"},
                    "correlation_id": {"type": "string", "format": "uuid", "required": True},
                    "user_id": {"type": "string", "required": True},
                    "source_service": {"type": "string", "required": True},
                },
            },
            "schema_version": {"type": "string", "required": True},
            "occurred_at": {"type": "string", "format": "iso8601", "required": True},
            "recorded_at": {"type": "string", "format": "iso8601", "required": True},
        },
        "quality": {
            "type": "SodaChecks",
            "specification": {
                "checks for events": [
                    "missing_count(event_id) = 0",
                    "missing_count(sequence_number) = 0",
                    "recorded_at >= occurred_at",
                ]
            },
        },
        "lineage": {"upstream": [], "downstream": downstream},
        "llm_annotations": _maybe_llm_annotate(
            "payload.bytes",
            "events",
            [str(r.get("payload", {}).get("bytes")) for r in rows[:20]],
            ["event_type", "aggregate_id"],
        ),
    }
    return contract


def _write_week5_dbt(out_dir: Path) -> None:
    yml = {
        "version": 2,
        "models": [
            {
                "name": "events",
                "description": "Week 5 event store projection source.",
                "columns": [
                    {"name": "event_id", "tests": ["not_null", "unique"]},
                    {"name": "aggregate_id", "tests": ["not_null"]},
                    {"name": "sequence_number", "tests": ["not_null"]},
                    {"name": "event_type", "tests": ["not_null"]},
                ],
            }
        ],
    }
    path = out_dir / "week5_events_dbt.yml"
    with path.open("w", encoding="utf-8") as f:
        yaml.dump(yml, f, default_flow_style=False, sort_keys=False, allow_unicode=True)


def _write_event_payload_schema(root: Path) -> None:
    reg_dir = root / "generated_contracts" / "event_payload_schemas"
    reg_dir.mkdir(parents=True, exist_ok=True)
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "required": ["doc_id", "status", "bytes"],
        "properties": {
            "doc_id": {"type": "string", "format": "uuid"},
            "status": {"type": "string", "enum": ["done", "failed"]},
            "bytes": {"type": "integer", "minimum": 0},
        },
        "additionalProperties": True,
    }
    (reg_dir / "DocumentProcessed.json").write_text(json.dumps(schema, indent=2), encoding="utf-8")
    registry = {
        "DocumentProcessed": "generated_contracts/event_payload_schemas/DocumentProcessed.json",
    }
    (root / "generated_contracts" / "event_schema_registry.json").write_text(
        json.dumps(registry, indent=2), encoding="utf-8"
    )


def build_week4_contract(rows: list[dict], root: Path) -> dict[str, Any]:
    contract = {
        "kind": "DataContract",
        "apiVersion": "v3.0.0",
        "id": "week4-brownfield-lineage-snapshots",
        "info": {
            "title": "Week 4 Brownfield Cartographer — Lineage Snapshots",
            "version": "1.0.0",
            "owner": "week4-team",
            "description": "Graph snapshot of code/data nodes and relationships.",
        },
        "servers": {
            "local": {"type": "local", "path": "outputs/week4/lineage_snapshots.jsonl", "format": "jsonl"}
        },
        "terms": {"usage": "Internal lineage for attribution and blast radius.", "limitations": "git_commit must be 40 hex chars."},
        "schema": {
            "snapshot_id": {"type": "string", "format": "uuid", "required": True},
            "codebase_root": {"type": "string", "required": True},
            "git_commit": {"type": "string", "pattern": "^[a-f0-9]{40}$", "required": True},
            "nodes": {"type": "array", "minItems": 1, "required": True},
            "edges": {"type": "array", "required": True},
            "captured_at": {"type": "string", "format": "iso8601", "required": True},
        },
        "quality": {"type": "SodaChecks", "specification": {"checks for lineage": ["row_count >= 1"]}},
        "lineage": {
            "upstream": [],
            "downstream": [
                {
                    "id": "week7-violation-attributor",
                    "description": "ViolationAttributor consumes lineage for blame chains",
                    "fields_consumed": ["nodes", "edges", "git_commit"],
                    "breaking_if_changed": ["edges", "nodes"],
                }
            ],
        },
    }
    return contract


def build_langsmith_contract(rows: list[dict], root: Path) -> dict[str, Any]:
    return {
        "kind": "DataContract",
        "apiVersion": "v3.0.0",
        "id": "langsmith-trace-runs",
        "info": {
            "title": "LangSmith Trace Export — runs.jsonl",
            "version": "1.0.0",
            "owner": "platform-team",
            "description": "Exported run records for AI contract extension checks.",
        },
        "servers": {"local": {"type": "local", "path": "outputs/traces/runs.jsonl", "format": "jsonl"}},
        "schema": {
            "id": {"type": "string", "format": "uuid", "required": True},
            "name": {"type": "string", "required": True},
            "run_type": {
                "type": "string",
                "enum": ["llm", "chain", "tool", "retriever", "embedding"],
                "required": True,
            },
            "start_time": {"type": "string", "format": "iso8601", "required": True},
            "end_time": {"type": "string", "format": "iso8601", "required": True},
            "total_tokens": {"type": "integer", "minimum": 0, "required": True},
            "prompt_tokens": {"type": "integer", "minimum": 0, "required": True},
            "completion_tokens": {"type": "integer", "minimum": 0, "required": True},
            "total_cost": {"type": "number", "minimum": 0.0, "required": True},
        },
        "quality": {
            "type": "SodaChecks",
            "specification": {
                "checks for traces": [
                    "total_tokens = prompt_tokens + completion_tokens",
                    "end_time > start_time",
                ]
            },
        },
        "lineage": {"upstream": [], "downstream": [{"id": "week7-ai-extensions", "description": "Trace schema enforcement"}]},
    }


def _write_schema_snapshot(contract_id: str, inferred_schema: dict[str, Any], root: Path) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    base = root / "schema_snapshots" / contract_id / ts
    base.mkdir(parents=True, exist_ok=True)
    write_yaml(base / "schema.yaml", {"contract_id": contract_id, "captured_at": iso_now(), "schema": inferred_schema})


def _infer_simple_schema_from_rows(rows: list[dict]) -> dict[str, Any]:
    """Minimal inferred schema for evolution diffs (top-level keys only)."""
    if not rows:
        return {}
    keys = set(rows[0].keys())
    inferred: dict[str, Any] = {}
    for k in sorted(keys):
        vals = [r.get(k) for r in rows[:200]]
        types = {type(v).__name__ for v in vals if v is not None}
        inferred[k] = {"types": sorted(types)}
    return inferred


def run_single_source(source: Path, out_dir: Path, root: Path) -> None:
    source = source.resolve()
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    name = source.name.lower()
    rows = load_jsonl(source)

    if "extraction" in name or "week3" in str(source).replace("\\", "/"):
        contract = build_week3_contract(rows, root)
        write_yaml(out_dir / "week3_extractions.yaml", contract)
        _write_week3_dbt(out_dir)
        _write_schema_snapshot(contract["id"], contract.get("schema", {}), root)
        _write_schema_snapshot(contract["id"], _infer_simple_schema_from_rows(rows), root)
    elif "event" in name or "week5" in str(source).replace("\\", "/"):
        contract = build_week5_contract(rows, root)
        write_yaml(out_dir / "week5_events.yaml", contract)
        _write_week5_dbt(out_dir)
        _write_event_payload_schema(root)
        _write_schema_snapshot(contract["id"], contract.get("schema", {}), root)
        _write_schema_snapshot(contract["id"], _infer_simple_schema_from_rows(rows), root)
    else:
        raise SystemExit(f"Unsupported --source {source}; use extractions or events JSONL.")

    print(f"ContractGenerator: wrote contracts to {out_dir}")


def run_all(root: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    w3 = load_jsonl(root / "outputs" / "week3" / "extractions.jsonl")
    write_yaml(out_dir / "week3_extractions.yaml", build_week3_contract(w3, root))
    _write_week3_dbt(out_dir)

    w5 = load_jsonl(root / "outputs" / "week5" / "events.jsonl")
    write_yaml(out_dir / "week5_events.yaml", build_week5_contract(w5, root))
    _write_week5_dbt(out_dir)
    _write_event_payload_schema(root)

    w4 = load_jsonl(root / "outputs" / "week4" / "lineage_snapshots.jsonl")
    write_yaml(out_dir / "week4_lineage.yaml", build_week4_contract(w4, root))

    tr = load_jsonl(root / "outputs" / "traces" / "runs.jsonl")
    write_yaml(out_dir / "langsmith_traces.yaml", build_langsmith_contract(tr, root))

    for cid, sch in [
        ("week3-document-refinery-extractions", build_week3_contract(w3, root).get("schema", {})),
        ("week5-event-sourcing-events", build_week5_contract(w5, root).get("schema", {})),
        ("week4-brownfield-lineage-snapshots", build_week4_contract(w4, root).get("schema", {})),
    ]:
        _write_schema_snapshot(cid, sch, root)
        evolved = json.loads(json.dumps(sch))
        evolved.setdefault("doc_id", sch.get("doc_id", {}))
        if cid == "week3-document-refinery-extractions":
            evolved["notes"] = {"type": "string", "required": False, "description": "Synthetic ADD for evolution demo"}
        elif cid == "week5-event-sourcing-events":
            evolved["payload"] = {"type": "integer", "required": True}
        else:
            evolved["tags"] = {"type": "array", "required": False}
        _write_schema_snapshot(cid, evolved, root)

    print(f"ContractGenerator: full bundle written to {out_dir}")


def main() -> None:
    parser = argparse.ArgumentParser(description="ContractGenerator (Week 7)")
    parser.add_argument("--source", type=Path, help="Path to a JSONL file (week3 extractions or week5 events).")
    parser.add_argument("--output", type=Path, default=Path("generated_contracts"), help="Output directory for YAML/dbt.")
    parser.add_argument("--all", action="store_true", help="Generate all standard contracts from outputs/.")
    args = parser.parse_args()
    root = repo_root()
    out = args.output
    if not args.source and not args.all:
        parser.error("Provide --source or --all")
    if args.all:
        run_all(root, out)
    else:
        run_single_source(args.source, out, root)


if __name__ == "__main__":
    main()
