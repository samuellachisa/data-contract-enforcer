#!/usr/bin/env python3
"""Generate sample JSONL outputs for Weeks 1-5 + LangSmith-style traces (TRP Week 7).

Week 3 rows match the Document Refinery extraction contract (id: week3-document-refinery-extractions).
`extraction_model` uses the TRP Week 3 canonical example (`claude-3-5-sonnet-20241022`) for seeded /
fallback rows; when `capture_refinery_snapshot` + `snapshot_to_contract_payload` succeed, that value
comes from the Week 3 repo export instead.

When the Week 3 repo is present and data/*.pdf exist, each PDF is processed with the real refinery
(profile_document + ExtractionRouter.route: fast_text / layout / vision per rubric) unless
SEED_WEEK3_REFINERY=0. One JSONL row per PDF under data/ (no cycling duplicates). Without PDFs, emits 55 synthetic rows. On refinery failure,
the seed falls back to pdf_plain_text. Set WEEK3_DOCUMENT_REFINERY_DATA_DIR to override the data folder.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs"

# TRP Week 3 extraction_record canonical example (challenge doc); contract expects claude-* or gpt-*.
WEEK3_EXTRACTION_MODEL = "claude-3-5-sonnet-20241022"


def iso_z(dt: datetime) -> str:
    return dt.replace(tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_week3_document_refinery_root() -> Path | None:
    """Absolute path to the Week 3 (Document Refinery) project directory, if available."""
    override = os.environ.get("WEEK3_DOCUMENT_REFINERY_ROOT", "").strip()
    if override:
        p = Path(override).expanduser().resolve()
        return p if p.is_dir() else None
    # Typical layouts: .../Week 7/<this repo> with refinery at .../Week 7/Week 3 (Document Refinery),
    # or .../tenx/Week 3 (Document Refinery) next to .../tenx/Week 7/...
    inner_week7 = ROOT.parent
    course_root = ROOT.parent.parent
    for base in (inner_week7, course_root):
        for name in ("Week 3 (Document Refinery)", "Week 3"):
            cand = (base / name).resolve()
            if cand.is_dir():
                return cand
    return None


def _week3_pdf_inventory(week3_root: Path | None) -> list[Path]:
    """PDF files under Week 3 Document Refinery data/ (or WEEK3_DOCUMENT_REFINERY_DATA_DIR), sorted by path."""
    if week3_root is None:
        return []
    override = os.environ.get("WEEK3_DOCUMENT_REFINERY_DATA_DIR", "").strip()
    if override:
        data_dir = Path(override).expanduser().resolve()
    else:
        data_dir = (week3_root / "data").resolve()
    if not data_dir.is_dir():
        return []
    pdfs = [
        p for p in data_dir.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf"
    ]
    return sorted(pdfs, key=lambda p: str(p).lower())


def _pdf_sha256_hex(path: Path, cache: dict[Path, str]) -> str:
    if path not in cache:
        cache[path] = hashlib.sha256(path.read_bytes()).hexdigest()
    return cache[path]


def _week3_source_and_hash(
    i: int,
    week3_root: Path | None,
    pdf_paths: list[Path],
    pdf_hash_cache: dict[Path, str],
) -> tuple[str, str]:
    """Absolute source_path (forward slashes) and 64-char hex source_hash for extraction row i."""
    if pdf_paths:
        p = pdf_paths[i % len(pdf_paths)]
        source_path = str(p.resolve()).replace("\\", "/")
        return source_path, _pdf_sha256_hex(p, pdf_hash_cache)

    rel = Path("documents") / "inbox" / "2025" / "01" / "batch-001" / f"doc_{i:04d}.pdf"
    if week3_root is not None:
        source_path = str((week3_root / rel).resolve()).replace("\\", "/")
    else:
        source_path = (
            f"/data/trp/week3-document-refinery/inbox/2025/01/batch-001/doc_{i:04d}.pdf"
        )
    source_hash = hashlib.sha256(
        f"week3-document-refinery|seed|{source_path}|v1".encode("utf-8")
    ).hexdigest()
    return source_path, source_hash


_CAP_PHRASE = re.compile(
    r"\b(?:[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,4}|[A-Z]{2,})\b"
)


def _ensure_week3_on_syspath(week3_root: Path) -> None:
    root = str(week3_root.resolve())
    if root not in sys.path:
        sys.path.insert(0, root)


def _week3_pdf_text_backend_available(week3_root: Path | None) -> bool:
    """True if pdfplumber + Week 3 ``src.utils.pdf_plain_text`` can be imported."""
    if week3_root is None:
        return False
    _ensure_week3_on_syspath(week3_root)
    try:
        import pdfplumber  # noqa: F401
    except ImportError:
        return False
    try:
        from src.utils.pdf_plain_text import extract_plain_text_for_seed  # noqa: F401
    except ImportError:
        return False
    return True


def _extract_pdf_plain_text(path: Path, week3_root: Path | None) -> tuple[str, int | None]:
    """Delegate to Week 3 Document Refinery (pdfplumber)."""
    if week3_root is None:
        return "", None
    _ensure_week3_on_syspath(week3_root)
    try:
        from src.utils.pdf_plain_text import extract_plain_text_for_seed
    except ImportError:
        return "", None
    try:
        return extract_plain_text_for_seed(path)
    except Exception:
        return "", None


def _guess_entity_type(name: str) -> str:
    lower = name.lower()
    # Titles like "CBE Annual Report 2012-13" contain years but are not DATE entities.
    if any(
        w in lower
        for w in ("report", "annual", "statement", "budget", "audit", "procurement")
    ):
        return "ORG"
    if re.search(r"[$€£]|ETB|Birr|USD|EUR", name, re.I):
        return "AMOUNT"
    if re.fullmatch(r"(19|20)\d{2}", name.strip()):
        return "DATE"
    if re.search(r"\b(19|20)\d{2}\b", name) and len(name.split()) <= 2:
        return "DATE"
    if len(name.split()) >= 2:
        return "ORG"
    return "OTHER"


def _capitalized_phrases(sample: str, limit: int = 8) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for m in _CAP_PHRASE.finditer(sample):
        w = m.group(0).strip()
        if len(w) < 3 or w in seen:
            continue
        seen.add(w)
        out.append(w)
        if len(out) >= limit:
            break
    return out


def _truncate_at_word(text: str, cap: int) -> str:
    text = text.strip()
    if len(text) <= cap:
        return text
    cut = text[:cap].rsplit(" ", 1)[0]
    return cut + "…" if cut else text[:cap] + "…"


def _facts_entities_from_pdf_text(
    filename: str,
    text: str,
    first_page: int | None,
) -> tuple[list[dict], list[dict], str]:
    """One fact + two entities grounded in extracted PDF text."""
    cap_fact = 520
    body = _truncate_at_word(text, cap_fact)
    excerpt = _truncate_at_word(text, 300)
    phrases = _capitalized_phrases(text[:6000])
    stem = Path(filename).stem.replace("_", " ").strip() or filename
    names: list[str] = []
    if stem:
        names.append(stem[:120])
    for ph in phrases:
        if ph not in names:
            names.append(ph[:120])
        if len(names) >= 4:
            break
    while len(names) < 2:
        names.append(f"Supporting mention {len(names)}")

    entities: list[dict] = []
    for nm in names[:2]:
        eid = str(uuid.uuid4())
        entities.append(
            {
                "entity_id": eid,
                "name": nm[:200],
                "type": _guess_entity_type(nm),
                "canonical_value": nm[:200].casefold(),
            }
        )
    refs = [entities[0]["entity_id"], entities[1]["entity_id"]]
    conf = round(0.74 + min(0.24, len(text) / 120_000), 2)
    fact = {
        "fact_id": str(uuid.uuid4()),
        "text": f"{filename}: {body}",
        "entity_refs": refs,
        "confidence": min(0.98, conf),
        "page_ref": first_page,
        "source_excerpt": excerpt,
    }
    return [fact], entities, WEEK3_EXTRACTION_MODEL


def _facts_entities_pdf_unreadable(filename: str) -> tuple[list[dict], list[dict], str]:
    """Honest placeholder when no plain text is available."""
    stem = Path(filename).stem.strip() or filename
    e1, e2 = str(uuid.uuid4()), str(uuid.uuid4())
    entities = [
        {
            "entity_id": e1,
            "name": stem[:120],
            "type": "OTHER",
            "canonical_value": stem[:200].casefold(),
        },
        {
            "entity_id": e2,
            "name": "Week 3 Document Refinery",
            "type": "ORG",
            "canonical_value": "week 3 document refinery",
        },
    ]
    fact = {
        "fact_id": str(uuid.uuid4()),
        "text": (
            f"{filename}: no substantial plain text extracted "
            f"(empty, encrypted, or image-only PDF)."
        ),
        "entity_refs": [e1, e2],
        "confidence": 0.42,
        "page_ref": None,
        "source_excerpt": f"file={filename}",
    }
    return [fact], entities, WEEK3_EXTRACTION_MODEL


def _facts_entities_fully_synthetic(i: int, entity_types: list[str]) -> tuple[list[dict], list[dict], str]:
    """Original template when no PDF is tied to the row."""
    e1 = str(uuid.uuid4())
    e2 = str(uuid.uuid4())
    entities = [
        {
            "entity_id": e1,
            "name": f"Entity A {i}",
            "type": random.choice(entity_types),
            "canonical_value": f"value-a-{i}",
        },
        {
            "entity_id": e2,
            "name": f"Entity B {i}",
            "type": random.choice(entity_types),
            "canonical_value": f"value-b-{i}",
        },
    ]
    fact = {
        "fact_id": str(uuid.uuid4()),
        "text": (
            f"Week 3 Document Refinery — document {i}: "
            f"revenue grew in Q4 per extracted narrative."
        ),
        "entity_refs": [e1, e2],
        "confidence": round(random.uniform(0.5, 0.98), 2),
        "page_ref": random.choice([None, 1, 2, 3, 4]),
        "source_excerpt": f"verbatim chunk {i} from page",
    }
    return [fact], entities, WEEK3_EXTRACTION_MODEL


def main() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        pass
    else:
        load_dotenv(ROOT / ".env")
        load_dotenv(ROOT / ".env.local", override=True)

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

    # Week 2 — verdicts (integer 1–5 scores; use tests or create_violation + violated.json for demo failures)
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
        verdicts.append(rec)
    _write_jsonl(OUT / "week2" / "verdicts.jsonl", verdicts)

    # Week 3 — Document Refinery extractions: one row per PDF in data/, else 55 synthetic rows.
    week3_root = _resolve_week3_document_refinery_root()
    week3_pdf_text_ok = _week3_pdf_text_backend_available(week3_root)
    week3_pdf_paths = _week3_pdf_inventory(week3_root)
    pdf_hash_cache: dict[Path, str] = {}
    refinery_snapshots: dict[str, object | None] = {}
    use_refinery = os.environ.get("SEED_WEEK3_REFINERY", "1").lower() not in (
        "0",
        "false",
        "no",
    )
    ENTITY_TYPES = ["PERSON", "ORG", "LOCATION", "DATE", "AMOUNT", "OTHER"]
    extractions = []
    week3_row_iter: list[tuple[int, Path | None]]
    if week3_pdf_paths:
        week3_row_iter = list(enumerate(week3_pdf_paths))
    else:
        week3_row_iter = [(i, None) for i in range(55)]

    for i, pdf_path in week3_row_iter:
        doc_id = str(uuid.uuid4())
        if pdf_path is not None:
            key = str(pdf_path.resolve())
            used_refinery = False
            if use_refinery and week3_root is not None:
                if key not in refinery_snapshots:
                    _ensure_week3_on_syspath(week3_root)
                    try:
                        from src.utils.week7_contract_export import (
                            capture_refinery_snapshot,
                            snapshot_to_contract_payload,
                        )

                        refinery_snapshots[key] = capture_refinery_snapshot(
                            pdf_path, week3_root
                        )
                    except Exception:
                        refinery_snapshots[key] = None
                snap = refinery_snapshots.get(key)
                if snap is not None:
                    (
                        facts,
                        entities,
                        extraction_model,
                        processing_time_ms,
                        token_count,
                    ) = snapshot_to_contract_payload(snap, pdf_path.name)
                    source_path = snap.source_path
                    source_hash = snap.source_hash
                    used_refinery = True
            if not used_refinery:
                source_path, source_hash = _week3_source_and_hash(
                    i, week3_root, week3_pdf_paths, pdf_hash_cache
                )
                text, first_page = _extract_pdf_plain_text(pdf_path, week3_root)
                if len(text) >= 40:
                    facts, entities, extraction_model = _facts_entities_from_pdf_text(
                        pdf_path.name, text, first_page
                    )
                else:
                    facts, entities, extraction_model = _facts_entities_pdf_unreadable(
                        pdf_path.name
                    )
                try:
                    sz = pdf_path.stat().st_size
                except OSError:
                    sz = 0
                processing_time_ms = max(50, min(12_000, int(sz / 2500)))
                token_count = {
                    "input": max(120, min(120_000, len(text) // 3 + 400)),
                    "output": max(80, min(16_000, len(facts) * 140 + len(text) // 80)),
                }
        else:
            source_path, source_hash = _week3_source_and_hash(
                i, week3_root, week3_pdf_paths, pdf_hash_cache
            )
            facts, entities, extraction_model = _facts_entities_fully_synthetic(
                i, ENTITY_TYPES
            )
            processing_time_ms = random.randint(200, 5000)
            token_count = {"input": 4000 + i, "output": 800 + i}
        extractions.append(
            {
                "doc_id": doc_id,
                "source_path": source_path,
                "source_hash": source_hash,
                "extracted_facts": facts,
                "entities": entities,
                "extraction_model": extraction_model,
                "processing_time_ms": processing_time_ms,
                "token_count": token_count,
                "extracted_at": iso_z(base + timedelta(seconds=i * 30)),
            }
        )
    _write_jsonl(OUT / "week3" / "extractions.jsonl", extractions)

    # Week 4 — lineage linking extractor -> cartographer + one TABLE node per doc (cross-contract Week3→Week4)
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
    for ex in extractions:
        did = str(ex["doc_id"])
        nfacts = len(ex.get("extracted_facts") or [])
        snap_nodes.append(
            {
                "node_id": f"table::doc:{did}",
                "type": "TABLE",
                "label": f"doc:{did[:8]}…",
                "metadata": {
                    "path": did,
                    "purpose": (
                        f"Extracted document with {nfacts} fact(s) from Week 3 Document Refinery"
                    ),
                    "last_modified": ex.get("extracted_at", iso_z(base)),
                },
            }
        )
        snap_edges.append(
            {
                "source": "pipeline::week3-document-refinery",
                "target": f"table::doc:{did}",
                "relationship": "PRODUCES",
                "confidence": 0.88,
            }
        )
        snap_edges.append(
            {
                "source": f"table::doc:{did}",
                "target": "file::src/week4/cartographer.py",
                "relationship": "CONSUMES",
                "confidence": 0.85,
            }
        )
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
    if week3_pdf_paths:
        print(
            f"Week 3 source_path: {len(week3_pdf_paths)} PDF(s) from {week3_pdf_paths[0].parent}"
        )
        if use_refinery:
            ok = sum(1 for v in refinery_snapshots.values() if v is not None)
            bad = sum(1 for v in refinery_snapshots.values() if v is None)
            print(
                f"Week 3 refinery (ExtractionRouter): {ok} PDF(s) extracted, "
                f"{bad} PDF(s) fell back to pdf_plain_text (see Week 3 deps / API keys / docling)."
            )
        if not week3_pdf_text_ok:
            print(
                "Install Week 3 deps (see Week 3 pyproject.toml) so pdf_plain_text fallback can run."
            )
    elif week3_root is not None:
        data_dir = week3_root / "data"
        print(
            f"Week 3 source_path: no PDFs under {data_dir.resolve()} — "
            f"fabricated paths under {week3_root.resolve()}"
        )
    else:
        print(
            "Week 3 source_path: synthetic /data/trp/week3-document-refinery/... "
            "(set WEEK3_DOCUMENT_REFINERY_ROOT or add sibling folder 'Week 3 (Document Refinery)')."
        )


def _write_jsonl(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
