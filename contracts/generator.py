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
    """
    Build a small, reviewer-friendly downstream list (no per-document duplicate blocks).
    Aggregates all table::doc:{uuid} nodes into one logical consumer entry with a count.
    """
    if not lineage:
        return []
    nodes = {n["node_id"]: n for n in lineage.get("nodes", [])}
    edges = lineage.get("edges", [])
    is_week3 = "week3" in dataset_hint or "week3-document-refinery" in dataset_hint
    is_week5 = "week5" in dataset_hint or "event" in dataset_hint

    if is_week3:
        fields = ["doc_id", "extracted_facts", "extraction_model"]
        breaking = ["extracted_facts.confidence", "doc_id"]
        pipe = "pipeline::week3-document-refinery"
        doc_table_count = 0
        seen_file: set[str] = set()
        seen_pipeline: set[str] = set()
        for e in edges:
            src, tgt = str(e.get("source", "")), str(e.get("target", ""))
            if pipe not in src and pipe not in tgt:
                continue
            if src == pipe:
                if tgt.startswith("table::doc:"):
                    doc_table_count += 1
                elif tgt.startswith("file::"):
                    seen_file.add(tgt)
                elif tgt.startswith("pipeline::"):
                    seen_pipeline.add(tgt)
        # Pipelines produced by downstream FILE consumers (e.g. cartographer → lineage generation)
        for e in edges:
            src, tgt = str(e.get("source", "")), str(e.get("target", ""))
            if src in seen_file and tgt.startswith("pipeline::"):
                seen_pipeline.add(tgt)
        out: list[dict[str, Any]] = []
        if doc_table_count > 0:
            out.append(
                {
                    "id": "week4-lineage-document-table-nodes",
                    "description": (
                        "Week 4 Cartographer materialises one TABLE lineage node per extracted document "
                        f"(`table::doc:{{uuid}}` pattern). Current snapshot: {doc_table_count} document node(s) "
                        "linking refinery output to downstream graph traversal and blast-radius analysis."
                    ),
                    "fields_consumed": fields,
                    "breaking_if_changed": breaking,
                    "lineage_doc_node_count": doc_table_count,
                }
            )
        for fid in sorted(seen_file):
            meta = nodes.get(fid, {}).get("metadata", {}) or {}
            label = str(meta.get("path", fid.replace("file::", "")))
            out.append(
                {
                    "id": label,
                    "description": f"Downstream file consumer `{fid}` (reads refinery / lineage context).",
                    "fields_consumed": fields,
                    "breaking_if_changed": breaking,
                }
            )
        for pid in sorted(seen_pipeline):
            out.append(
                {
                    "id": pid.replace("pipeline::", ""),
                    "description": f"Downstream pipeline `{pid}`.",
                    "fields_consumed": fields,
                    "breaking_if_changed": breaking,
                }
            )
        return out

    if is_week5:
        fields = ["event_type", "payload", "sequence_number", "aggregate_id"]
        breaking = ["payload", "event_type"]
        out_w5: list[dict[str, Any]] = []
        for e in edges:
            src, tgt = str(e.get("source", "")), str(e.get("target", ""))
            if "week5" not in src and "week5" not in tgt:
                continue
            tn = nodes.get(tgt, {})
            nid = str(tn.get("node_id", tgt))
            if nid.startswith("file::") or tn.get("type") == "FILE":
                label = str(tn.get("metadata", {}).get("path", tn.get("label", nid)))
                out_w5.append(
                    {
                        "id": label,
                        "description": f"Lineage edge {e.get('relationship')} from {src} to {tgt}",
                        "fields_consumed": fields,
                        "breaking_if_changed": breaking,
                    }
                )
        # Dedupe by id
        seen: set[str] = set()
        deduped: list[dict[str, Any]] = []
        for item in out_w5:
            k = str(item.get("id"))
            if k in seen:
                continue
            seen.add(k)
            deduped.append(item)
        return deduped

    return []


def _llm_annotations_stub(
    column: str,
    table: str,
    samples: list[Any],
    neighbors: list[str],
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    desc = (
        f"Column `{column}` in `{table}`; neighbors: {neighbors}. "
        "Treat as contract-critical: downstream validators and drift checks depend on stable semantics."
    )
    rule = (
        f"Keep `{column}` distribution consistent with baseline (detect >2σ drift as WARN, >3σ as FAIL). "
        "Breaking changes require migration impact report."
    )
    if profile and profile.get("max") is not None:
        rule += (
            f" Observed profile on this snapshot: min={profile.get('min')}, max={profile.get('max')}, "
            f"mean={profile.get('mean')}, p95={profile.get('p95')}."
        )
    return {
        "column": column,
        "table": table,
        "description": desc,
        "business_rule": rule,
        "cross_column_relationship": "Join keys and consumer fields are listed under `lineage.downstream` in the data contract.",
        "samples": [str(s) for s in samples[:5]],
        "statistical_context": (
            {k: profile[k] for k in ("min", "max", "mean", "p95", "stddev") if profile and k in profile} or None
        ),
    }


def _llm_annotate_openai(column: str, table: str, samples: list[Any], neighbors: list[str]) -> dict[str, Any] | None:
    try:
        from openai import OpenAI
    except Exception:
        return None
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None
    client = OpenAI(api_key=api_key)
    prompt = (
        f'Column "{column}" in dataset "{table}". Neighbor fields: {neighbors}. '
        f"Sample values: {samples[:5]!r}. "
        'Reply with ONLY compact JSON: {"description":"...","business_rule":"...","cross_column_relationship":"..."}'
    )
    try:
        r = client.chat.completions.create(
            model=os.environ.get("CONTRACT_LLM_MODEL", "gpt-4o-mini"),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=400,
        )
        text = (r.choices[0].message.content or "").strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:].lstrip()
        data = json.loads(text)
        return {
            "column": column,
            "table": table,
            "description": str(data.get("description", "")),
            "business_rule": str(data.get("business_rule", "")),
            "cross_column_relationship": str(data.get("cross_column_relationship", "")),
            "samples": [str(s) for s in samples[:5]],
            "provider": "openai",
        }
    except Exception:
        return None


def _llm_annotate_anthropic(column: str, table: str, samples: list[Any], neighbors: list[str]) -> dict[str, Any] | None:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        import anthropic
    except Exception:
        return None
    client = anthropic.Anthropic(api_key=api_key)
    prompt = (
        f'Column "{column}" in dataset "{table}". Neighbor fields: {neighbors}. '
        f"Sample values: {samples[:5]!r}. "
        'Reply with ONLY compact JSON: {"description":"...","business_rule":"...","cross_column_relationship":"..."}'
    )
    try:
        msg = client.messages.create(
            model=os.environ.get("ANTHROPIC_CONTRACT_MODEL", "claude-3-5-haiku-20241022"),
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        text = ""
        for b in msg.content:
            if b.type == "text":
                text += b.text
        text = text.strip()
        if text.startswith("```"):
            parts = text.split("```")
            text = parts[1] if len(parts) > 1 else text
            if text.startswith("json"):
                text = text[4:].lstrip()
        data = json.loads(text)
        return {
            "column": column,
            "table": table,
            "description": str(data.get("description", "")),
            "business_rule": str(data.get("business_rule", "")),
            "cross_column_relationship": str(data.get("cross_column_relationship", "")),
            "samples": [str(s) for s in samples[:5]],
            "provider": "anthropic",
        }
    except Exception:
        return None


def _maybe_llm_annotate(
    column: str,
    table: str,
    samples: list[Any],
    neighbors: list[str],
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if os.environ.get("CONTRACT_LLM_OFF", "").strip() in ("1", "true", "yes"):
        return _llm_annotations_stub(column, table, samples, neighbors, profile=profile)
    out = _llm_annotate_anthropic(column, table, samples, neighbors)
    if out:
        if profile:
            out["statistical_context"] = {k: profile[k] for k in ("min", "max", "mean", "p95") if k in profile}
        return out
    out = _llm_annotate_openai(column, table, samples, neighbors)
    if out:
        if profile:
            out["statistical_context"] = {k: profile[k] for k in ("min", "max", "mean", "p95") if k in profile}
        return out
    return _llm_annotations_stub(column, table, samples, neighbors, profile=profile)


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
        profile=conf_stats or None,
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
                "description": "Array of fact objects; items follow JSON Schema draft-07 object form.",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "fact_id",
                        "text",
                        "entity_refs",
                        "confidence",
                        "source_excerpt",
                    ],
                    "properties": {
                        "fact_id": {
                            "type": "string",
                            "format": "uuid",
                            "description": "Unique fact id within the extraction record.",
                            "unique": True,
                        },
                        "text": {"type": "string", "minLength": 1},
                        "entity_refs": {
                            "type": "array",
                            "items": {"type": "string", "format": "uuid"},
                        },
                        "confidence": {
                            "type": "number",
                            "minimum": 0.0,
                            "maximum": 1.0,
                            "description": "Model confidence; breaking if scaled to 0–100 integer.",
                        },
                        "page_ref": {
                            "oneOf": [{"type": "integer", "minimum": 1}, {"type": "null"}],
                            "description": "1-based page index when applicable; null otherwise.",
                        },
                        "source_excerpt": {"type": "string", "minLength": 1},
                    },
                },
            },
            "entities": {
                "type": "array",
                "required": True,
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["entity_id", "name", "type", "canonical_value"],
                    "properties": {
                        "entity_id": {"type": "string", "format": "uuid"},
                        "name": {"type": "string", "minLength": 1},
                        "type": {
                            "type": "string",
                            "enum": ["PERSON", "ORG", "LOCATION", "DATE", "AMOUNT", "OTHER"],
                        },
                        "canonical_value": {"type": "string", "minLength": 1},
                    },
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
                "description": "Non-negative input/output token counts; both keys required.",
                "additionalProperties": False,
                "properties": {
                    "input": {"type": "integer", "minimum": 0},
                    "output": {"type": "integer", "minimum": 0},
                },
            },
            "extracted_at": {
                "type": "string",
                "format": "date-time",
                "required": True,
                "description": "Extraction completion timestamp (RFC 3339 / ISO 8601 date-time).",
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
                    "missing_count(extracted_facts[*].fact_id) = 0",
                    "duplicate_count(extracted_facts[*].fact_id) = 0",
                    "min(extracted_facts[*].confidence) >= 0.0",
                    "max(extracted_facts[*].confidence) <= 1.0",
                    "min(processing_time_ms) >= 1",
                    "row_count >= 1",
                    "max(cardinality(extracted_facts[*].entity_refs)) >= 0",
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
    """
    dbt schema mirroring week3-document-refinery-extractions: parent + exploded child models
    with relationships, accepted_values, and singular SQL companions under generated_contracts/dbt_tests/.
    """
    rel_doc = {"relationships": {"to": "ref('extractions')", "field": "doc_id"}}
    entity_enum = {"accepted_values": {"values": ["PERSON", "ORG", "LOCATION", "DATE", "AMOUNT", "OTHER"]}}
    yml: dict[str, Any] = {
        "version": 2,
        "models": [
            {
                "name": "extractions",
                "description": (
                    "Parent table: one row per document extraction. Aligns with data contract "
                    "`week3-document-refinery-extractions` (Bitol v3)."
                ),
                "columns": [
                    {"name": "doc_id", "description": "Primary key UUIDv4.", "tests": ["not_null", "unique"]},
                    {"name": "source_path", "description": "Absolute path or https URL.", "tests": ["not_null"]},
                    {"name": "source_hash", "description": "SHA-256 hex (64 chars).", "tests": ["not_null"]},
                    {"name": "extraction_model", "description": "Must start with claude- or gpt-.", "tests": ["not_null"]},
                    {"name": "processing_time_ms", "description": "Positive integer.", "tests": ["not_null"]},
                    {"name": "extracted_at", "description": "RFC 3339 date-time.", "tests": ["not_null"]},
                    {"name": "token_count", "description": "Object with input/output token counts.", "tests": ["not_null"]},
                ],
            },
            {
                "name": "extraction_facts",
                "description": (
                    "Exploded `extracted_facts[]` — one row per fact. FK to extractions.doc_id. "
                    "Confidence must remain float 0.0–1.0 per contract (see singular test)."
                ),
                "columns": [
                    {"name": "doc_id", "tests": ["not_null", rel_doc]},
                    {"name": "fact_id", "tests": ["not_null", "unique"]},
                    {"name": "confidence", "tests": ["not_null"]},
                    {"name": "text", "tests": ["not_null"]},
                    {"name": "source_excerpt", "tests": ["not_null"]},
                ],
            },
            {
                "name": "extraction_entities",
                "description": "Exploded `entities[]` — FK to extractions.doc_id; entity_type enum per contract.",
                "columns": [
                    {"name": "doc_id", "tests": ["not_null", rel_doc]},
                    {"name": "entity_id", "tests": ["not_null", "unique"]},
                    {"name": "entity_type", "tests": ["not_null", entity_enum]},
                    {"name": "name", "tests": ["not_null"]},
                    {"name": "canonical_value", "tests": ["not_null"]},
                ],
            },
            {
                "name": "extraction_fact_entity_refs",
                "description": (
                    "Bridge: each entity_ref on a fact must resolve to extraction_entities.entity_id "
                    "for the same doc_id (dbt relationships test)."
                ),
                "columns": [
                    {"name": "doc_id", "tests": ["not_null", rel_doc]},
                    {"name": "fact_id", "tests": ["not_null", {"relationships": {"to": "ref('extraction_facts')", "field": "fact_id"}}]},
                    {
                        "name": "entity_id",
                        "tests": [
                            "not_null",
                            {"relationships": {"to": "ref('extraction_entities')", "field": "entity_id"}},
                        ],
                    },
                ],
            },
        ],
    }
    path = out_dir / "week3_extractions_dbt.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.dump(yml, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    tests_dir = out_dir / "dbt_tests" / "singular"
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / "week3_extraction_facts_confidence_0_1.sql").write_text(
        "-- Fails rows where per-fact confidence is outside [0,1] (contract clause extracted_facts.confidence).\n"
        "select *\nfrom {{ ref('extraction_facts') }}\n"
        "where confidence is null or confidence < 0 or confidence > 1\n",
        encoding="utf-8",
    )


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
                "description": "Required on each event; inner keys follow JSON Schema object form.",
                "additionalProperties": False,
                "required": ["correlation_id", "user_id", "source_service"],
                "properties": {
                    "causation_id": {
                        "oneOf": [{"type": "string", "format": "uuid"}, {"type": "null"}],
                        "description": "Prior event UUID when applicable; null otherwise.",
                    },
                    "correlation_id": {"type": "string", "format": "uuid"},
                    "user_id": {"type": "string", "minLength": 1},
                    "source_service": {"type": "string", "minLength": 1},
                },
            },
            "schema_version": {"type": "string", "required": True},
            "occurred_at": {"type": "string", "format": "date-time", "required": True},
            "recorded_at": {"type": "string", "format": "date-time", "required": True},
        },
        "quality": {
            "type": "SodaChecks",
            "specification": {
                "checks for events": [
                    "missing_count(event_id) = 0",
                    "missing_count(sequence_number) = 0",
                    "recorded_at >= occurred_at",
                    "payload.bytes >= 0",
                    "missing_count(metadata.correlation_id) = 0",
                    "min(sequence_number) >= 0",
                    "max(sequence_number) >= min(sequence_number)",
                    "min(recorded_at) >= min(occurred_at)",
                    "row_count >= 1",
                ]
            },
        },
        "lineage": {"upstream": [], "downstream": downstream},
        "llm_annotations": _maybe_llm_annotate(
            "payload.bytes",
            "events",
            [str(r.get("payload", {}).get("bytes")) for r in rows[:20]],
            ["event_type", "aggregate_id"],
            profile=_numeric_stats(
                [
                    float(r.get("payload", {}).get("bytes"))
                    for r in rows
                    if isinstance(r.get("payload"), dict)
                    and isinstance(r.get("payload", {}).get("bytes"), (int, float))
                ]
            )
            or None,
        ),
    }
    return contract


def _write_week5_dbt(out_dir: Path) -> None:
    payload_status = {"accepted_values": {"values": ["done", "failed"], "quote": True}}
    yml: dict[str, Any] = {
        "version": 2,
        "models": [
            {
                "name": "events",
                "description": (
                    "Append-only event log per aggregate. Aligns with `week5-event-sourcing-events`. "
                    "Payload validates per `event_schema_registry.json` / `event_payload_schemas/`."
                ),
                "columns": [
                    {"name": "event_id", "tests": ["not_null", "unique"]},
                    {"name": "event_type", "description": "PascalCase; registered types only.", "tests": ["not_null"]},
                    {"name": "aggregate_id", "tests": ["not_null"]},
                    {"name": "aggregate_type", "tests": ["not_null"]},
                    {"name": "sequence_number", "description": "Monotonic per aggregate_id.", "tests": ["not_null"]},
                    {"name": "payload", "tests": ["not_null"]},
                    {"name": "metadata", "tests": ["not_null"]},
                    {"name": "schema_version", "tests": ["not_null"]},
                    {"name": "occurred_at", "tests": ["not_null"]},
                    {"name": "recorded_at", "tests": ["not_null"]},
                ],
            },
            {
                "name": "event_document_processed_payload",
                "description": (
                    "Exploded payload for `DocumentProcessed` events — FK event_id to events. "
                    "Mirrors `DocumentProcessed.json` JSON Schema."
                ),
                "columns": [
                    {
                        "name": "event_id",
                        "tests": [
                            "not_null",
                            {"relationships": {"to": "ref('events')", "field": "event_id"}},
                        ],
                    },
                    {"name": "doc_id", "tests": ["not_null"]},
                    {"name": "status", "tests": ["not_null", payload_status]},
                    {"name": "bytes", "tests": ["not_null"]},
                ],
            },
            {
                "name": "event_metadata_exploded",
                "description": "Exploded `metadata` object; FK to events.event_id (unique per event row).",
                "columns": [
                    {
                        "name": "event_id",
                        "tests": [
                            "not_null",
                            {"relationships": {"to": "ref('events')", "field": "event_id"}},
                        ],
                    },
                    {"name": "correlation_id", "tests": ["not_null"]},
                    {"name": "user_id", "tests": ["not_null"]},
                    {"name": "source_service", "tests": ["not_null"]},
                ],
            },
        ],
    }
    path = out_dir / "week5_events_dbt.yml"
    with path.open("w", encoding="utf-8") as f:
        yaml.dump(yml, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    tests_dir = out_dir / "dbt_tests" / "singular"
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / "week5_events_recorded_gte_occurred.sql").write_text(
        "-- Temporal contract: recorded_at >= occurred_at\n"
        "select *\nfrom {{ ref('events') }}\n"
        "where recorded_at < occurred_at\n",
        encoding="utf-8",
    )
    (tests_dir / "week5_payload_bytes_non_negative.sql").write_text(
        "-- DocumentProcessed payload.bytes >= 0\n"
        "select *\nfrom {{ ref('event_document_processed_payload') }}\nwhere bytes < 0\n",
        encoding="utf-8",
    )


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
            "captured_at": {"type": "string", "format": "date-time", "required": True},
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


def build_week1_contract(rows: list[dict], root: Path) -> dict[str, Any]:
    # Structural contract for outputs/week1/intent_records.jsonl
    return {
        "kind": "DataContract",
        "apiVersion": "v3.0.0",
        "id": "week1-intent-code-correlator-intent-records",
        "info": {
            "title": "Week 1 Intent-Code Correlator — Intent Records",
            "version": "1.0.0",
            "owner": "week1-team",
            "description": "One record per inferred intent, including correlated code references and confidence.",
        },
        "servers": {
            "local": {
                "type": "local",
                "path": "outputs/week1/intent_records.jsonl",
                "format": "jsonl",
            }
        },
        "terms": {
            "usage": "Internal inter-system data contract. Do not publish.",
            "limitations": "intent_record.confidence must remain in 0.0–1.0.",
        },
        "schema": {
            "intent_id": {"type": "string", "format": "uuid", "required": True, "unique": True},
            "description": {"type": "string", "required": True, "minLength": 1},
            "code_refs": {
                "type": "array",
                "required": True,
                "minItems": 1,
                "items": {
                    "type": "object",
                    "required": ["file", "line_start", "line_end", "symbol", "confidence"],
                    "properties": {
                        "file": {"type": "string", "required": True, "minLength": 1},
                        "line_start": {"type": "integer", "required": True, "minimum": 1},
                        "line_end": {"type": "integer", "required": True, "minimum": 1},
                        "symbol": {"type": "string", "required": True, "minLength": 1},
                        "confidence": {"type": "number", "required": True, "minimum": 0.0, "maximum": 1.0},
                    },
                },
            },
            "governance_tags": {
                "type": "array",
                "required": True,
                "items": {"type": "string", "minLength": 1},
            },
            "created_at": {"type": "string", "required": True, "format": "date-time"},
        },
        "quality": {
            "type": "SodaChecks",
            "specification": {
                "checks for intents": [
                    "row_count >= 1",
                    "min(code_refs.confidence) >= 0.0",
                    "max(code_refs.confidence) <= 1.0",
                    "missing_count(intent_id) = 0",
                    "duplicate_count(intent_id) = 0",
                    "min(code_refs) >= 1",
                ]
            },
        },
        "lineage": {"upstream": [], "downstream": [{"id": "week2-digital-courtroom-verdicts", "description": "Digital Courtroom consumes intent target code references."}]},
    }


def build_week2_contract(rows: list[dict], root: Path) -> dict[str, Any]:
    return {
        "kind": "DataContract",
        "apiVersion": "v3.0.0",
        "id": "week2-digital-courtroom-verdicts",
        "info": {
            "title": "Week 2 Digital Courtroom — Verdict Records",
            "version": "1.0.0",
            "owner": "week2-team",
            "description": "Structured LLM verdicts with per-criterion scores and overall verdict.",
        },
        "servers": {
            "local": {
                "type": "local",
                "path": "outputs/week2/verdicts.jsonl",
                "format": "jsonl",
            }
        },
        "terms": {
            "usage": "Internal inter-system contract for structured verdicts.",
            "limitations": "overall_verdict must be PASS|FAIL|WARN and scores must be integers 1–5.",
        },
        "schema": {
            "verdict_id": {"type": "string", "format": "uuid", "required": True, "unique": True},
            "target_ref": {"type": "string", "required": True, "minLength": 1},
            "rubric_id": {"type": "string", "required": True, "minLength": 1},
            "rubric_version": {"type": "string", "required": True, "minLength": 1},
            "scores": {"type": "object", "required": True},
            "overall_verdict": {"type": "string", "required": True, "enum": ["PASS", "FAIL", "WARN"]},
            "overall_score": {"type": "number", "required": True},
            "confidence": {"type": "number", "required": True, "minimum": 0.0, "maximum": 1.0},
            "evaluated_at": {"type": "string", "required": True, "format": "date-time"},
        },
        "quality": {
            "type": "SodaChecks",
            "specification": {
                "checks for verdicts": [
                    "missing_count(verdict_id) = 0",
                    "duplicate_count(verdict_id) = 0",
                    "overall_verdict in {'PASS','FAIL','WARN'}",
                    "min(scores[*].score) >= 1",
                    "max(scores[*].score) <= 5",
                    "min(confidence) >= 0.0",
                    "max(confidence) <= 1.0",
                ]
            },
        },
        "lineage": {"upstream": [], "downstream": [{"id": "week7-ai-extensions", "description": "AI Contract Extension validates structured output schema for verdicts."}]},
    }


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
            "start_time": {"type": "string", "format": "date-time", "required": True},
            "end_time": {"type": "string", "format": "date-time", "required": True},
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
    w1 = load_jsonl(root / "outputs" / "week1" / "intent_records.jsonl")
    write_yaml(out_dir / "week1_intent_records.yaml", build_week1_contract(w1, root))

    w2 = load_jsonl(root / "outputs" / "week2" / "verdicts.jsonl")
    write_yaml(out_dir / "week2_verdicts.yaml", build_week2_contract(w2, root))

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
            # Synthetic breaking change for the analyzer: mutate the top-level type of extracted_facts.
            ef = evolved.get("extracted_facts", {})
            if isinstance(ef, dict):
                ef = dict(ef)
                ef["type"] = "object"
                evolved["extracted_facts"] = ef
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
