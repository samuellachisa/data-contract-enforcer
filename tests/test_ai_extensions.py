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


def test_merge_ai_extension_config_overrides() -> None:
    from contracts.ai_extensions import AIExtensionConfig, merge_ai_extension_config

    base = AIExtensionConfig()
    merged = merge_ai_extension_config(
        base,
        embedding_drift_threshold=0.99,
        embedding_sample_size=10,
        prompt_preview_max_length=100,
    )
    assert merged.embedding_drift_threshold == 0.99
    assert merged.embedding_sample_size == 10
    assert merged.prompt_preview_max_length == 100
    assert merged.llm_violation_trend_multiplier == base.llm_violation_trend_multiplier


def test_check_prompt_input_schema_echoes_preview_max_length(iso_repo: Path) -> None:
    from contracts.ai_extensions import AIExtensionConfig, check_prompt_input_schema

    cfg = AIExtensionConfig(prompt_preview_max_length=7)
    rows = [
        {
            "doc_id": "doc-1",
            "source_path": "/data/doc.pdf",
            "extracted_facts": [{"text": "1234567890", "confidence": 0.5}],
        }
    ]
    out = check_prompt_input_schema(rows, config=cfg)
    assert out["status"] == "PASS"
    assert out["content_preview_max_length"] == 7


def test_write_prompt_input_schema_file_uses_config_max_length(iso_repo: Path) -> None:
    from contracts.ai_extensions import AIExtensionConfig, _write_prompt_input_schema_file

    cfg = AIExtensionConfig(prompt_preview_max_length=123)
    _write_prompt_input_schema_file(cfg)
    path = iso_repo / "generated_contracts" / "prompt_inputs" / "week3_extraction_prompt_input.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["properties"]["content_preview"]["maxLength"] == 123


def test_validate_llm_output_schema_trend_stable_with_strict_thresholds(iso_repo: Path) -> None:
    from contracts.ai_extensions import AIExtensionConfig, validate_llm_output_schema

    baseline_path = iso_repo / "schema_snapshots" / "llm_violation_baseline.json"
    baseline_path.write_text(json.dumps({"baseline_violation_rate": 1.0}), encoding="utf-8")

    bad = dict(_minimal_valid_verdict())
    bad["scores"] = {"c1": {"score": 99, "evidence": [], "notes": ""}}
    cfg = AIExtensionConfig(llm_violation_trend_multiplier=1.5, llm_violation_trend_min_delta=0.001)
    r = validate_llm_output_schema([bad], config=cfg)
    assert r["violation_rate"] == 1.0
    assert r["trend"] == "stable"
    assert r["status"] == "PASS"


def test_build_ai_monitoring_snapshot_shape(iso_repo: Path) -> None:
    from contracts.ai_extensions import AIExtensionConfig, build_ai_monitoring_snapshot

    cfg = AIExtensionConfig()
    snap = build_ai_monitoring_snapshot(
        embedding={"drift_score": 0.1, "status": "PASS", "threshold": 0.15},
        prompt={"quarantined_count": 0, "status": "PASS"},
        llm={"violation_rate": 0.0, "baseline_violation_rate": 0.0, "schema_violations": 0, "total_outputs": 1, "status": "PASS", "trend": "stable"},
        traces={"checks_failed": 0, "total_traces": 0, "status": "PASS"},
        config=cfg,
    )
    assert snap["kind"] == "ai_contract_extensions"
    assert "gauges" in snap and "states_numeric" in snap
    assert snap["config_echo"]["embedding_drift_threshold"] == cfg.embedding_drift_threshold


def test_main_writes_metrics_and_monitoring(iso_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from contracts import ai_extensions

    (iso_repo / "outputs" / "week3").mkdir(parents=True)
    (iso_repo / "outputs" / "week2").mkdir(parents=True)
    ext = iso_repo / "outputs" / "week3" / "extractions.jsonl"
    ver = iso_repo / "outputs" / "week2" / "verdicts.jsonl"
    ext.write_text(
        json.dumps(
            {
                "doc_id": "d1",
                "source_path": "/p",
                "extracted_facts": [{"text": "hello world from test corpus", "confidence": 0.9}],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ver.write_text(json.dumps(_minimal_valid_verdict()) + "\n", encoding="utf-8")

    metrics_out = iso_repo / "validation_reports" / "m.json"
    mon_out = iso_repo / "validation_reports" / "mon.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ai_extensions",
            "--output",
            str(metrics_out),
            "--monitoring-output",
            str(mon_out),
        ],
    )
    ai_extensions.MONITORING_HOOKS.clear()
    ai_extensions.main()
    assert metrics_out.is_file()
    body = json.loads(metrics_out.read_text(encoding="utf-8"))
    assert "ai_extension_config" in body
    assert mon_out.is_file()
    mon = json.loads(mon_out.read_text(encoding="utf-8"))
    assert mon["gauges"]["ai_embedding_drift_score"] == 0.0


def test_main_skips_monitoring_when_disabled(iso_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from contracts import ai_extensions

    (iso_repo / "outputs" / "week3").mkdir(parents=True)
    (iso_repo / "outputs" / "week2").mkdir(parents=True)
    ext = iso_repo / "outputs" / "week3" / "extractions.jsonl"
    ver = iso_repo / "outputs" / "week2" / "verdicts.jsonl"
    ext.write_text(
        json.dumps(
            {
                "doc_id": "d1",
                "source_path": "/p",
                "extracted_facts": [{"text": "hello world from test corpus", "confidence": 0.9}],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ver.write_text(json.dumps(_minimal_valid_verdict()) + "\n", encoding="utf-8")

    default_mon = iso_repo / "validation_reports" / "ai_monitoring_metrics.json"
    monkeypatch.setenv("CONTRACT_AI_MONITORING_DISABLE", "1")
    monkeypatch.setattr(sys, "argv", ["ai_extensions", "--output", str(iso_repo / "validation_reports" / "x.json")])
    ai_extensions.MONITORING_HOOKS.clear()
    ai_extensions.main()
    assert not default_mon.exists()


def test_monitoring_hook_receives_payload(iso_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from contracts import ai_extensions

    received: list[dict] = []

    def _hook(payload: dict) -> None:
        received.append(payload)

    (iso_repo / "outputs" / "week3").mkdir(parents=True)
    (iso_repo / "outputs" / "week2").mkdir(parents=True)
    ext = iso_repo / "outputs" / "week3" / "extractions.jsonl"
    ver = iso_repo / "outputs" / "week2" / "verdicts.jsonl"
    ext.write_text(
        json.dumps(
            {
                "doc_id": "d1",
                "source_path": "/p",
                "extracted_facts": [{"text": "hello world from test corpus", "confidence": 0.9}],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    ver.write_text(json.dumps(_minimal_valid_verdict()) + "\n", encoding="utf-8")

    monkeypatch.setattr(sys, "argv", ["ai_extensions", "--output", str(iso_repo / "validation_reports" / "y.json")])
    ai_extensions.MONITORING_HOOKS.clear()
    ai_extensions.register_ai_monitoring_hook(_hook)
    ai_extensions.main()
    assert len(received) == 1
    assert received[0]["schema_version"] == "1"
