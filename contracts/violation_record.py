"""
Week 8–compatible violation record shape. Use when emitting new violations from tooling.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def normalize_violation_record(
    *,
    violation_id: str,
    check_id: str,
    violation_type: str,
    source_contract_id: str,
    message: str,
    records_failing: int = 0,
    severity: str = "HIGH",
    blame_hint: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "violation_id": violation_id,
        "type": violation_type,
        "check_id": check_id,
        "detected_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "severity": severity,
        "source_contract_id": source_contract_id,
        "message": message,
        "records_failing": records_failing,
        "sentinel_ingest_version": "1.0",
    }
    if blame_hint:
        row["blame_hint"] = blame_hint
    if extra:
        row.update(extra)
    return row
