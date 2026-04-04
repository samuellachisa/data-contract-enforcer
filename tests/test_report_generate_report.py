"""ReportGenerator.generate_report with isolated _REPO."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _minimal_validation_report(*, with_critical_fail: bool = False) -> dict:
    results = [
        {
            "check_id": "week3.doc_id.uuid",
            "column_name": "doc_id",
            "check_type": "format",
            "status": "PASS",
            "actual_value": "ok",
            "expected": "uuid",
            "severity": "LOW",
            "records_failing": 0,
            "sample_failing": [],
            "message": "ok",
        }
    ]
    if with_critical_fail:
        results.append(
            {
                "check_id": "week3.extracted_facts.confidence.range",
                "column_name": "confidence",
                "check_type": "range",
                "status": "FAIL",
                "actual_value": "bad",
                "expected": "0-1",
                "severity": "CRITICAL",
                "records_failing": 3,
                "sample_failing": ["f1"],
                "message": "out of range",
            }
        )
    return {
        "report_id": "r1",
        "contract_id": "week3-document-refinery-extractions",
        "total_checks": len(results),
        "passed": sum(1 for r in results if r["status"] == "PASS"),
        "failed": sum(1 for r in results if r["status"] == "FAIL"),
        "warned": 0,
        "errored": 0,
        "results": results,
    }


def test_generate_report_writes_report_data_and_core_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("contracts.report_generator._REPO", tmp_path)
    (tmp_path / "validation_reports").mkdir(parents=True)
    (tmp_path / "violation_log").mkdir(parents=True)
    (tmp_path / "enforcer_report").mkdir(parents=True)

    w3 = tmp_path / "validation_reports" / "week3_unit.json"
    w5 = tmp_path / "validation_reports" / "week5_unit.json"
    w3.write_text(json.dumps(_minimal_validation_report()), encoding="utf-8")
    w5.write_text(json.dumps(_minimal_validation_report()), encoding="utf-8")

    ai = tmp_path / "validation_reports" / "ai_metrics.json"
    ai.write_text(
        json.dumps(
            {
                "embedding_drift": {"status": "PASS", "drift_score": 0.01},
                "trend": "stable",
                "violation_rate": 0.0,
                "baseline_violation_rate": 0.0,
                "status": "PASS",
            }
        ),
        encoding="utf-8",
    )

    missing_violations = tmp_path / "violation_log" / "none.jsonl"

    from contracts.report_generator import generate_report

    out = generate_report(
        week3_report=w3,
        week5_report=w5,
        ai_metrics_path=ai,
        violations_path=missing_violations,
        strict_pdf=False,
    )

    for key in (
        "data_health_score",
        "executive_summary",
        "violations_this_week",
        "schema_changes_detected",
        "ai_system_risk_assessment",
        "recommended_actions",
    ):
        assert key in out

    written = tmp_path / "enforcer_report" / "report_data.json"
    assert written.is_file()
    disk = json.loads(written.read_text(encoding="utf-8"))
    assert disk.get("data_health_score") == out["data_health_score"]
