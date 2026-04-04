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
        "data_health_score_breakdown",
        "executive_summary",
        "violations_this_week",
        "violations_pagination",
        "schema_changes_detected",
        "ai_system_risk_assessment",
        "recommended_actions",
    ):
        assert key in out

    written = tmp_path / "enforcer_report" / "report_data.json"
    assert written.is_file()
    disk = json.loads(written.read_text(encoding="utf-8"))
    assert disk.get("data_health_score") == out["data_health_score"]


def test_load_json_explicit_or_glob_invalid_json_returns_none(tmp_path: Path) -> None:
    from contracts.report_generator import _load_json_explicit_or_glob

    bad = tmp_path / "not_json.json"
    bad.write_text("{broken", encoding="utf-8")
    data, src = _load_json_explicit_or_glob(bad, str(tmp_path / "nope_*.json"))
    assert data is None
    assert src is not None


def test_load_json_explicit_or_glob_non_dict_root_returns_none(tmp_path: Path) -> None:
    from contracts.report_generator import _load_json_explicit_or_glob

    p = tmp_path / "array.json"
    p.write_text("[1, 2]", encoding="utf-8")
    data, _src = _load_json_explicit_or_glob(p, str(tmp_path / "x_*.json"))
    assert data is None


def test_generate_report_survives_malformed_week3_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("contracts.report_generator._REPO", tmp_path)
    (tmp_path / "validation_reports").mkdir(parents=True)
    (tmp_path / "violation_log").mkdir(parents=True)
    (tmp_path / "enforcer_report").mkdir(parents=True)

    w3 = tmp_path / "validation_reports" / "week3_bad.json"
    w3.write_text("NOT JSON {{{", encoding="utf-8")
    w5 = tmp_path / "validation_reports" / "week5_ok.json"
    w5.write_text(json.dumps(_minimal_validation_report()), encoding="utf-8")
    ai = tmp_path / "validation_reports" / "ai_metrics.json"
    ai.write_text(json.dumps({"status": "PASS", "embedding_drift": {"status": "PASS"}}), encoding="utf-8")

    from contracts.report_generator import generate_report

    out = generate_report(
        week3_report=w3,
        week5_report=w5,
        ai_metrics_path=ai,
        violations_path=tmp_path / "violation_log" / "missing.jsonl",
        strict_pdf=False,
    )
    assert "data_health_score" in out
    assert "executive_summary" in out
    assert isinstance(out.get("violations_this_week"), list)


def test_generate_report_survives_results_not_a_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("contracts.report_generator._REPO", tmp_path)
    (tmp_path / "validation_reports").mkdir(parents=True)
    (tmp_path / "violation_log").mkdir(parents=True)
    (tmp_path / "enforcer_report").mkdir(parents=True)

    bad_rep = {
        "report_id": "r-bad",
        "contract_id": "week3-document-refinery-extractions",
        "total_checks": 0,
        "passed": 0,
        "failed": 0,
        "warned": 0,
        "errored": 0,
        "results": "this_should_be_a_list",
    }
    w3 = tmp_path / "validation_reports" / "week3_weird.json"
    w3.write_text(json.dumps(bad_rep), encoding="utf-8")
    w5 = tmp_path / "validation_reports" / "week5_ok.json"
    w5.write_text(json.dumps(_minimal_validation_report()), encoding="utf-8")
    ai = tmp_path / "validation_reports" / "ai_metrics.json"
    ai.write_text(json.dumps({"status": "PASS", "embedding_drift": {"status": "PASS"}}), encoding="utf-8")

    from contracts.report_generator import generate_report

    out = generate_report(
        week3_report=w3,
        week5_report=w5,
        ai_metrics_path=ai,
        violations_path=tmp_path / "violation_log" / "missing.jsonl",
        strict_pdf=False,
    )
    assert out["data_health_score"] is not None


def test_generate_report_violations_pagination(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("contracts.report_generator._REPO", tmp_path)
    (tmp_path / "validation_reports").mkdir(parents=True)
    (tmp_path / "violation_log").mkdir(parents=True)
    (tmp_path / "enforcer_report").mkdir(parents=True)

    w3 = tmp_path / "validation_reports" / "week3_unit.json"
    w5 = tmp_path / "validation_reports" / "week5_unit.json"
    w3.write_text(json.dumps(_minimal_validation_report()), encoding="utf-8")
    w5.write_text(json.dumps(_minimal_validation_report()), encoding="utf-8")

    vlog = tmp_path / "violation_log" / "many.jsonl"
    lines = []
    for i in range(5):
        lines.append(
            json.dumps(
                {
                    "violation_id": f"id-{i}",
                    "type": "contract_violation",
                    "check_id": f"week5.fake_check_{i}",
                    "severity": "MEDIUM",
                    "message": f"issue {i}",
                    "records_failing": 10 - i,
                }
            )
        )
    vlog.write_text("\n".join(lines) + "\n", encoding="utf-8")

    ai = tmp_path / "validation_reports" / "ai_metrics.json"
    ai.write_text(json.dumps({"status": "PASS", "embedding_drift": {"status": "PASS"}}), encoding="utf-8")

    from contracts.report_generator import generate_report

    p0 = generate_report(
        week3_report=w3,
        week5_report=w5,
        ai_metrics_path=ai,
        violations_path=vlog,
        violations_page=0,
        violations_page_size=2,
        strict_pdf=False,
    )
    assert p0["violations_pagination"]["total_violations"] == 5
    assert p0["violations_pagination"]["total_pages"] == 3
    assert len(p0["violations_this_week"]) == 2

    p1 = generate_report(
        week3_report=w3,
        week5_report=w5,
        ai_metrics_path=ai,
        violations_path=vlog,
        violations_page=2,
        violations_page_size=2,
        strict_pdf=False,
    )
    assert len(p1["violations_this_week"]) == 1
    assert p1["violations_pagination"]["page"] == 2


def test_generate_report_health_type_weight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("contracts.report_generator._REPO", tmp_path)
    (tmp_path / "validation_reports").mkdir(parents=True)
    (tmp_path / "violation_log").mkdir(parents=True)
    (tmp_path / "enforcer_report").mkdir(parents=True)

    w3 = tmp_path / "validation_reports" / "week3_unit.json"
    w3.write_text(json.dumps(_minimal_validation_report(with_critical_fail=True)), encoding="utf-8")
    w5 = tmp_path / "validation_reports" / "week5_unit.json"
    w5.write_text(json.dumps(_minimal_validation_report()), encoding="utf-8")

    ai = tmp_path / "validation_reports" / "ai_metrics.json"
    ai.write_text(json.dumps({"status": "PASS", "embedding_drift": {"status": "PASS"}}), encoding="utf-8")

    from contracts.report_generator import generate_report

    baseline = generate_report(
        week3_report=w3,
        week5_report=w5,
        ai_metrics_path=ai,
        violations_path=tmp_path / "violation_log" / "none.jsonl",
        health_type_weights=None,
        strict_pdf=False,
    )
    weighted = generate_report(
        week3_report=w3,
        week5_report=w5,
        ai_metrics_path=ai,
        violations_path=tmp_path / "violation_log" / "none.jsonl",
        health_type_weights={"week3.": 2.0},
        strict_pdf=False,
    )
    assert baseline["data_health_score"] == 80.0
    assert weighted["data_health_score"] == 60.0
    rows = weighted["data_health_score_breakdown"]["per_failing_check"]
    assert len(rows) == 1
    assert rows[0]["type_weight"] == 2.0
    assert rows[0]["applied_deduction"] == 40.0
