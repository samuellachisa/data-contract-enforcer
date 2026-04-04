#!/usr/bin/env python3
"""
ReportGenerator (Week 7)

Auto-generates:
- enforcer_report/report_data.json
- enforcer_report/report_{date}.pdf
"""
from __future__ import annotations

import argparse
import glob
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

_REPO = Path(__file__).resolve().parents[1]

# Practitioner manual — severity deductions across all validation_reports/*.json with a `results` array.
SEVERITY_DEDUCTIONS_MANUAL = {"CRITICAL": 20, "HIGH": 10, "MEDIUM": 5, "LOW": 1, "WARNING": 1}


def _now_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _load_latest_json_with_path(path_glob: str) -> tuple[dict[str, Any] | None, str | None]:
    matches = sorted(glob.glob(path_glob), key=lambda p: Path(p).stat().st_mtime)
    if not matches:
        return None, None
    p = Path(matches[-1])
    return json.loads(p.read_text(encoding="utf-8")), str(p.resolve())


def _load_json_explicit_or_glob(
    explicit: Path | None, path_glob: str
) -> tuple[dict[str, Any] | None, str | None]:
    if explicit is not None:
        p = explicit.expanduser().resolve()
        if not p.is_file():
            return None, str(p)
        return json.loads(p.read_text(encoding="utf-8")), str(p)
    return _load_latest_json_with_path(path_glob)


def _severity_rank(sev: str) -> int:
    order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "WARNING": 4}
    return order.get(sev, 99)


def _iter_structured_validation_reports(root: Path) -> list[Path]:
    """JSON files under validation_reports/ that look like ValidationRunner output."""
    vr = root / "validation_reports"
    if not vr.is_dir():
        return []
    skip = {"ai_metrics.json", "ai_extensions.json", "report_data.json"}
    out: list[Path] = []
    for p in sorted(vr.glob("*.json")):
        if p.name in skip or p.name.startswith("migration_impact_"):
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and isinstance(data.get("results"), list):
            out.append(p)
    return out


def _manual_health_score_and_fails(root: Path) -> tuple[float, list[dict[str, Any]]]:
    """Dedupe failing checks by check_id so duplicate report files do not multiply deductions."""
    best_fail_per_check: dict[str, dict[str, Any]] = {}
    for p in _iter_structured_validation_reports(root):
        data = json.loads(p.read_text(encoding="utf-8"))
        for r in data.get("results", []) or []:
            if str(r.get("status")) not in {"FAIL", "ERROR"}:
                continue
            row = dict(r)
            cid = str(row.get("check_id", p.name))
            prev = best_fail_per_check.get(cid)
            if prev is None or _severity_rank(str(row.get("severity", "LOW"))) < _severity_rank(
                str(prev.get("severity", "LOW"))
            ):
                best_fail_per_check[cid] = row
    all_fails = list(best_fail_per_check.values())
    score = 100.0
    for f in all_fails:
        score -= float(SEVERITY_DEDUCTIONS_MANUAL.get(str(f.get("severity", "LOW")), 1))
    return max(0.0, min(100.0, round(score, 2))), all_fails


def _plain_language_failure(
    result: dict[str, Any], registry_path: Path
) -> str:
    """Rubric-style sentence tying a failed check to registry subscribers."""
    try:
        reg = yaml.safe_load(registry_path.read_text(encoding="utf-8")) if registry_path.is_file() else {}
    except Exception:
        reg = {}
    subs_list = reg.get("subscriptions", []) if isinstance(reg, dict) else []
    check_id = str(result.get("check_id", ""))
    contract_id = ""
    if check_id.startswith("week3.") or check_id.startswith("cross.week3"):
        contract_id = "week3-document-refinery-extractions"
    elif check_id.startswith("week5.") or check_id.startswith("cross.week5"):
        contract_id = "week5-event-sourcing-events"
    elif check_id.startswith("week4.") or check_id.startswith("cross.week4"):
        contract_id = "week4-brownfield-lineage-snapshots"
    elif check_id.startswith("langsmith.") or check_id.startswith("cross.langsmith"):
        contract_id = "langsmith-trace-runs"
    sub_strs = [
        str(s.get("subscriber_id", ""))
        for s in subs_list
        if isinstance(s, dict) and str(s.get("contract_id", "")) == contract_id
    ]
    sub_str = ", ".join(sub_strs) if sub_strs else "no registered subscribers"
    col = str(result.get("column_name", result.get("field", "?")))
    ctype = str(result.get("check_type", "contract"))
    exp = str(result.get("expected", "per contract"))
    act = str(result.get("actual_value", "see report"))
    nfail = result.get("records_failing", "unknown")
    return (
        f"The '{col}' field failed its {ctype} check. Expected {exp}, found {act}. "
        f"Downstream subscribers affected: {sub_str}. Records failing: {nfail}."
    )


def _load_violations_from_path(p: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("//"):
                continue
            rows.append(json.loads(line))
    return rows


def _load_violations_with_blame(explicit: Path | None = None) -> tuple[list[dict[str, Any]], str | None]:
    if explicit is not None:
        p = explicit.expanduser().resolve()
        if p.is_file():
            return _load_violations_from_path(p), str(p)
        return [], str(p)
    p = _REPO / "violation_log" / "violations_with_blame.jsonl"
    if not p.exists():
        p = _REPO / "violation_log" / "violations.jsonl"
    if not p.exists():
        return [], None
    return _load_violations_from_path(p), str(p.resolve())


def _registry_subscriber_impact(violations: list[dict[str, Any]]) -> dict[str, Any]:
    counts: list[int] = []
    for v in violations:
        br = v.get("blast_radius")
        if not isinstance(br, dict):
            continue
        subs = br.get("subscribers")
        if isinstance(subs, list):
            counts.append(len(subs))
    return {
        "violations_with_blast_radius": sum(1 for c in counts if c > 0),
        "max_subscribers_on_single_violation": max(counts) if counts else 0,
        "total_subscriber_rows_across_violations": sum(counts),
    }


def _top_violations_from_validation(week3: dict[str, Any] | None, week5: dict[str, Any] | None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for report, system in [(week3, "week3-document-refinery"), (week5, "week5-event-sourcing")]:
        if not report:
            continue
        for r in report.get("results", []):
            if r.get("status") in {"FAIL", "ERROR"}:
                items.append(
                    {
                        "check_id": r.get("check_id"),
                        "system": system,
                        "field": r.get("column_name"),
                        "severity": r.get("severity"),
                        "records_failing": r.get("records_failing"),
                        "message": r.get("message"),
                    }
                )
    items.sort(key=lambda x: _severity_rank(str(x.get("severity"))))
    return items[:3]


def _normalize_violation_row(item: dict[str, Any]) -> dict[str, Any]:
    """Uniform keys for dedupe, sorting, and _describe_violation."""
    row = dict(item)
    if not row.get("field"):
        row["field"] = row.get("column_name") or (
            "verdict_record" if row.get("type") == "llm_output_schema" else "*"
        )
    row.setdefault("message", "")
    row.setdefault("check_id", "")
    row.setdefault("severity", "LOW")
    return row


def _parse_migration_report_payload(data: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    """Turn schema_analyzer migration_impact_*.json into bullets + structured summary."""
    mi = data.get("migration_impact")
    if not isinstance(mi, dict):
        mi = {}
    verdict = mi.get("compatibility_verdict") or data.get("compatibility_verdict") or "UNKNOWN"
    breaking = mi.get("breaking_fields") or data.get("breaking_fields") or []
    if not isinstance(breaking, list):
        breaking = []
    rollback = str(mi.get("rollback_plan") or data.get("rollback_plan") or "")
    checklist = mi.get("migration_checklist") or data.get("migration_checklist") or []
    if not isinstance(checklist, list):
        checklist = []

    cid = str(data.get("contract_id") or "contract")
    bullets: list[str] = [f"{cid}: compatibility_verdict={verdict}."]
    for bf in breaking[:10]:
        if isinstance(bf, str) and bf.strip():
            bullets.append(f"{cid}: breaking field `{bf}`.")
    for task in checklist[:6]:
        if isinstance(task, dict):
            t = str(task.get("task", "")).strip()
            if t:
                bullets.append(f"{cid}: migration task — {t}")
    if rollback:
        tail = "..." if len(rollback) > 280 else ""
        bullets.append(f"{cid}: rollback — {rollback[:280]}{tail}")

    summary: dict[str, Any] = {
        "contract_id": data.get("contract_id"),
        "compatibility_verdict": verdict,
        "breaking_fields": breaking,
        "rollback_plan": rollback,
        "checklist_preview": checklist[:8],
    }
    return bullets[:14], summary


def _load_migration_schema_section(
    explicit: Path | None, root: Path
) -> tuple[list[str], dict[str, Any] | None, str | None]:
    """
    Prefer explicit migration JSON; else latest validation_reports/migration_impact_*.json by mtime.
    """
    if explicit is not None:
        p = explicit.expanduser().resolve()
        if not p.is_file():
            return [], None, str(p)
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return [], None, str(p.resolve())
        bullets, summary = _parse_migration_report_payload(data)
        return bullets, summary, str(p.resolve())

    pattern = str(root / "validation_reports" / "migration_impact_*.json")
    matches = sorted(glob.glob(pattern), key=lambda x: Path(x).stat().st_mtime)
    if not matches:
        return [], None, None
    p = Path(matches[-1])
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return [], None, str(p.resolve())
    bullets, summary = _parse_migration_report_payload(data)
    return bullets, summary, str(p.resolve())


def _schema_diff_breaking_summary() -> list[str]:
    """
    Quick summary from the last two schema snapshots for week3/week5.
    """
    out: list[str] = []

    def last_two(contract_id: str) -> list[Path]:
        d = _REPO / "schema_snapshots" / contract_id
        if not d.exists():
            return []
        subs = sorted([p for p in d.iterdir() if (p / "schema.yaml").exists()], key=lambda x: x.name)
        return subs[-2:]

    def diff_type(a: dict[str, Any], b: dict[str, Any]) -> list[str]:
        msgs = []
        sa = a.get("schema", {}) if isinstance(a, dict) else {}
        sb = b.get("schema", {}) if isinstance(b, dict) else {}
        for k in set(sa.keys()) & set(sb.keys()):
            da = sa.get(k, {})
            db = sb.get(k, {})
            if isinstance(da, dict) and isinstance(db, dict) and da.get("type") != db.get("type"):
                msgs.append(f"Field `{k}` changed type from `{da.get('type')}` to `{db.get('type')}`.")
        return msgs

    for cid, label in [
        ("week3-document-refinery-extractions", "Week 3 extraction records"),
        ("week5-event-sourcing-events", "Week 5 event records"),
    ]:
        subs = last_two(cid)
        if len(subs) < 2:
            continue
        a = yaml.safe_load((subs[0] / "schema.yaml").read_text(encoding="utf-8"))
        b = yaml.safe_load((subs[1] / "schema.yaml").read_text(encoding="utf-8"))
        msgs = diff_type(a, b)
        for m in msgs:
            out.append(f"{label}: {m} Breaking change requires coordinated consumer updates.")
    return out[:5]


def _describe_violation(v: dict[str, Any]) -> str:
    check_id = str(v.get("check_id", ""))
    if "week3.extracted_facts.confidence.range" in check_id:
        return (
            "Week 3 Document Refinery: `extracted_facts[*].confidence` must stay a float in 0.0–1.0. "
            "Downstream systems that interpret confidence as a probability (Week 4 lineage attribution and any scoring consumers) may mis-rank or mis-blame changes."
        )
    if "week3.extracted_facts.entity_refs.relationship" in check_id:
        return (
            "Week 3 Document Refinery: `extracted_facts[*].entity_refs` must reference IDs present in `entities[]`. "
            "Downstream lineage metadata can become inconsistent, breaking entity-aware attribution and any audit joins."
        )
    if v.get("type") == "llm_output_schema":
        return (
            "Week 2 Digital Courtroom: verdict records failed structured LLM output schema validation. "
            "Downstream AI consumers can ingest malformed scores or evidence and produce incorrect risk decisions."
        )
    return v.get("message", "Contract violation.")


def _recommended_actions(
    violations: list[dict[str, Any]], ai_metrics: dict[str, Any], *, repo: Path
) -> list[dict[str, Any]]:
    """Return up to three distinct, ordered remediation steps (rubric: prioritised actions)."""
    runner = (repo / "contracts" / "runner.py").resolve()
    extractor = (repo / "src" / "week3" / "extractor.py").resolve()
    baselines_path = (repo / "schema_snapshots" / "baselines.json").resolve()
    check_ids = {str(v.get("check_id", "")) for v in violations}

    candidates: list[dict[str, Any]] = []
    if any("week3.extracted_facts.confidence.range" in c for c in check_ids):
        candidates.append(
            {
                "priority": 1,
                "risk_reduction": "High",
                "action": (
                    f"Update `{extractor}` so `extracted_facts[*].confidence` is a float in 0.0–1.0 "
                    "(contract `week3-document-refinery-extractions`, clause `week3.extracted_facts.confidence.range`)."
                ),
            }
        )
    if any("week3.extracted_facts.entity_refs.relationship" in c for c in check_ids):
        candidates.append(
            {
                "priority": 2,
                "risk_reduction": "High",
                "action": (
                    f"Update `{extractor}` so each `extracted_facts[*].entity_refs[]` references an `entity_id` "
                    "from the same record’s `entities[]` (clause `week3.extracted_facts.entity_refs.relationship`)."
                ),
            }
        )
    if any("week3.extracted_facts.confidence.statistical_drift" in c for c in check_ids):
        candidates.append(
            {
                "priority": 3,
                "risk_reduction": "High",
                "action": (
                    f"After restoring `extracted_facts[*].confidence` to 0.0–1.0, re-run `{runner}` and refresh "
                    f"statistical baselines in `{baselines_path}` for that field "
                    "(clause `week3.extracted_facts.confidence.statistical_drift`)."
                ),
            }
        )

    if not candidates:
        candidates.append(
            {
                "priority": 1,
                "risk_reduction": "Medium",
                "action": (
                    f"Review failing rows in `validation_reports/*.json` and align producers with "
                    f"`generated_contracts/*.yaml`."
                ),
            }
        )

    if ai_metrics.get("status") == "WARN":
        candidates.append(
            {
                "priority": 4,
                "risk_reduction": "Medium",
                "action": (
                    "Stabilize Week 2 structured verdict outputs so `scores[*].score` is integer 1–5 "
                    "(contract `week2-digital-courtroom-verdicts`, clause `week2.scores.criterion.range`); "
                    "inspect `outputs/week2/verdicts.jsonl` and `contracts/ai_extensions.py`."
                ),
            }
        )
    else:
        candidates.append(
            {
                "priority": 4,
                "risk_reduction": "Low",
                "action": (
                    f"Add `{runner}` as a CI step before Week 3 deployments; refresh drift baselines monthly."
                ),
            }
        )

    candidates.sort(key=lambda x: x["priority"])
    return candidates[:3]


def generate_report(
    *,
    week3_report: Path | None = None,
    week5_report: Path | None = None,
    ai_metrics_path: Path | None = None,
    violations_path: Path | None = None,
    migration_report: Path | None = None,
    strict_pdf: bool = False,
) -> dict[str, Any]:
    week3, w3_src = _load_json_explicit_or_glob(
        week3_report, str(_REPO / "validation_reports" / "week3_*.json")
    )
    week5, w5_src = _load_json_explicit_or_glob(
        week5_report, str(_REPO / "validation_reports" / "week5_*.json")
    )

    violations, viol_src = _load_violations_with_blame(violations_path)

    mig_bullets, mig_summary, mig_src = _load_migration_schema_section(migration_report, _REPO)

    if ai_metrics_path is not None:
        amp = ai_metrics_path.expanduser().resolve()
        if amp.is_file():
            ai_metrics = json.loads(amp.read_text(encoding="utf-8"))
            ai_src = str(amp)
        else:
            ai_metrics = {}
            ai_src = str(amp)
    else:
        default_ai = _REPO / "validation_reports" / "ai_metrics.json"
        if default_ai.exists():
            ai_metrics = json.loads(default_ai.read_text(encoding="utf-8"))
            ai_src = str(default_ai.resolve())
        else:
            ai_metrics = {}
            ai_src = None

    ai_ext_path = _REPO / "validation_reports" / "ai_extensions.json"
    if ai_ext_path.exists():
        try:
            extra_ai = json.loads(ai_ext_path.read_text(encoding="utf-8"))
            if isinstance(extra_ai, dict):
                for k, v in extra_ai.items():
                    ai_metrics.setdefault(k, v)
        except json.JSONDecodeError:
            pass

    sources_used = {
        "week3_validation": w3_src,
        "week5_validation": w5_src,
        "ai_metrics": ai_src,
        "violations": viol_src,
        "migration_report": mig_src,
    }

    total_checks = 0
    checks_passed = 0
    critical_failures = 0

    for rep in [week3, week5]:
        if not rep:
            continue
        total_checks += int(rep.get("total_checks", 0))
        checks_passed += int(rep.get("passed", 0))
        for r in rep.get("results", []):
            if str(r.get("status")) in {"FAIL", "ERROR"} and str(r.get("severity")) == "CRITICAL":
                critical_failures += 1

    manual_score, manual_fail_rows = _manual_health_score_and_fails(_REPO)
    score = manual_score
    critical_failures = sum(
        1 for r in manual_fail_rows if str(r.get("severity")) == "CRITICAL"
    )

    reg_yaml = _REPO / "contract_registry" / "subscriptions.yaml"
    top_plain = sorted(manual_fail_rows, key=lambda x: _severity_rank(str(x.get("severity", "LOW"))))[:3]
    top_violations_plain = [_plain_language_failure(r, reg_yaml) for r in top_plain]
    violations_by_severity = {
        "CRITICAL": sum(1 for r in manual_fail_rows if str(r.get("severity")) == "CRITICAL"),
        "HIGH": sum(1 for r in manual_fail_rows if str(r.get("severity")) == "HIGH"),
        "MEDIUM": sum(1 for r in manual_fail_rows if str(r.get("severity")) == "MEDIUM"),
    }

    top_violations = _top_violations_from_validation(week3, week5)
    violations_this_week = []
    for v in violations:
        if v.get("type") == "llm_output_schema":
            violations_this_week.append(
                _normalize_violation_row(
                    {
                        "check_id": v.get("check_id"),
                        "severity": "CRITICAL",
                        "type": "llm_output_schema",
                        "message": v.get("message", ""),
                        "field": "verdict_record",
                    }
                )
            )
    for tv in top_violations:
        violations_this_week.append(_normalize_violation_row(tv))
    # De-dup + pick 3 by severity
    violations_this_week.sort(key=lambda x: _severity_rank(str(x.get("severity", "LOW"))))
    violations_this_week_final = []
    seen = set()
    for item in violations_this_week:
        key = str(item.get("check_id")) + "::" + str(item.get("field", ""))
        if key in seen:
            continue
        seen.add(key)
        violations_this_week_final.append(item)
        if len(violations_this_week_final) >= 3:
            break

    if not violations_this_week_final and top_plain:
        for r in top_plain[:3]:
            violations_this_week_final.append(
                _normalize_violation_row(
                    {
                        "check_id": r.get("check_id"),
                        "severity": r.get("severity", "HIGH"),
                        "field": r.get("column_name", ""),
                        "message": r.get("message", ""),
                    }
                )
            )

    schema_changes = mig_bullets if mig_bullets else _schema_diff_breaking_summary()

    ai_risk_parts = []
    emb = ai_metrics.get("embedding_drift") or {}
    if emb.get("status") == "FAIL":
        ai_risk_parts.append(f"Embedding drift exceeded threshold: drift_score={emb.get('drift_score')}.")
    elif emb.get("status") == "PASS":
        ai_risk_parts.append("Embedding drift is within acceptable bounds.")
    else:
        ai_risk_parts.append("Embedding drift check did not conclusively PASS/FAIL.")

    if ai_metrics.get("trend") == "rising":
        ai_risk_parts.append(
            f"Structured LLM output schema violation rate is rising (violation_rate={ai_metrics.get('violation_rate')}, baseline={ai_metrics.get('baseline_violation_rate')})."
        )
    else:
        ai_risk_parts.append("LLM output schema violation rate is stable.")

    recommended_actions = _recommended_actions(violations, ai_metrics, repo=_REPO)

    pass_rate = round((checks_passed / max(1, total_checks)) * 100.0, 2)
    n_fail_all = len(manual_fail_rows)
    health_narrative = (
        f"Score {score}/100 from {n_fail_all} failed check(s) across validation_reports/*.json (manual rubric deductions). "
        + (
            "All sampled systems operating within contract parameters."
            if score >= 90 and n_fail_all == 0
            else f"{critical_failures} critical issue(s) require immediate action."
        )
    )
    exec_summary = (
        f"This run aggregated {total_checks} contract checks across the selected Week 3 and Week 5 reports "
        f"({pass_rate}% PASS on those two). Data health score is {score}/100 using severity-weighted deductions "
        f"over all structured validation JSON in validation_reports/ ({n_fail_all} failing check rows). "
        "Review violations, schema drift, and AI metrics for prioritized fixes."
    )

    registry_impact = _registry_subscriber_impact(violations)

    now = datetime.now(timezone.utc)
    report_data: dict[str, Any] = {
        "report_generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "period": f"{(now - timedelta(days=7)).date()} to {now.date()}",
        "sources_used": sources_used,
        "executive_summary": exec_summary,
        "registry_subscriber_impact": registry_impact,
        "data_health_score": score,
        "n_critical_contract_violations": critical_failures,
        "data_health_narrative": health_narrative,
        "top_violations_plain_language": top_violations_plain,
        "violations_by_severity": violations_by_severity,
        "ai_risk": {
            "embedding_drift": (ai_metrics.get("embedding_drift") or {}).get("drift_score", "N/A"),
            "output_violation_rate": ai_metrics.get("violation_rate", "N/A"),
            "status": ai_metrics.get("status", "UNKNOWN"),
        },
        "violations_this_week": [
            {
                "severity": v.get("severity"),
                "description": _describe_violation(v),
                "check_id": v.get("check_id"),
                "field": v.get("field"),
            }
            for v in violations_this_week_final
        ],
        "schema_changes_detected": schema_changes,
        "schema_evolution_summary": mig_summary,
        "ai_system_risk_assessment": ai_risk_parts,
        "recommended_actions": recommended_actions,
        "pdf_path": None,
        "pdf_status": "pending",
        "pdf_error": None,
    }

    out_data = _REPO / "enforcer_report" / "report_data.json"

    # PDF report (minimal but readable).
    pdf_date = _now_date()
    pdf_path = _REPO / "enforcer_report" / f"report_{pdf_date}.pdf"
    report_data["pdf_path"] = str(pdf_path).replace("\\", "/")
    try:
        from fpdf import FPDF

        def _pdf_sanitize(s: Any) -> str:
            t = str(s)
            # FPDF core fonts are latin-1; replace common punctuation.
            t = (
                t.replace("—", "-")
                .replace("–", "-")
                .replace("’", "'")
                .replace("“", '"')
                .replace("”", '"')
                .replace("…", "...")
                .replace("`", "")
                .replace(".", " ")
                .replace("[", " ")
                .replace("]", " ")
                .replace("*", " ")
                .replace("/", " ")
                .replace("_", " ")
                .replace("\n", " ")
                .replace("\r", " ")
            )
            t = " ".join(t.split())
            if len(t) > 240:
                t = t[:237] + "..."
            return t

        pdf = FPDF()
        pdf.set_auto_page_break(auto=True, margin=15)
        pdf.add_page()
        pdf.set_font("Helvetica", size=12)
        w = pdf.w - pdf.l_margin - pdf.r_margin

        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(w, 7, _pdf_sanitize("TRP Week 7 — Data Contract Enforcer\n(Automatically generated report)"))
        pdf.ln(2)
        pdf.set_font("Helvetica", size=10)
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(w, 7, _pdf_sanitize(f"Data Health Score: {score}/100"))
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(w, 6, _pdf_sanitize(exec_summary))
        pdf.ln(1)
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(w, 7, _pdf_sanitize("Violations (top 3):"))
        for v in report_data["violations_this_week"]:
            pdf.set_x(pdf.l_margin)
            pdf.multi_cell(w, 6, _pdf_sanitize(f"- {v.get('severity')}: {v.get('description')}"))
        pdf.ln(1)

        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(w, 7, _pdf_sanitize("Schema changes detected:"))
        if schema_changes:
            for s in schema_changes:
                pdf.set_x(pdf.l_margin)
                pdf.multi_cell(w, 6, _pdf_sanitize(f"- {s}"))
        else:
            pdf.set_x(pdf.l_margin)
            pdf.multi_cell(w, 6, _pdf_sanitize("- No breaking schema changes detected in the last snapshots."))
        pdf.ln(1)

        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(w, 7, _pdf_sanitize("AI system risk assessment:"))
        for part in ai_risk_parts:
            pdf.set_x(pdf.l_margin)
            pdf.multi_cell(w, 6, _pdf_sanitize(f"- {part}"))

        pdf.ln(1)
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(w, 7, _pdf_sanitize("Recommended actions:"))
        for a in recommended_actions:
            pdf.set_x(pdf.l_margin)
            pdf.multi_cell(w, 6, _pdf_sanitize(f"{a.get('priority')}. {a.get('action')}"))

        pdf.output(str(pdf_path).replace("\\", "/"))
        report_data["pdf_status"] = "ok"
        report_data["pdf_error"] = None
    except Exception as exc:
        import traceback

        traceback.print_exc()
        pdf_error_msg = f"{type(exc).__name__}: {exc}"
        report_data["pdf_status"] = "failed"
        report_data["pdf_error"] = pdf_error_msg

    out_data.parent.mkdir(parents=True, exist_ok=True)
    out_data.write_text(json.dumps(report_data, indent=2), encoding="utf-8")

    if strict_pdf and report_data.get("pdf_status") == "failed":
        raise SystemExit(1)

    return report_data


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Enforcer Report (Week 7)")
    parser.add_argument("--week3-report", type=Path, default=None, help="Explicit Week 3 validation JSON (default: latest week3_*.json).")
    parser.add_argument("--week5-report", type=Path, default=None, help="Explicit Week 5 validation JSON (default: latest week5_*.json).")
    parser.add_argument("--ai-metrics", type=Path, default=None, help="Path to ai_metrics.json (default: validation_reports/ai_metrics.json).")
    parser.add_argument("--violations", type=Path, default=None, help="Violations JSONL (default: violations_with_blame or violations).")
    parser.add_argument(
        "--migration-report",
        type=Path,
        default=None,
        help="migration_impact_*.json from schema_analyzer (default: latest validation_reports/migration_impact_*.json).",
    )
    parser.add_argument(
        "--strict-pdf",
        action="store_true",
        help="Exit with code 1 if PDF generation fails (default: tolerate PDF errors).",
    )
    args = parser.parse_args()
    generate_report(
        week3_report=args.week3_report,
        week5_report=args.week5_report,
        ai_metrics_path=args.ai_metrics,
        violations_path=args.violations,
        migration_report=args.migration_report,
        strict_pdf=args.strict_pdf,
    )


if __name__ == "__main__":
    main()

