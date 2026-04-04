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
