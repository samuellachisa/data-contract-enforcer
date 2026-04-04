#!/usr/bin/env python3
"""
ValidationRunner: executes contract clauses against JSONL; emits structured JSON report.
Usage: python contracts/runner.py --contract generated_contracts/week3_extractions.yaml --data outputs/week3/extractions.jsonl
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from contracts.common import CheckResult, load_jsonl, load_yaml, repo_root
from contracts.validation_checks import (
    run_cross_system_validation,
    validate_langsmith_runs,
    validate_week1_intents,
    validate_week2_verdicts,
    validate_week4_lineage,
    _apply_numeric_mean_drift,
)


UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.I,
)


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _jsonl_snapshot_id(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def validate_week3_extractions(rows: list[dict], contract: dict[str, Any], root: Path) -> list[CheckResult]:
    results: list[CheckResult] = []
    contract_id = contract.get("id", "unknown")

    def add(
        check_id: str,
        col: str,
        ctype: str,
        status: str,
        actual: str,
        expected: str,
        severity: str,
        n_fail: int,
        samples: list[str],
        msg: str,
    ) -> None:
        results.append(
            CheckResult(
                check_id=check_id,
                column_name=col,
                check_type=ctype,
                status=status,
                actual_value=actual,
                expected=expected,
                severity=severity,
                records_failing=n_fail,
                sample_failing=samples[:5],
                message=msg,
            )
        )

    # doc_id required + uuid
    missing_doc = sum(1 for r in rows if not r.get("doc_id"))
    if missing_doc:
        add(
            "week3.doc_id.required",
            "doc_id",
            "required",
            "FAIL",
            f"missing={missing_doc}",
            "non-null string",
            "CRITICAL",
            missing_doc,
            [],
            "doc_id is required.",
        )
    else:
        bad_uuid = [r.get("doc_id") for r in rows if not UUID_RE.match(str(r.get("doc_id", "")))]
        st = "FAIL" if bad_uuid else "PASS"
        add(
            "week3.doc_id.uuid",
            "doc_id",
            "format",
            st,
            f"invalid_count={len(bad_uuid)}",
            "uuid v4 pattern",
            "CRITICAL" if bad_uuid else "LOW",
            len(bad_uuid),
            [str(x) for x in bad_uuid[:5]],
            "doc_id must match UUID pattern.",
        )

    dup_docs: dict[str, int] = {}
    for r in rows:
        d = str(r.get("doc_id", ""))
        dup_docs[d] = dup_docs.get(d, 0) + 1
    dup_ids = [k for k, v in dup_docs.items() if v > 1]
    add(
        "week3.doc_id.unique",
        "doc_id",
        "unique",
        "FAIL" if dup_ids else "PASS",
        f"duplicates={len(dup_ids)}",
        "unique",
        "CRITICAL" if dup_ids else "LOW",
        len(dup_ids),
        dup_ids[:5],
        "doc_id must be unique per row.",
    )

    # source_hash sha256
    bad_hash = [r.get("doc_id") for r in rows if not re.match(r"^[a-f0-9]{64}$", str(r.get("source_hash", "")))]
    add(
        "week3.source_hash.pattern",
        "source_hash",
        "pattern",
        "FAIL" if bad_hash else "PASS",
        f"failing_rows={len(bad_hash)}",
        "^[a-f0-9]{64}$",
        "CRITICAL" if bad_hash else "LOW",
        len(bad_hash),
        [str(x) for x in bad_hash[:5]],
        "source_hash must be 64 hex chars.",
    )

    empty_facts = [r.get("doc_id") for r in rows if not r.get("extracted_facts")]
    add(
        "week3.extracted_facts.non_empty",
        "extracted_facts",
        "array",
        "FAIL" if empty_facts else "PASS",
        f"empty={len(empty_facts)}",
        "minItems>=1",
        "CRITICAL" if empty_facts else "LOW",
        len(empty_facts),
        [str(x) for x in empty_facts[:5]],
        "extracted_facts must be non-empty.",
    )

    conf_fail_ids: list[str] = []
    conf_values: list[float] = []
    for r in rows:
        for f in r.get("extracted_facts") or []:
            c = f.get("confidence")
            fid = str(f.get("fact_id", ""))
            if not isinstance(c, (int, float)):
                conf_fail_ids.append(fid or "unknown")
            else:
                cv = float(c)
                conf_values.append(cv)
                if cv < 0.0 or cv > 1.0:
                    conf_fail_ids.append(fid)
    st_conf = "FAIL" if conf_fail_ids else "PASS"
    mean_c = float(np.mean(conf_values)) if conf_values else 0.0
    max_c = float(np.max(conf_values)) if conf_values else 0.0
    min_c = float(np.min(conf_values)) if conf_values else 0.0
    add(
        "week3.extracted_facts.confidence.range",
        "extracted_facts[*].confidence",
        "range",
        st_conf,
        f"max={max_c:.3f}, mean={mean_c:.3f}, min={min_c:.3f}",
        "max<=1.0, min>=0.0",
        "CRITICAL" if conf_fail_ids else "LOW",
        len(conf_fail_ids),
        conf_fail_ids[:5],
        "confidence is float 0.0–1.0; breaking change if scaled to 0–100.",
    )

    # entity_refs exist in same record entities[]
    ent_fail: list[str] = []
    for r in rows:
        eids = {e.get("entity_id") for e in r.get("entities") or []}
        for f in r.get("extracted_facts") or []:
            for ref in f.get("entity_refs") or []:
                if ref not in eids:
                    ent_fail.append(str(f.get("fact_id", "")))
    add(
        "week3.extracted_facts.entity_refs.relationship",
        "extracted_facts[*].entity_refs",
        "relationship",
        "FAIL" if ent_fail else "PASS",
        f"failing_facts={len(ent_fail)}",
        "all refs in entities[].entity_id",
        "CRITICAL" if ent_fail else "LOW",
        len(ent_fail),
        ent_fail[:5],
        "entity_refs must reference entity_id present in entities[].",
    )

    allowed_types = {"PERSON", "ORG", "LOCATION", "DATE", "AMOUNT", "OTHER"}
    bad_types: list[str] = []
    for r in rows:
        for e in r.get("entities") or []:
            if str(e.get("type", "")) not in allowed_types:
                bad_types.append(str(e.get("entity_id", "")))
    add(
        "week3.entities.type.enum",
        "entities[*].type",
        "enum",
        "FAIL" if bad_types else "PASS",
        f"invalid={len(bad_types)}",
        str(allowed_types),
        "CRITICAL" if bad_types else "LOW",
        len(bad_types),
        bad_types[:5],
        "entity.type must be one of six enum values.",
    )

    bad_proc = [r.get("doc_id") for r in rows if not isinstance(r.get("processing_time_ms"), int) or r.get("processing_time_ms") <= 0]
    add(
        "week3.processing_time_ms.positive",
        "processing_time_ms",
        "range",
        "FAIL" if bad_proc else "PASS",
        f"failing={len(bad_proc)}",
        "integer > 0",
        "CRITICAL" if bad_proc else "LOW",
        len(bad_proc),
        [str(x) for x in bad_proc[:5]],
        "processing_time_ms must be positive int.",
    )

    bad_model = [r.get("doc_id") for r in rows if not re.match(r"^(claude|gpt)-", str(r.get("extraction_model", "")))]
    add(
        "week3.extraction_model.pattern",
        "extraction_model",
        "pattern",
        "FAIL" if bad_model else "PASS",
        f"failing={len(bad_model)}",
        "^(claude|gpt)-",
        "CRITICAL" if bad_model else "LOW",
        len(bad_model),
        [str(x) for x in bad_model[:5]],
        "extraction_model must start with claude- or gpt-.",
    )

    _apply_numeric_mean_drift(
        root,
        contract_id,
        "extracted_facts.confidence.mean",
        conf_values,
        add,
        "week3.extracted_facts.confidence.drift_baseline",
        "week3.extracted_facts.confidence.statistical_drift",
        "extracted_facts[*].confidence",
    )

    proc_vals = [float(r.get("processing_time_ms")) for r in rows if isinstance(r.get("processing_time_ms"), int)]
    _apply_numeric_mean_drift(
        root,
        contract_id,
        "processing_time_ms.mean",
        proc_vals,
        add,
        "week3.processing_time_ms.drift_baseline",
        "week3.processing_time_ms.statistical_drift",
        "processing_time_ms",
    )

    return results


def validate_week5_events(rows: list[dict], contract: dict[str, Any], root: Path) -> list[CheckResult]:
    results: list[CheckResult] = []
    contract_id = contract.get("id", "unknown")

    def add(
        check_id: str,
        col: str,
        ctype: str,
        status: str,
        actual: str,
        expected: str,
        severity: str,
        n_fail: int,
        samples: list[str],
        msg: str,
    ) -> None:
        results.append(
            CheckResult(
                check_id=check_id,
                column_name=col,
                check_type=ctype,
                status=status,
                actual_value=actual,
                expected=expected,
                severity=severity,
                records_failing=n_fail,
                sample_failing=samples[:5],
                message=msg,
            )
        )

    pascal = re.compile(r"^[A-Z][a-zA-Z0-9]*$")
    bad_et = [r.get("event_id") for r in rows if not pascal.match(str(r.get("event_type", "")))]
    add(
        "week5.event_type.pascal",
        "event_type",
        "pattern",
        "FAIL" if bad_et else "PASS",
        f"count={len(bad_et)}",
        "PascalCase",
        "CRITICAL" if bad_et else "LOW",
        len(bad_et),
        [str(x) for x in bad_et[:5]],
        "event_type must be PascalCase.",
    )

    # sequence monotonic per aggregate
    by_agg: dict[str, list[tuple[int, str]]] = {}
    for r in rows:
        aid = str(r.get("aggregate_id", ""))
        seq = r.get("sequence_number")
        eid = str(r.get("event_id", ""))
        if isinstance(seq, int):
            by_agg.setdefault(aid, []).append((seq, eid))
    seq_fail = 0
    samples: list[str] = []
    for aid, lst in by_agg.items():
        lst.sort(key=lambda x: x[0])
        for prev, cur in zip(lst, lst[1:]):
            if cur[0] != prev[0] + 1:
                seq_fail += 1
                samples.append(cur[1])
        seen = set()
        for s, eid in lst:
            if s in seen:
                seq_fail += 1
                samples.append(eid)
            seen.add(s)
    add(
        "week5.sequence_number.monotonic",
        "sequence_number",
        "ordering",
        "FAIL" if seq_fail else "PASS",
        f"violations={seq_fail}",
        "strict +1 per aggregate, no dupes",
        "CRITICAL" if seq_fail else "LOW",
        seq_fail,
        samples[:5],
        "sequence_number monotonic with no gaps/duplicates per aggregate_id.",
    )

    time_fail = 0
    bad_eids: list[str] = []
    for r in rows:
        try:
            o = datetime.fromisoformat(str(r.get("occurred_at", "")).replace("Z", "+00:00"))
            rec = datetime.fromisoformat(str(r.get("recorded_at", "")).replace("Z", "+00:00"))
            if rec < o:
                time_fail += 1
                bad_eids.append(str(r.get("event_id", "")))
        except Exception:
            time_fail += 1
            bad_eids.append(str(r.get("event_id", "")))
    add(
        "week5.recorded_at.order",
        "recorded_at",
        "temporal",
        "FAIL" if time_fail else "PASS",
        f"failing={time_fail}",
        "recorded_at >= occurred_at",
        "CRITICAL" if time_fail else "LOW",
        time_fail,
        bad_eids[:5],
        "recorded_at must be >= occurred_at.",
    )

    # payload JSON schema
    schema_path = root / "generated_contracts" / "event_payload_schemas" / "DocumentProcessed.json"
    payload_fail: list[str] = []
    if schema_path.exists():
        import jsonschema

        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        for r in rows:
            if str(r.get("event_type")) != "DocumentProcessed":
                continue
            try:
                jsonschema.validate(r.get("payload"), schema)
            except Exception:
                payload_fail.append(str(r.get("event_id", "")))
        add(
            "week5.payload.jsonschema",
            "payload",
            "jsonschema",
            "FAIL" if payload_fail else "PASS",
            f"failing={len(payload_fail)}",
            "DocumentProcessed.json",
            "CRITICAL" if payload_fail else "LOW",
            len(payload_fail),
            payload_fail[:5],
            "payload must validate for registered event types.",
        )
    else:
        add(
            "week5.payload.jsonschema",
            "payload",
            "jsonschema",
            "ERROR",
            "schema file missing",
            "generated_contracts/event_payload_schemas/DocumentProcessed.json",
            "LOW",
            0,
            [],
            "Run ContractGenerator to emit event payload schema.",
        )

    byte_vals: list[float] = []
    for r in rows:
        if str(r.get("event_type")) != "DocumentProcessed":
            continue
        pl = r.get("payload") or {}
        b = pl.get("bytes")
        if isinstance(b, (int, float)):
            byte_vals.append(float(b))
    if byte_vals:
        _apply_numeric_mean_drift(
            root,
            contract_id,
            "payload.bytes.mean",
            byte_vals,
            add,
            "week5.payload.bytes.drift_baseline",
            "week5.payload.bytes.statistical_drift",
            "payload.bytes",
        )

    return results


def validate_generic_missing_column(contract: dict[str, Any]) -> list[CheckResult]:
    """Emit ERROR rows for columns referenced in contract schema but not implemented for this runner."""
    return []


def run_validation(contract_path: Path, data_path: Path, output_path: Path | None, root: Path) -> dict[str, Any]:
    contract = load_yaml(contract_path)
    rows = load_jsonl(data_path)
    cid = contract.get("id", "unknown")
    snap = _jsonl_snapshot_id(data_path)

    if "week3" in cid or "extraction" in cid:
        checks = validate_week3_extractions(rows, contract, root)
    elif "week1" in cid and "intent" in cid:
        checks = validate_week1_intents(rows, contract, root)
    elif "week2" in cid and "verdict" in cid:
        checks = validate_week2_verdicts(rows, contract, root)
    elif "week4" in cid and "lineage" in cid:
        checks = validate_week4_lineage(rows, contract, root)
    elif "langsmith" in cid or "trace-runs" in cid:
        checks = validate_langsmith_runs(rows, contract, root)
    elif "week5" in cid or "event-sourcing" in cid:
        checks = validate_week5_events(rows, contract, root)
    else:
        checks = validate_generic_missing_column(contract)
        checks.append(
            CheckResult(
                check_id="runner.unsupported_contract",
                column_name="*",
                check_type="support",
                status="ERROR",
                actual_value=cid,
                expected="week1|week2|week3|week4|week5|langsmith",
                severity="LOW",
                records_failing=0,
                sample_failing=[],
                message="No validation logic for this contract id.",
            )
        )

    passed = sum(1 for c in checks if c.status == "PASS")
    failed = sum(1 for c in checks if c.status == "FAIL")
    warned = sum(1 for c in checks if c.status == "WARN")
    errored = sum(1 for c in checks if c.status == "ERROR")

    report = {
        "report_id": str(uuid.uuid4()),
        "contract_id": cid,
        "snapshot_id": snap,
        "run_timestamp": _iso_now(),
        "total_checks": len(checks),
        "passed": passed,
        "failed": failed,
        "warned": warned,
        "errored": errored,
        "results": [
            {
                "check_id": c.check_id,
                "column_name": c.column_name,
                "check_type": c.check_type,
                "status": c.status,
                "actual_value": c.actual_value,
                "expected": c.expected,
                "severity": c.severity,
                "records_failing": c.records_failing,
                "sample_failing": c.sample_failing,
                "message": c.message,
            }
            for c in checks
        ],
    }

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"Wrote {output_path}")
    return report


def pipeline_should_block(report: dict[str, Any], mode: str) -> bool:
    """
    AUDIT: never block. WARN: block on FAIL+CRITICAL. ENFORCE: block on FAIL+(CRITICAL|HIGH).
    """
    m = (mode or "AUDIT").strip().upper()
    if m == "AUDIT":
        return False
    for r in report.get("results", []):
        if r.get("status") != "FAIL":
            continue
        sev = str(r.get("severity", "")).upper()
        if m == "WARN" and sev == "CRITICAL":
            return True
        if m == "ENFORCE" and sev in {"CRITICAL", "HIGH"}:
            return True
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description="ValidationRunner (Week 7)")
    parser.add_argument("--contract", type=Path, default=None, help="generated_contracts/*.yaml")
    parser.add_argument("--data", type=Path, default=None, help="outputs/.../*.jsonl")
    parser.add_argument("--output", type=Path, help="validation_reports/....json")
    parser.add_argument(
        "--mode",
        choices=["AUDIT", "WARN", "ENFORCE"],
        default="AUDIT",
        help="AUDIT: always exit 0. WARN: exit 1 if any FAIL with CRITICAL. ENFORCE: exit 1 if FAIL with CRITICAL or HIGH.",
    )
    parser.add_argument(
        "--cross-dependencies",
        action="store_true",
        help="Run explicit Week1→Week2 and Week3→Week4 cross-system contract checks.",
    )
    args = parser.parse_args()
    root = repo_root()
    out = args.output
    if not out:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out = root / "validation_reports" / f"run_{ts}.json"

    if args.cross_dependencies:
        report = run_cross_system_validation(root)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"Wrote {out}")
        if pipeline_should_block(report, args.mode):
            print(f"Blocking (--mode={args.mode}): failing checks exceed policy.", file=sys.stderr)
            raise SystemExit(1)
        return

    if not args.contract or not args.data:
        parser.error("Provide --contract and --data, or use --cross-dependencies")

    report = run_validation(args.contract.resolve(), args.data.resolve(), out, root)
    if pipeline_should_block(report, args.mode):
        print(f"Blocking (--mode={args.mode}): failing checks exceed policy.", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
