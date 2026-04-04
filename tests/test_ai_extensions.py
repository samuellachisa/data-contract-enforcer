"""AI extensions tests with isolated _REPO (tmp_path)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def iso_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Minimal repo layout for ai_extensions path resolution."""
    (tmp_path / "schema_snapshots").mkdir(parents=True)
    (tmp_path / "violation_log").mkdir(parents=True)
    (tmp_path / "outputs" / "quarantine").mkdir(parents=True)
    monkeypatch.setattr("contracts.ai_extensions._REPO", tmp_path)
    monkeypatch.setenv("EMBEDDING_OFF", "1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    return tmp_path


def test_check_embedding_drift_no_texts_warns(iso_repo: Path) -> None:
    from contracts.ai_extensions import check_embedding_drift

    out = check_embedding_drift([])
    assert out["status"] == "WARN"
    assert out.get("reason") == "no texts"


def test_check_embedding_drift_establishes_baseline(iso_repo: Path) -> None:
    from contracts.ai_extensions import check_embedding_drift

    extractions = [
        {
            "doc_id": "d1",
            "extracted_facts": [{"text": "hello world from test corpus", "confidence": 0.9}],
        }
    ]
    out = check_embedding_drift(extractions)
    assert out["status"] == "PASS"
    assert out.get("baseline_created") is True
    assert (iso_repo / "schema_snapshots" / "embedding_baselines.npz").is_file()


def test_check_prompt_input_schema_pass(iso_repo: Path) -> None:
    from contracts.ai_extensions import check_prompt_input_schema

    rows = [
        {
            "doc_id": "doc-1",
            "source_path": "/data/doc.pdf",
            "extracted_facts": [{"text": "preview text", "confidence": 0.5}],
        }
    ]
    out = check_prompt_input_schema(rows)
    assert out["status"] == "PASS"
    assert out["quarantined_count"] == 0


def test_check_prompt_input_schema_quarantine(iso_repo: Path) -> None:
    from contracts.ai_extensions import check_prompt_input_schema

    rows = [
        {
            "doc_id": "doc-1",
            # missing source_path -> invalid prompt_input record
            "extracted_facts": [{"text": "x"}],
        }
    ]
    out = check_prompt_input_schema(rows)
    assert out["status"] == "FAIL"
    assert out["quarantined_count"] == 1
    qdir = iso_repo / "outputs" / "quarantine"
    assert any(p.name.startswith("prompt_inputs_quarantine_") for p in qdir.glob("*.jsonl"))


def _minimal_valid_verdict() -> dict:
    return {
        "verdict_id": "v1",
        "target_ref": "ref",
        "rubric_id": "rub",
        "rubric_version": "1.0.0",
        "scores": {
            "c1": {"score": 3, "evidence": ["e"], "notes": "n"},
        },
        "overall_verdict": "PASS",
        "overall_score": 3.0,
        "confidence": 0.9,
        "evaluated_at": "2026-01-01T00:00:00Z",
    }


def test_validate_llm_output_schema_pass(iso_repo: Path) -> None:
    from contracts.ai_extensions import validate_llm_output_schema

    r = validate_llm_output_schema([_minimal_valid_verdict()])
    assert r["schema_violations"] == 0
    assert r["violation_rate"] == 0.0


def test_validate_llm_output_schema_invalid_appends_violation(iso_repo: Path) -> None:
    from contracts.ai_extensions import validate_llm_output_schema

    bad = dict(_minimal_valid_verdict())
    bad["scores"] = {"c1": {"score": 99, "evidence": [], "notes": ""}}  # score out of range

    vlog = iso_repo / "violation_log" / "violations.jsonl"
    vlog.write_text("# header\n", encoding="utf-8")

    r = validate_llm_output_schema([bad])
    assert r["schema_violations"] >= 1
    assert r["violation_rate"] > 0
    text = vlog.read_text(encoding="utf-8")
    assert "llm_output_schema" in text or "week2.verdict_record.schema" in text
