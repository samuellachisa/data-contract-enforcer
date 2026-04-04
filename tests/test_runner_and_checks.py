"""ValidationRunner helpers, drift thresholds, pipeline policy."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from contracts.common import CheckResult  # noqa: E402
from contracts.runner import pipeline_should_block, validate_generic_missing_column  # noqa: E402
from contracts.validation_checks import _apply_numeric_mean_drift  # noqa: E402


def test_validate_generic_missing_column_detects_absent_key() -> None:
    contract = {
        "schema": {
            "doc_id": {"type": "string", "required": True},
            "optional_field": {"type": "string", "required": False},
        }
    }
    rows = [{"optional_field": "x"}]
    out = validate_generic_missing_column(contract, rows)
    assert any(r.check_id == "runner.schema.required.doc_id" and r.status == "FAIL" for r in out)


def test_validate_generic_missing_column_pass_when_present() -> None:
    contract = {"schema": {"doc_id": {"type": "string", "required": True}}}
    rows = [{"doc_id": "abc"}]
    out = validate_generic_missing_column(contract, rows)
    row = next(r for r in out if r.check_id == "runner.schema.required.doc_id")
    assert row.status == "PASS"


def test_apply_numeric_mean_drift_establish_then_pass(tmp_path: Path) -> None:
    snap = tmp_path / "schema_snapshots"
    snap.mkdir(parents=True)

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
                sample_failing=samples,
                message=msg,
            )
        )

    vals = [10.0, 10.0, 10.0]
    _apply_numeric_mean_drift(
        tmp_path,
        "test-contract",
        "metric.mean",
        vals,
        add,
        "test.metric.drift_baseline",
        "test.metric.statistical_drift",
        "metric",
    )
    assert any(c.check_id == "test.metric.drift_baseline" for c in results)
    assert results[-1].status == "PASS"

    results.clear()
    _apply_numeric_mean_drift(
        tmp_path,
        "test-contract",
        "metric.mean",
        vals,
        add,
        "test.metric.drift_baseline",
        "test.metric.statistical_drift",
        "metric",
    )
    drift = next(c for c in results if c.check_id == "test.metric.statistical_drift")
    assert drift.status == "PASS"


def test_apply_numeric_mean_drift_warn_and_fail_sigma(tmp_path: Path) -> None:
    snap = tmp_path / "schema_snapshots"
    snap.mkdir(parents=True)
    baselines = {
        "demo::x.mean": {"mean": 100.0, "std": 1.0},
    }
    (snap / "baselines.json").write_text(json.dumps(baselines), encoding="utf-8")

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
                sample_failing=samples,
                message=msg,
            )
        )

    # mean 102.5 -> dev 2.5 sigma -> WARN
    _apply_numeric_mean_drift(
        tmp_path,
        "demo",
        "x.mean",
        [102.5, 102.5],
        add,
        "ignore",
        "drift.check",
        "x",
    )
    assert results[0].status == "WARN"

    results.clear()
    # mean 104 -> dev 4 sigma -> FAIL
    _apply_numeric_mean_drift(
        tmp_path,
        "demo",
        "x.mean",
        [104.0, 104.0],
        add,
        "ignore",
        "drift.check2",
        "x",
    )
    assert results[0].status == "FAIL"


def test_pipeline_should_block_ignores_warn_status() -> None:
    report = {"results": [{"status": "WARN", "severity": "CRITICAL", "check_id": "x"}]}
    assert pipeline_should_block(report, "WARN") is False
    assert pipeline_should_block(report, "ENFORCE") is False


def test_pipeline_should_block_fail_non_critical_not_warn_mode() -> None:
    report = {"results": [{"status": "FAIL", "severity": "MEDIUM", "check_id": "x"}]}
    assert pipeline_should_block(report, "WARN") is False
