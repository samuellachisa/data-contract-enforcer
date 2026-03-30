#!/usr/bin/env python3
"""Generate sample JSONL outputs for Weeks 1-5 + LangSmith-style traces (TRP Week 7)."""
from __future__ import annotations

import hashlib
import json
import random
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs"


def iso_z(dt: datetime) -> str:
    return dt.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> None:
    random.seed(42)
    for sub in [
        "week1",
        "week2",
        "week3",
        "week4",
        "week5",
        "traces",
    ]:
        (OUT / sub).mkdir(parents=True, exist_ok=True)

    base = datetime(2025, 1, 10, 12, 0, 0, tzinfo=timezone.utc)

    # Week 1 — intent_records
    intents = []
    for i in range(20):
        intents.append(
            {
                "intent_id": str(uuid.uuid4()),
                "description": f"Ensure auth flow validates session for scenario {i}",
                "code_refs": [
                    {
                        "file": f"src/week1/handlers/auth_{i % 5}.py",
                        "line_start": 10 + i,
                        "line_end": 25 + i,
                        "symbol": f"validate_session_{i % 3}",
                        "confidence": round(random.uniform(0.55, 0.99), 2),
                    }
                ],
                "governance_tags": random.sample(
                    ["auth", "pii", "billing", "audit"], k=random.randint(1, 3)
                ),
                "created_at": iso_z(base + timedelta(hours=i)),
            }
        )
    _write_jsonl(OUT / "week1" / "intent_records.jsonl", intents)

    # Week 2 — verdicts (some intentionally invalid for LLM schema metric)
    rubric_path = ROOT / "rubrics" / "sample_rubric.yaml"
    rubric_path.parent.mkdir(parents=True, exist_ok=True)
    rubric_path.write_text("version: 1.2.0\ncriteria:\n  quality: {}\n", encoding="utf-8")
    rubric_id = hashlib.sha256(rubric_path.read_bytes()).hexdigest()
    verdicts = []
    for i in range(25):
        scores = {
            "clarity": {
                "score": random.randint(1, 5),
                "evidence": [f"Excerpt {i} line 1"],
                "notes": "auto",
            },
            "correctness": {
                "score": random.randint(1, 5),
                "evidence": [f"Excerpt {i} line 2"],
                "notes": "auto",
            },
        }
        w = [scores["clarity"]["score"], scores["correctness"]["score"]]
        overall = round(sum(w) / len(w), 2)
        rec = {
            "verdict_id": str(uuid.uuid4()),
            "target_ref": f"src/week1/handlers/auth_{i % 5}.py",
            "rubric_id": rubric_id,
            "rubric_version": "1.2.0",
            "scores": scores,
            "overall_verdict": random.choice(["PASS", "FAIL", "WARN"]),
            "overall_score": overall,
            "confidence": round(random.uniform(0.6, 0.99), 2),
            "evaluated_at": iso_z(base + timedelta(minutes=i)),
        }
        # Inject invalid LLM output shape on a few rows (wrong score type)
        if i in (3, 7, 11):
            rec["scores"]["clarity"]["score"] = "three"  # type: ignore[assignment]
        verdicts.append(rec)
    _write_jsonl(OUT / "week2" / "verdicts.jsonl", verdicts)

    # Week 3 — extractions (55+); one record has entity ref violation for "real" violation
    ENTITY_TYPES = ["PERSON", "ORG", "LOCATION", "DATE", "AMOUNT", "OTHER"]
    extractions = []
    for i in range(55):
        doc_id = str(uuid.uuid4())
        e1 = str(uuid.uuid4())
        e2 = str(uuid.uuid4())
        entities = [
            {
                "entity_id": e1,
                "name": f"Entity A {i}",
                "type": random.choice(ENTITY_TYPES),
                "canonical_value": f"value-a-{i}",
            },
            {
                "entity_id": e2,
                "name": f"Entity B {i}",
                "type": random.choice(ENTITY_TYPES),
                "canonical_value": f"value-b-{i}",
            },
        ]
        fact_entity_refs = [e1, e2]
        if i == 19:
            fact_entity_refs.append("00000000-0000-4000-8000-000000000999")  # missing entity
        facts = [
            {
                "fact_id": str(uuid.uuid4()),
                "text": f"Document {i} states revenue grew in Q4 with confidence narrative.",
                "entity_refs": fact_entity_refs,
                "confidence": (51.3 if i == 20 else round(random.uniform(0.5, 0.98), 2)), 
                "page_ref": random.choice([None, 1, 2, 3, 4]),
                "source_excerpt": f"verbatim chunk {i} from page",
            }
        ]
        extractions.append(
            {
                "doc_id": doc_id,
                "source_path": f"https://example.com/docs/doc_{i}.pdf",
                "source_hash": "b" * 64,
                "extracted_facts": facts,
                "entities": entities,
                "extraction_model": "claude-3-5-sonnet-20241022",
                "processing_time_ms": random.randint(200, 5000),
                "token_count": {"input": 4000 + i, "output": 800 + i},
                "extracted_at": iso_z(base + timedelta(seconds=i * 30)),
            }
        )
    _write_jsonl(OUT / "week3" / "extractions.jsonl", extractions)

    # Week 4 — lineage linking extractor -> cartographer
    snap_nodes = [
        {
            "node_id": "file::src/week3/extractor.py",
            "type": "FILE",
            "label": "extractor.py",
            "metadata": {
                "path": "src/week3/extractor.py",
                "language": "python",
                "purpose": "Extracts facts and entities from documents",
                "last_modified": iso_z(base),
            },
        },
        {
            "node_id": "file::src/week4/cartographer.py",
            "type": "FILE",
            "label": "cartographer.py",
            "metadata": {
                "path": "src/week4/cartographer.py",
                "language": "python",
                "purpose": "Builds lineage snapshots from pipeline outputs",
                "last_modified": iso_z(base + timedelta(days=1)),
            },
        },
        {
            "node_id": "pipeline::week3-document-refinery",
            "type": "PIPELINE",
            "label": "week3-document-refinery",
            "metadata": {"path": "outputs/week3/extractions.jsonl"},
        },
        {
            "node_id": "pipeline::week4-lineage-generation",
            "type": "PIPELINE",
            "label": "week4-lineage-generation",
            "metadata": {"path": "outputs/week4/lineage_snapshots.jsonl"},
        },
    ]
    snap_edges = [
        {
            "source": "file::src/week3/extractor.py",
            "target": "pipeline::week3-document-refinery",
            "relationship": "PRODUCES",
            "confidence": 0.95,
        },
        {
            "source": "pipeline::week3-document-refinery",
            "target": "file::src/week4/cartographer.py",
            "relationship": "CONSUMES",
            "confidence": 0.9,
        },
        {
            "source": "file::src/week4/cartographer.py",
            "target": "pipeline::week4-lineage-generation",
            "relationship": "PRODUCES",
            "confidence": 0.92,
        },
    ]
    lineage_snapshot = {
        "snapshot_id": str(uuid.uuid4()),
        "codebase_root": str(ROOT.resolve()).replace("\\", "/"),
        "git_commit": "0" * 40,
        "nodes": snap_nodes,
        "edges": snap_edges,
        "captured_at": iso_z(base + timedelta(days=2)),
    }
    _write_jsonl(OUT / "week4" / "lineage_snapshots.jsonl", [lineage_snapshot])

    # Week 5 — events (55+), monotonic sequence per aggregate
    events = []
    agg = str(uuid.uuid4())
    for seq in range(55):
        events.append(
            {
                "event_id": str(uuid.uuid4()),
                "event_type": "DocumentProcessed",
                "aggregate_id": agg,
                "aggregate_type": "Document",
                "sequence_number": seq,
                "payload": {
                    "doc_id": str(uuid.uuid4()),
                    "status": random.choice(["done", "failed"]),
                    "bytes": random.randint(1000, 99999),
                },
                "metadata": {
                    "causation_id": str(uuid.uuid4()) if seq % 2 == 0 else None,
                    "correlation_id": str(uuid.uuid4()),
                    "user_id": f"user_{seq % 10}",
                    "source_service": "week3-document-refinery",
                },
                "schema_version": "1.0",
                "occurred_at": iso_z(base + timedelta(seconds=seq)),
                "recorded_at": iso_z(base + timedelta(seconds=seq, milliseconds=500)),
            }
        )
    _write_jsonl(OUT / "week5" / "events.jsonl", events)

    # LangSmith-style traces
    traces = []
    for i in range(30):
        st = base + timedelta(minutes=i)
        et = st + timedelta(seconds=2)
        pt, ct = 4200 + i, 890 + i
        traces.append(
            {
                "id": str(uuid.uuid4()),
                "name": f"extraction_chain_{i}",
                "run_type": random.choice(
                    ["llm", "chain", "tool", "retriever", "embedding"]
                ),
                "inputs": {"doc_id": str(uuid.uuid4())},
                "outputs": {"facts": 3},
                "error": None,
                "start_time": iso_z(st),
                "end_time": iso_z(et),
                "total_tokens": pt + ct,
                "prompt_tokens": pt,
                "completion_tokens": ct,
                "total_cost": round(random.uniform(0.001, 0.05), 4),
                "tags": ["week3", "extraction"],
                "parent_run_id": None,
                "session_id": str(uuid.uuid4()),
            }
        )
    _write_jsonl(OUT / "traces" / "runs.jsonl", traces)

    print(f"Wrote sample outputs under {OUT}")


def _write_jsonl(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
