"""SchemaEvolutionAnalyzer migration helpers."""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from contracts.schema_analyzer import _migration_impact_report, _parse_since  # noqa: E402


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
    mi = _migration_impact_report(diff, "week5-event-sourcing-events")
    assert mi["compatibility_verdict"] == "BREAKING"
    assert mi["breaking_fields"]
    assert mi["migration_checklist"]
    assert len(mi["rollback_plan"]) > 10
