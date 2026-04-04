"""Smoke tests: registry, runner policy, validation report shape."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_load_subscriptions_minimum() -> None:
    from contracts.registry import load_subscriptions

    subs = load_subscriptions(ROOT)
    assert len(subs) >= 4
    for s in subs:
        assert "contract_id" in s
        assert "subscriber_id" in s
        assert isinstance(s.get("breaking_fields"), list)


def test_pipeline_should_block_modes() -> None:
    from contracts.runner import pipeline_should_block

    report = {"results": [{"status": "FAIL", "severity": "CRITICAL", "check_id": "x"}]}
    assert pipeline_should_block(report, "AUDIT") is False
    assert pipeline_should_block(report, "WARN") is True
    assert pipeline_should_block(report, "ENFORCE") is True

    report_high = {"results": [{"status": "FAIL", "severity": "HIGH", "check_id": "y"}]}
    assert pipeline_should_block(report_high, "WARN") is False
    assert pipeline_should_block(report_high, "ENFORCE") is True


def test_week3_validation_report_schema() -> None:
    from contracts.runner import run_validation

    contract = ROOT / "generated_contracts" / "week3_extractions.yaml"
    data = ROOT / "outputs" / "week3" / "extractions.jsonl"
    if not contract.exists() or not data.exists():
        pytest.skip("fixture paths missing")
    report = run_validation(contract.resolve(), data.resolve(), None, ROOT)
    for key in (
        "report_id",
        "contract_id",
        "snapshot_id",
        "run_timestamp",
        "total_checks",
        "passed",
        "failed",
        "warned",
        "errored",
        "results",
    ):
        assert key in report
    assert isinstance(report["results"], list)
    for row in report["results"]:
        for k in (
            "check_id",
            "column_name",
            "check_type",
            "status",
            "severity",
            "records_failing",
            "message",
        ):
            assert k in row


def test_week3_latest_json_if_present() -> None:
    p = ROOT / "validation_reports" / "week3_latest.json"
    if not p.exists():
        pytest.skip("no committed week3_latest.json")
    report = json.loads(p.read_text(encoding="utf-8"))
    assert report.get("contract_id")
    assert isinstance(report.get("results"), list)


def test_breaking_field_match() -> None:
    from contracts.registry import breaking_field_matches_check

    assert breaking_field_matches_check("extracted_facts.confidence", "week3.extracted_facts.confidence.range")
    assert breaking_field_matches_check("payload", "week5.payload.jsonschema")
