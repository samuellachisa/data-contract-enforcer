"""Additional ValidationRunner checks: Week 1/2/4, LangSmith traces, cross-system contracts."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np

from contracts.common import load_jsonl

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.I,
)

CheckAdd = Callable[..., None]


def _apply_numeric_mean_drift(
    root: Path,
    contract_id: str,
    baseline_key_suffix: str,
    values: list[float],
    add: CheckAdd,
    establish_id: str,
    drift_id: str,
    column_name: str,
) -> None:
    from contracts.common import load_baselines, save_baselines

    baselines = load_baselines(root)
    key = f"{contract_id}::{baseline_key_suffix}"
    m = float(np.mean(values)) if values else 0.0
    s = float(np.std(values)) if len(values) > 1 else 0.0
    if key not in baselines:
        baselines[key] = {"mean": m, "std": max(s, 1e-9)}
        save_baselines(root, baselines)
        add(
            establish_id,
            column_name,
            "drift",
            "PASS",
            f"baseline_established mean={m:.4f}",
            "first-run baseline",
            "LOW",
            0,
            [],
            f"Established statistical baseline for {baseline_key_suffix}.",
        )
        return
    bm = baselines[key]["mean"]
    bsd = max(float(baselines[key].get("std", 0.0)), 1e-9)
    dev = abs(m - bm) / bsd
    if dev > 3:
        drift_status, sev = "FAIL", "HIGH"
    elif dev > 2:
        drift_status, sev = "WARN", "MEDIUM"
    else:
        drift_status, sev = "PASS", "LOW"
    add(
        drift_id,
        column_name,
        "drift",
        drift_status,
        f"current_mean={m:.4f}, baseline_mean={bm:.4f}, dev_sigma={dev:.2f}",
        "WARN if >2σ, FAIL if >3σ",
        sev,
        0,
        [],
        "Statistical drift vs first-run baseline.",
    )


def validate_week1_intents(rows: list[dict], contract: dict[str, Any], root: Path) -> list[Any]:
    from contracts.common import CheckResult

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

    missing = sum(1 for r in rows if not r.get("intent_id"))
    add(
        "week1.intent_id.required",
        "intent_id",
        "required",
        "FAIL" if missing else "PASS",
        f"missing={missing}",
        "non-null",
        "CRITICAL" if missing else "LOW",
        missing,
        [],
        "intent_id required.",
    )
    bad_uuid = [r.get("intent_id") for r in rows if r.get("intent_id") and not UUID_RE.match(str(r.get("intent_id")))]
    add(
        "week1.intent_id.uuid",
        "intent_id",
        "format",
        "FAIL" if bad_uuid else "PASS",
        f"count={len(bad_uuid)}",
        "uuid v4",
        "CRITICAL" if bad_uuid else "LOW",
        len(bad_uuid),
        [str(x) for x in bad_uuid[:5]],
        "intent_id must be UUID v4 pattern.",
    )

    empty_refs: list[str] = []
    bad_conf: list[str] = []
    bad_lines: list[str] = []
    missing_files: list[str] = []
    for r in rows:
        iid = str(r.get("intent_id", ""))
        refs = r.get("code_refs") or []
        if not refs:
            empty_refs.append(iid)
        for cr in refs:
            c = cr.get("confidence")
            if not isinstance(c, (int, float)) or float(c) < 0.0 or float(c) > 1.0:
                bad_conf.append(iid)
            ls, le = cr.get("line_start"), cr.get("line_end")
            if not isinstance(ls, int) or not isinstance(le, int) or le < ls:
                bad_lines.append(iid)
            rel = str(cr.get("file", "")).replace("\\", "/")
            if rel and not (root / rel).is_file():
                missing_files.append(rel)
    add(
        "week1.code_refs.non_empty",
        "code_refs",
        "array",
        "FAIL" if empty_refs else "PASS",
        f"empty={len(empty_refs)}",
        "minItems>=1",
        "CRITICAL" if empty_refs else "LOW",
        len(empty_refs),
        empty_refs[:5],
        "code_refs must be non-empty per intent_record.",
    )
    add(
        "week1.code_refs.confidence.range",
        "code_refs[*].confidence",
        "range",
        "FAIL" if bad_conf else "PASS",
        f"invalid={len(bad_conf)}",
        "0.0–1.0",
        "CRITICAL" if bad_conf else "LOW",
        len(bad_conf),
        bad_conf[:5],
        "code_refs.confidence must be float 0.0–1.0.",
    )
    add(
        "week1.code_refs.line_order",
        "code_refs[*].line_end",
        "range",
        "FAIL" if bad_lines else "PASS",
        f"invalid={len(bad_lines)}",
        "line_end >= line_start",
        "CRITICAL" if bad_lines else "LOW",
        len(bad_lines),
        bad_lines[:5],
        "line_end must be >= line_start.",
    )
    add(
        "week1.code_refs.file.exists",
        "code_refs[*].file",
        "file_exists",
        "FAIL" if missing_files else "PASS",
        f"missing={len(missing_files)}",
        "path exists under repo root",
        "CRITICAL" if missing_files else "LOW",
        len(missing_files),
        missing_files[:5],
        "Each code_refs.file must exist relative to repository root.",
    )

    bad_gov: list[str] = []
    for r in rows:
        iid = str(r.get("intent_id", ""))
        tags = r.get("governance_tags")
        if not isinstance(tags, list) or len(tags) < 1:
            bad_gov.append(iid)
    add(
        "week1.governance_tags.non_empty",
        "governance_tags",
        "array",
        "FAIL" if bad_gov else "PASS",
        f"invalid={len(bad_gov)}",
        "minItems>=1",
        "CRITICAL" if bad_gov else "LOW",
        len(bad_gov),
        bad_gov[:5],
        "governance_tags must be a non-empty array of strings.",
    )

    bad_time: list[str] = []
    for r in rows:
        iid = str(r.get("intent_id", ""))
        try:
            datetime.fromisoformat(str(r.get("created_at", "")).replace("Z", "+00:00"))
        except Exception:
            bad_time.append(iid)
    add(
        "week1.created_at.iso8601",
        "created_at",
        "format",
        "FAIL" if bad_time else "PASS",
        f"invalid={len(bad_time)}",
        "ISO 8601",
        "CRITICAL" if bad_time else "LOW",
        len(bad_time),
        bad_time[:5],
        "created_at must be ISO 8601.",
    )

    conf_vals: list[float] = []
    for r in rows:
        for cr in r.get("code_refs") or []:
            c = cr.get("confidence")
            if isinstance(c, (int, float)):
                conf_vals.append(float(c))
    _apply_numeric_mean_drift(
        root,
        contract_id,
        "code_refs.confidence.mean",
        conf_vals,
        add,
        "week1.code_refs.confidence.drift_baseline",
        "week1.code_refs.confidence.statistical_drift",
        "code_refs[*].confidence",
    )

    return results


def validate_week2_verdicts(rows: list[dict], contract: dict[str, Any], root: Path) -> list[Any]:
    from contracts.common import CheckResult

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

    rubric_path = root / "rubrics" / "sample_rubric.yaml"
    expected_rubric_hash = ""
    if rubric_path.is_file():
        expected_rubric_hash = hashlib.sha256(rubric_path.read_bytes()).hexdigest()

    semver_re = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$")
    verdict_enum = {"PASS", "FAIL", "WARN"}
    bad_ver: list[str] = []
    bad_score: list[str] = []
    bad_overall: list[str] = []
    bad_rubric: list[str] = []
    bad_semver: list[str] = []
    bad_conf: list[str] = []
    conf_vals: list[float] = []

    for r in rows:
        vid = str(r.get("verdict_id", ""))
        if str(r.get("overall_verdict", "")) not in verdict_enum:
            bad_ver.append(vid)
        scores = r.get("scores") or {}
        ints: list[int] = []
        all_criteria_valid = isinstance(scores, dict) and len(scores) > 0
        if isinstance(scores, dict):
            for _k, v in scores.items():
                if not isinstance(v, dict):
                    all_criteria_valid = False
                    continue
                sc = v.get("score")
                if isinstance(sc, int) and 1 <= sc <= 5:
                    ints.append(sc)
                elif isinstance(sc, int):
                    bad_score.append(vid)
                    all_criteria_valid = False
                elif sc is not None:
                    bad_score.append(vid)
                    all_criteria_valid = False
                else:
                    all_criteria_valid = False
        if all_criteria_valid and ints and len(ints) == len(scores):
            exp = sum(ints) / len(ints)
            os_ = r.get("overall_score")
            if isinstance(os_, (int, float)) and abs(float(os_) - exp) > 0.05:
                bad_overall.append(vid)
        if expected_rubric_hash and str(r.get("rubric_id", "")) != expected_rubric_hash:
            bad_rubric.append(vid)
        if not semver_re.match(str(r.get("rubric_version", ""))):
            bad_semver.append(vid)
        c = r.get("confidence")
        if not isinstance(c, (int, float)) or float(c) < 0.0 or float(c) > 1.0:
            bad_conf.append(vid)
        elif isinstance(c, (int, float)):
            conf_vals.append(float(c))

    add(
        "week2.overall_verdict.enum",
        "overall_verdict",
        "enum",
        "FAIL" if bad_ver else "PASS",
        f"invalid={len(bad_ver)}",
        "PASS|FAIL|WARN",
        "CRITICAL" if bad_ver else "LOW",
        len(bad_ver),
        bad_ver[:5],
        "overall_verdict must be PASS, FAIL, or WARN.",
    )
    add(
        "week2.scores.criterion.range",
        "scores[*].score",
        "range",
        "FAIL" if bad_score else "PASS",
        f"invalid={len(bad_score)}",
        "integer 1–5",
        "CRITICAL" if bad_score else "LOW",
        len(bad_score),
        bad_score[:5],
        "Each criterion score must be integer 1–5.",
    )
    add(
        "week2.overall_score.weighted_mean",
        "overall_score",
        "computed",
        "FAIL" if bad_overall else "PASS",
        f"mismatch={len(bad_overall)}",
        "mean of integer criterion scores (equal weights)",
        "CRITICAL" if bad_overall else "LOW",
        len(bad_overall),
        bad_overall[:5],
        "overall_score must equal mean of valid integer scores (±0.05).",
    )
    if expected_rubric_hash:
        add(
            "week2.rubric_id.sha256",
            "rubric_id",
            "hash",
            "FAIL" if bad_rubric else "PASS",
            f"mismatch={len(bad_rubric)}",
            "sha256(rubrics/sample_rubric.yaml)",
            "CRITICAL" if bad_rubric else "LOW",
            len(bad_rubric),
            bad_rubric[:5],
            "rubric_id must match SHA-256 of rubrics/sample_rubric.yaml.",
        )
    else:
        add(
            "week2.rubric_id.sha256",
            "rubric_id",
            "hash",
            "ERROR",
            "rubric file missing",
            "rubrics/sample_rubric.yaml",
            "LOW",
            0,
            [],
            "Cannot verify rubric_id without rubrics/sample_rubric.yaml.",
        )
    add(
        "week2.rubric_version.semver",
        "rubric_version",
        "semver",
        "FAIL" if bad_semver else "PASS",
        f"invalid={len(bad_semver)}",
        "semver",
        "CRITICAL" if bad_semver else "LOW",
        len(bad_semver),
        bad_semver[:5],
        "rubric_version must look like semantic version x.y.z.",
    )
    add(
        "week2.confidence.range",
        "confidence",
        "range",
        "FAIL" if bad_conf else "PASS",
        f"invalid={len(bad_conf)}",
        "0.0–1.0",
        "CRITICAL" if bad_conf else "LOW",
        len(bad_conf),
        bad_conf[:5],
        "confidence must be 0.0–1.0.",
    )

    _apply_numeric_mean_drift(
        root,
        contract_id,
        "confidence.mean",
        conf_vals,
        add,
        "week2.confidence.drift_baseline",
        "week2.confidence.statistical_drift",
        "confidence",
    )

    return results


def validate_week4_lineage(rows: list[dict], contract: dict[str, Any], root: Path) -> list[Any]:
    from contracts.common import CheckResult

    results: list[CheckResult] = []
    _ = contract

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

    if not rows:
        add(
            "week4.snapshot.non_empty",
            "lineage_snapshots",
            "required",
            "FAIL",
            "0 rows",
            ">=1 snapshot",
            "CRITICAL",
            1,
            [],
            "lineage_snapshots.jsonl must contain at least one snapshot.",
        )
        return results

    snap = rows[-1]
    sid = str(snap.get("snapshot_id", ""))

    if not UUID_RE.match(sid):
        add(
            "week4.snapshot_id.uuid",
            "snapshot_id",
            "format",
            "FAIL",
            "invalid",
            "uuid v4",
            "CRITICAL",
            1,
            [sid],
            "snapshot_id must be UUID v4 pattern.",
        )
    else:
        add(
            "week4.snapshot_id.uuid",
            "snapshot_id",
            "format",
            "PASS",
            "ok",
            "uuid v4",
            "LOW",
            0,
            [],
            "snapshot_id format OK.",
        )

    gc = str(snap.get("git_commit", ""))
    add(
        "week4.git_commit.hex40",
        "git_commit",
        "pattern",
        "FAIL" if not re.match(r"^[a-f0-9]{40}$", gc) else "PASS",
        gc[:16] + "…",
        "40 lowercase hex",
        "CRITICAL" if not re.match(r"^[a-f0-9]{40}$", gc) else "LOW",
        0 if re.match(r"^[a-f0-9]{40}$", gc) else 1,
        [],
        "git_commit must be exactly 40 hex chars.",
    )

    nodes = snap.get("nodes") or []
    node_ids = {str(n.get("node_id")) for n in nodes if n.get("node_id")}
    allowed_node_types = {"FILE", "TABLE", "SERVICE", "MODEL", "PIPELINE", "EXTERNAL"}
    bad_nt: list[str] = []
    for n in nodes:
        if str(n.get("type", "")) not in allowed_node_types:
            bad_nt.append(str(n.get("node_id", "")))
    add(
        "week4.nodes.type.enum",
        "nodes[*].type",
        "enum",
        "FAIL" if bad_nt else "PASS",
        f"invalid={len(bad_nt)}",
        str(allowed_node_types),
        "CRITICAL" if bad_nt else "LOW",
        len(bad_nt),
        bad_nt[:5],
        "node.type must be one of FILE|TABLE|SERVICE|MODEL|PIPELINE|EXTERNAL.",
    )

    allowed_rel = {"IMPORTS", "CALLS", "READS", "WRITES", "PRODUCES", "CONSUMES"}
    bad_ep: list[str] = []
    bad_rel: list[str] = []
    bad_conf_e: list[str] = []
    for e in snap.get("edges") or []:
        s, t = e.get("source"), e.get("target")
        if str(s) not in node_ids or str(t) not in node_ids:
            bad_ep.append(f"{s}->{t}")
        if str(e.get("relationship", "")) not in allowed_rel:
            bad_rel.append(str(e.get("relationship", "")))
        cf = e.get("confidence")
        if not isinstance(cf, (int, float)) or float(cf) < 0.0 or float(cf) > 1.0:
            bad_conf_e.append(str(s))
    add(
        "week4.edges.endpoints",
        "edges[*].source",
        "reference",
        "FAIL" if bad_ep else "PASS",
        f"issues={len(bad_ep)}",
        "source/target in nodes[].node_id",
        "CRITICAL" if bad_ep else "LOW",
        len(bad_ep),
        bad_ep[:5],
        "Every edge source/target must reference an existing node_id.",
    )
    add(
        "week4.edges.relationship.enum",
        "edges[*].relationship",
        "enum",
        "FAIL" if bad_rel else "PASS",
        f"invalid={len(bad_rel)}",
        str(allowed_rel),
        "CRITICAL" if bad_rel else "LOW",
        len(bad_rel),
        bad_rel[:5],
        "edge.relationship must be one of six enum values.",
    )
    add(
        "week4.edges.confidence.range",
        "edges[*].confidence",
        "range",
        "FAIL" if bad_conf_e else "PASS",
        f"invalid={len(bad_conf_e)}",
        "0.0–1.0",
        "CRITICAL" if bad_conf_e else "LOW",
        len(bad_conf_e),
        bad_conf_e[:5],
        "edge.confidence must be numeric 0.0–1.0.",
    )

    try:
        datetime.fromisoformat(str(snap.get("captured_at", "")).replace("Z", "+00:00"))
        add(
            "week4.captured_at.iso8601",
            "captured_at",
            "format",
            "PASS",
            "ok",
            "ISO 8601",
            "LOW",
            0,
            [],
            "captured_at OK.",
        )
    except Exception:
        add(
            "week4.captured_at.iso8601",
            "captured_at",
            "format",
            "FAIL",
            "invalid",
            "ISO 8601",
            "CRITICAL",
            1,
            [],
            "captured_at must be ISO 8601.",
        )

    return results


def validate_langsmith_runs(rows: list[dict], contract: dict[str, Any], root: Path) -> list[Any]:
    from contracts.common import CheckResult

    _ = contract, root
    results: list[CheckResult] = []

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

    run_types = {"llm", "chain", "tool", "retriever", "embedding"}
    bad_rt: list[str] = []
    bad_time: list[str] = []
    bad_tok: list[str] = []
    bad_cost: list[str] = []
    bad_uuid: list[str] = []

    for r in rows:
        rid = str(r.get("id", ""))
        if not UUID_RE.match(rid):
            bad_uuid.append(rid)
        if str(r.get("run_type", "")) not in run_types:
            bad_rt.append(rid)
        try:
            st = datetime.fromisoformat(str(r.get("start_time", "")).replace("Z", "+00:00"))
            et = datetime.fromisoformat(str(r.get("end_time", "")).replace("Z", "+00:00"))
            if et <= st:
                bad_time.append(rid)
        except Exception:
            bad_time.append(rid)
        pt, ct, tt = r.get("prompt_tokens"), r.get("completion_tokens"), r.get("total_tokens")
        if not all(isinstance(x, int) for x in (pt, ct, tt)) or tt != pt + ct:
            bad_tok.append(rid)
        c = r.get("total_cost")
        if not isinstance(c, (int, float)) or float(c) < 0.0:
            bad_cost.append(rid)

    add(
        "langsmith.id.uuid",
        "id",
        "format",
        "FAIL" if bad_uuid else "PASS",
        f"invalid={len(bad_uuid)}",
        "uuid",
        "CRITICAL" if bad_uuid else "LOW",
        len(bad_uuid),
        bad_uuid[:5],
        "Trace id must be UUID v4 pattern.",
    )
    add(
        "langsmith.run_type.enum",
        "run_type",
        "enum",
        "FAIL" if bad_rt else "PASS",
        f"invalid={len(bad_rt)}",
        str(run_types),
        "CRITICAL" if bad_rt else "LOW",
        len(bad_rt),
        bad_rt[:5],
        "run_type must be llm|chain|tool|retriever|embedding.",
    )
    add(
        "langsmith.end_time.after_start",
        "end_time",
        "temporal",
        "FAIL" if bad_time else "PASS",
        f"invalid={len(bad_time)}",
        "end_time > start_time",
        "CRITICAL" if bad_time else "LOW",
        len(bad_time),
        bad_time[:5],
        "end_time must be strictly after start_time.",
    )
    add(
        "langsmith.total_tokens.sum",
        "total_tokens",
        "identity",
        "FAIL" if bad_tok else "PASS",
        f"invalid={len(bad_tok)}",
        "total_tokens = prompt_tokens + completion_tokens",
        "CRITICAL" if bad_tok else "LOW",
        len(bad_tok),
        bad_tok[:5],
        "Token identity check.",
    )
    add(
        "langsmith.total_cost.non_negative",
        "total_cost",
        "range",
        "FAIL" if bad_cost else "PASS",
        f"invalid={len(bad_cost)}",
        ">= 0",
        "CRITICAL" if bad_cost else "LOW",
        len(bad_cost),
        bad_cost[:5],
        "total_cost must be numeric and >= 0.",
    )

    return results


def run_cross_system_validation(root: Path) -> dict[str, Any]:
    """Explicit cross-dataset contracts: Week1→Week2, Week3→Week4."""
    from contracts.common import CheckResult, jsonl_snapshot_id
    import uuid

    def _iso_now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    intents = load_jsonl(root / "outputs" / "week1" / "intent_records.jsonl")
    verdicts = load_jsonl(root / "outputs" / "week2" / "verdicts.jsonl")
    extractions = load_jsonl(root / "outputs" / "week3" / "extractions.jsonl")
    lineage_rows = load_jsonl(root / "outputs" / "week4" / "lineage_snapshots.jsonl")

    results: list[CheckResult] = []

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

    intent_files: set[str] = set()
    for r in intents:
        for cr in r.get("code_refs") or []:
            f = cr.get("file")
            if f:
                intent_files.add(str(f).replace("\\", "/"))

    bad_targets: list[str] = []
    for v in verdicts:
        tr = str(v.get("target_ref", "")).replace("\\", "/")
        if tr not in intent_files:
            bad_targets.append(str(v.get("verdict_id", "")))
    add(
        "cross.week2.target_ref.in_week1_code_refs",
        "verdict_record.target_ref",
        "relationship",
        "FAIL" if bad_targets else "PASS",
        f"mismatch={len(bad_targets)}",
        "target_ref ∈ ⋃ intent_record.code_refs.file",
        "CRITICAL" if bad_targets else "LOW",
        len(bad_targets),
        bad_targets[:5],
        "Week 2 target_ref must reference a file path produced by Week 1 code_refs.",
    )

    doc_ids = {str(r.get("doc_id")) for r in extractions if r.get("doc_id")}
    latest = lineage_rows[-1] if lineage_rows else {}
    node_ids = {str(n.get("node_id")) for n in (latest.get("nodes") or []) if n.get("node_id")}
    missing_doc_nodes: list[str] = []
    for did in doc_ids:
        if f"table::doc:{did}" not in node_ids:
            missing_doc_nodes.append(did)
    add(
        "cross.week4.doc_id.as_lineage_node",
        "lineage.nodes",
        "relationship",
        "FAIL" if missing_doc_nodes else "PASS",
        f"missing_nodes={len(missing_doc_nodes)}",
        "table::doc:{doc_id} for each extraction",
        "CRITICAL" if missing_doc_nodes else "LOW",
        len(missing_doc_nodes),
        missing_doc_nodes[:5],
        "Each Week 3 doc_id must appear as table::doc:{doc_id} in latest lineage snapshot.",
    )

    combined_path = root / "outputs" / "_cross_validation_manifest.jsonl"
    combined_path.parent.mkdir(parents=True, exist_ok=True)
    combined_path.write_text(
        json.dumps({"week1_files": sorted(intent_files), "doc_count": len(doc_ids)}) + "\n", encoding="utf-8"
    )
    snap = jsonl_snapshot_id(combined_path)

    passed = sum(1 for c in results if c.status == "PASS")
    failed = sum(1 for c in results if c.status == "FAIL")
    warned = sum(1 for c in results if c.status == "WARN")
    errored = sum(1 for c in results if c.status == "ERROR")

    return {
        "report_id": str(uuid.uuid4()),
        "contract_id": "cross-system-dependencies",
        "snapshot_id": snap,
        "run_timestamp": _iso_now(),
        "total_checks": len(results),
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
            for c in results
        ],
    }
