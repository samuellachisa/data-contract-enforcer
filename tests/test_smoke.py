"""Smoke tests: registry, runner policy, validation report shape."""
from __future__ import annotations

import json
import sys
import textwrap
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


def test_load_jsonl_with_issues_skips_bad_lines(tmp_path: Path) -> None:
    from contracts.common import load_jsonl_with_issues

    p = tmp_path / "x.jsonl"
    p.write_text(
        '{"a": 1}\nnot-json\n"string-not-object"\n{"b": 2}\n',
        encoding="utf-8",
    )
    rows, issues = load_jsonl_with_issues(p)
    assert len(rows) == 2
    assert any(r.get("a") == 1 for r in rows)
    assert any(r.get("b") == 2 for r in rows)
    kinds = {i["kind"] for i in issues}
    assert "json_decode" in kinds
    assert "not_object" in kinds


def test_validate_subscriptions_empty_registry_fails(tmp_path: Path) -> None:
    from contracts.registry import validate_subscriptions

    reg = tmp_path / "contract_registry"
    reg.mkdir()
    (reg / "subscriptions.yaml").write_text("subscriptions: []\n", encoding="utf-8")
    ok, lines = validate_subscriptions(tmp_path)
    assert ok is False
    assert any("empty" in x.lower() for x in lines)


def test_validate_subscriptions_real_repo() -> None:
    from contracts.registry import validate_subscriptions

    ok, lines = validate_subscriptions(ROOT)
    assert ok is True
    assert isinstance(lines, list)


def test_run_validation_jsonl_ingest_errors(tmp_path: Path) -> None:
    from contracts.runner import run_validation

    data = tmp_path / "data.jsonl"
    data.write_text('{"doc_id": "550e8400-e29b-41d4-a716-446655440000"}\nBOGUS\n', encoding="utf-8")
    contract_path = tmp_path / "c.yaml"
    contract_path.write_text(
        textwrap.dedent(
            """
            id: smoke-unknown-contract
            schema:
              doc_id:
                type: string
                required: true
            """
        ).strip(),
        encoding="utf-8",
    )
    report = run_validation(contract_path, data, None, ROOT)
    ingest = [r for r in report["results"] if r.get("check_type") == "ingest"]
    assert ingest and ingest[0]["status"] == "ERROR"
    assert "runner.unsupported_contract" in {r.get("check_id") for r in report["results"]}


def test_schema_analyzer_nested_diff() -> None:
    from contracts.schema_analyzer import _diff_schemas

    a = {
        "root": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"c": {"type": "number", "required": False}},
            },
        }
    }
    b = {
        "root": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"c": {"type": "string", "required": False}},
            },
        }
    }
    d = _diff_schemas(a, b)
    assert d["compatibility_verdict"] == "BREAKING"
    fields = " ".join(str(c.get("field")) for c in d["breaking_changes"])
    assert "properties.c" in fields
