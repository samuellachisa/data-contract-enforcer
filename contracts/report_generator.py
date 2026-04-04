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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]


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


def _schema_diff_breaking_summary() -> list[str]:
    """
    Quick summary from the last two schema snapshots for week3/week5.
    """
    out: list[str] = []
    import yaml

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


def _recommended_actions(violations: list[dict[str, Any]], ai_metrics: dict[str, Any]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    # Prioritize concrete contract breaks first.
    for v in violations:
        check_id = str(v.get("check_id", ""))
        if "week3.extracted_facts.confidence.range" in check_id:
            actions.append(
                {
                    "priority": 1,
                    "risk_reduction": "High",
                    "action": "Update `src/week3/extractor.py` so `extracted_facts[*].confidence` is emitted as a float in the 0.0–1.0 range (contract clause: `week3.extracted_facts.confidence.range`).",
                }
            )
        if "week3.extracted_facts.entity_refs.relationship" in check_id:
            actions.append(
                {
                    "priority": 2,
                    "risk_reduction": "High",
                    "action": "Update `src/week3/extractor.py` so every `extracted_facts[*].entity_refs[]` value is an `entity_id` present in the same record’s `entities[]` array (contract clause: `week3.extracted_facts.entity_refs.relationship`).",
                }
            )

    if not actions:
        actions.append({"priority": 1, "risk_reduction": "Medium", "action": "Review the highest-severity contract failures and update producers/consumers accordingly."})

    # AI risks
    if ai_metrics.get("status") == "WARN":
        actions.append(
            {
                "priority": 3,
                "risk_reduction": "Medium",
                "action": "Stabilize Week 2 structured verdict outputs by updating the LLM prompt or parser so the `scores[*].score` field is always an integer 1–5 (AI contract: `week2.verdict_record.schema`).",
            }
        )
    else:
        actions.append(
            {
                "priority": 3,
                "risk_reduction": "Low",
                "action": "Re-run AI extensions after any model/prompt change and monitor `validation_reports/ai_metrics.json` for rising schema violation rate.",
            }
        )

    actions.sort(key=lambda x: x["priority"])
    return actions[:3]


def generate_report(
    *,
    week3_report: Path | None = None,
    week5_report: Path | None = None,
    ai_metrics_path: Path | None = None,
    violations_path: Path | None = None,
    strict_pdf: bool = False,
) -> dict[str, Any]:
    week3, w3_src = _load_json_explicit_or_glob(
        week3_report, str(_REPO / "validation_reports" / "week3_*.json")
    )
    week5, w5_src = _load_json_explicit_or_glob(
        week5_report, str(_REPO / "validation_reports" / "week5_*.json")
    )

    violations, viol_src = _load_violations_with_blame(violations_path)

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

    sources_used = {
        "week3_validation": w3_src,
        "week5_validation": w5_src,
        "ai_metrics": ai_src,
        "violations": viol_src,
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

    base = (checks_passed / max(1, total_checks)) * 100.0
    score = max(0.0, float(base) - (critical_failures * 20.0))
    score = round(score, 2)

    top_violations = _top_violations_from_validation(week3, week5)
    violations_this_week = []
    for v in violations:
        if v.get("type") == "llm_output_schema":
            violations_this_week.append({"check_id": v.get("check_id"), "severity": "CRITICAL", "message": _describe_violation(v)})
    for tv in top_violations:
        violations_this_week.append(tv)
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

    schema_changes = _schema_diff_breaking_summary()

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

    recommended_actions = _recommended_actions(violations, ai_metrics)

    pass_rate = round((checks_passed / max(1, total_checks)) * 100.0, 2)
    exec_summary = (
        f"This run aggregated {total_checks} contract checks across the selected Week 3 and Week 5 reports "
        f"({pass_rate}% PASS). Data health score is {score}/100 with {critical_failures} critical-severity "
        f"failure(s) in validation results. Review violations, schema drift, and AI metrics for prioritized fixes."
    )

    registry_impact = _registry_subscriber_impact(violations)

    report_data: dict[str, Any] = {
        "report_generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources_used": sources_used,
        "executive_summary": exec_summary,
        "registry_subscriber_impact": registry_impact,
        "data_health_score": score,
        "n_critical_contract_violations": critical_failures,
        "data_health_narrative": (
            "Overall health is reduced by high-severity contract violations. "
            "Fix CRITICAL fields to prevent silent corruption downstream."
        ),
        "violations_this_week": [
            {
                "severity": v.get("severity"),
                "description": _describe_violation({"check_id": v.get("check_id"), "type": v.get("type"), "message": v.get("message")})
                if v.get("message")
                else _describe_violation(v),
                "check_id": v.get("check_id"),
            }
            for v in violations_this_week_final
        ],
        "schema_changes_detected": schema_changes,
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
        strict_pdf=args.strict_pdf,
    )


if __name__ == "__main__":
    main()

