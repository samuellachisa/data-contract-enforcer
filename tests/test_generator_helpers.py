"""Unit tests for ContractGenerator helper functions."""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from contracts.generator import (  # noqa: E402
    _ingest_block,
    _numeric_stats,
    _week3_ingest_anomalies,
    _week5_ingest_anomalies,
    persist_generator_numeric_baselines,
    suspicious_unit_interval_mean_notes,
)


def test_numeric_stats_empty() -> None:
    assert _numeric_stats([]) == {}


def test_numeric_stats_known_values() -> None:
    s = _numeric_stats([1.0, 2.0, 3.0, 4.0])
    assert s["min"] == 1.0
    assert s["max"] == 4.0
    assert s["mean"] == 2.5
    assert math.isfinite(s["stddev"])


def test_ingest_block_counts() -> None:
    issues = [{"line_no": 1, "kind": "json_decode"}, {"line_no": 2, "kind": "not_object"}]
    block = _ingest_block(issues, accepted=10)
    assert block["jsonl_lines_accepted"] == 10
    assert block["jsonl_lines_rejected"] == 2
    assert len(block["issue_sample"]) <= 8


def test_week3_ingest_anomalies_wrong_extracted_facts_type() -> None:
    rows = [{"doc_id": "a", "extracted_facts": "not-a-list"}]
    a = _week3_ingest_anomalies(rows)
    assert a["extracted_facts_wrong_type_row_count"] == 1


def test_week3_ingest_anomalies_non_numeric_confidence() -> None:
    rows = [
        {
            "doc_id": "a",
            "extracted_facts": [{"fact_id": "f1", "confidence": "high"}],
        }
    ]
    a = _week3_ingest_anomalies(rows)
    assert a["extracted_facts_confidence_non_numeric_count"] == 1


def test_week5_ingest_anomalies() -> None:
    rows = [
        {"payload": "bad", "sequence_number": "1", "metadata": []},
    ]
    a = _week5_ingest_anomalies(rows)
    assert a["payload_non_object_count"] == 1
    assert a["sequence_number_non_int_count"] == 1
    assert a["metadata_non_object_count"] == 1


def test_week5_ingest_anomalies_clean() -> None:
    rows = [{"payload": {}, "sequence_number": 1, "metadata": {}}]
    a = _week5_ingest_anomalies(rows)
    assert a["payload_non_object_count"] == 0
    assert a["sequence_number_non_int_count"] == 0
    assert a["metadata_non_object_count"] == 0


def test_suspicious_unit_interval_mean_notes_near_zero() -> None:
    prof = {"mean": 0.02, "min": 0.0, "max": 0.5}
    notes = suspicious_unit_interval_mean_notes(prof, margin=0.05)
    assert len(notes) == 1
    assert "within 0.05 of 0" in notes[0]
    assert "0.0200" in notes[0]


def test_suspicious_unit_interval_mean_notes_near_one() -> None:
    prof = {"mean": 0.98, "min": 0.5, "max": 1.0}
    notes = suspicious_unit_interval_mean_notes(prof, margin=0.05)
    assert len(notes) == 1
    assert "within 0.05 of 1" in notes[0]


def test_suspicious_unit_interval_mean_notes_not_triggered_midrange() -> None:
    prof = {"mean": 0.5, "min": 0.1, "max": 0.9}
    assert suspicious_unit_interval_mean_notes(prof) == []


def test_suspicious_unit_interval_skips_non_unit_fields() -> None:
    prof = {"mean": 0.01, "min": 0.0, "max": 100.0}
    assert suspicious_unit_interval_mean_notes(prof) == []


def test_persist_generator_numeric_baselines_writes_file(tmp_path: Path) -> None:
    snap = tmp_path / "schema_snapshots"
    snap.mkdir(parents=True)
    # repo_root for baselines is the project; point load/save at tmp_path by patching would need
    # inject root — persist_generator_numeric_baselines takes root explicitly.
    written = persist_generator_numeric_baselines(
        tmp_path,
        "demo-contract",
        {"score": {"mean": 0.5, "stddev": 0.1, "min": 0.0, "max": 1.0}},
    )
    assert "score" in written
    assert written["score"]["mean"] == 0.5
    assert written["score"]["stddev"] == 0.1
    assert written["score"]["std"] >= 0.1
    p = snap / "baselines.json"
    assert p.is_file()
    import json

    data = json.loads(p.read_text(encoding="utf-8"))
    key = "demo-contract::generator.numeric.score"
    assert key in data
    assert data[key]["source"] == "contract_generator"
