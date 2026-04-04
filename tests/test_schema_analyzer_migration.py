"""SchemaEvolutionAnalyzer migration helpers."""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from contracts.schema_analyzer import (  # noqa: E402
    _diff_schemas,
    _migration_impact_report,
    _parse_since,
    classify_scale_change_critical,
    per_consumer_failure_modes,
    rollback_plan_sections,
)


def test_parse_since_days() -> None:
    assert _parse_since("14 days ago") == timedelta(days=14)
    assert _parse_since("1 days ago") == timedelta(days=1)


def test_parse_since_invalid_defaults_seven_days() -> None:
    assert _parse_since("nonsense") == timedelta(days=7)


def test_migration_impact_backward_compatible() -> None:
    diff = {"compatibility_verdict": "BACKWARD_COMPATIBLE"}
    mi = _migration_impact_report(diff, "cid")
    assert mi["compatibility_verdict"] == "BACKWARD_COMPATIBLE"
    assert mi["migration_checklist"] == []
    assert "rollback" in mi["rollback_plan"].lower()


def test_migration_impact_breaking_has_checklist_and_rollback() -> None:
    diff = {
        "compatibility_verdict": "BREAKING",
        "breaking_changes": [
            {"field": "payload.properties.bytes", "change_type": "Change type (breaking)"},
        ],
    }
    mi = _migration_impact_report(diff, "week5-event-sourcing-events", root=ROOT)
    assert mi["compatibility_verdict"] == "BREAKING"
    assert mi["breaking_fields"]
    assert mi["migration_checklist"]
    assert len(mi["rollback_plan"]) > 10
    assert "rollback_plan_sections" in mi
    assert mi["rollback_plan_sections"].get("immediate_actions")
    assert isinstance(mi.get("per_consumer_failure_modes"), list)


def test_classify_scale_change_critical_float_unit_to_int_hundred() -> None:
    old = {"type": "number", "minimum": 0.0, "maximum": 1.0}
    new = {"type": "integer", "minimum": 0, "maximum": 100}
    out = classify_scale_change_critical(old, new, "confidence")
    assert out is not None
    assert out["pattern"] == "UNIT_INTERVAL_TO_HUNDRED_SCALE"


def test_classify_scale_change_critical_same_type_number_bounds() -> None:
    old = {"type": "number", "minimum": 0, "maximum": 1}
    new = {"type": "number", "minimum": 0, "maximum": 100}
    assert classify_scale_change_critical(old, new, "x") is not None


def test_classify_scale_change_not_triggered_for_unrelated_type_change() -> None:
    old = {"type": "number", "minimum": 0, "maximum": 1}
    new = {"type": "string"}
    assert classify_scale_change_critical(old, new, "x") is None


def test_diff_schemas_emits_scale_change_critical() -> None:
    a = {"score": {"type": "number", "minimum": 0.0, "maximum": 1.0}}
    b = {"score": {"type": "integer", "minimum": 0, "maximum": 100}}
    d = _diff_schemas(a, b)
    assert any(x.get("classification") == "SCALE_CHANGE_CRITICAL" for x in d.get("breaking_changes", []))
    assert d.get("narrow_classifications")


def test_rollback_plan_sections_includes_scale_block_when_critical() -> None:
    diff = {
        "breaking_changes": [
            {"field": "c", "classification": "SCALE_CHANGE_CRITICAL", "severity": "CRITICAL"},
        ]
    }
    sec = rollback_plan_sections("week3-document-refinery-extractions", diff)
    assert sec.get("scale_change_specific")


def test_per_consumer_failure_modes_matches_week3_registry() -> None:
    diff = {
        "breaking_changes": [
            {"field": "extracted_facts.confidence.range", "change_type": "Change type (breaking)"},
        ]
    }
    modes = per_consumer_failure_modes("week3-document-refinery-extractions", diff, ROOT)
    assert modes
    sids = {m.get("subscriber_id") for m in modes}
    assert "week4-cartographer" in sids or "week7-data-contract-enforcer" in sids


def test_per_consumer_failure_modes_nested_path_matches_registry_clause() -> None:
    diff = {
        "breaking_changes": [
            {"field": "extracted_facts.properties.confidence", "change_type": "Change type (breaking)"},
        ]
    }
    modes = per_consumer_failure_modes("week3-document-refinery-extractions", diff, ROOT)
    triggered = [m for m in modes if m.get("registry_breaking_fields_triggered")]
    assert triggered, "expected at least one subscriber with matched registry clauses"
