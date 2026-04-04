"""
ContractRegistry (Tier 1): load contract_registry/subscriptions.yaml and query subscribers.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from contracts.common import load_yaml


def registry_path(root: Path) -> Path:
    return root / "contract_registry" / "subscriptions.yaml"


def load_subscriptions(root: Path) -> list[dict[str, Any]]:
    p = registry_path(root)
    if not p.exists():
        return []
    data = load_yaml(p)
    if not isinstance(data, dict):
        return []
    subs = data.get("subscriptions")
    return list(subs) if isinstance(subs, list) else []


def _check_id_tail(check_id: str) -> str:
    """Strip common week/cross prefixes so we can match breaking field paths."""
    s = check_id.strip()
    for prefix in (
        "week1.",
        "week2.",
        "week3.",
        "week4.",
        "week5.",
        "cross.week2.",
        "cross.week4.",
        "langsmith.",
    ):
        if s.startswith(prefix):
            return s[len(prefix) :]
    return s


def breaking_field_matches_check(breaking_field: str, check_id: str) -> bool:
    """
    True if this subscription breaking field is implicated by the validation check_id.
    """
    bf = breaking_field.strip()
    if not bf:
        return False
    tail = _check_id_tail(check_id)
    if tail == bf or tail.startswith(bf + "."):
        return True
    # e.g. check edges.source validity — still relates to edges
    if bf.split(".")[0] and tail.startswith(bf.split(".")[0] + "."):
        return bf in tail or tail.startswith(bf.split(".")[0] + ".")
    return bf in check_id


def subscribers_for_violation(
    root: Path,
    source_contract_id: str,
    check_id: str,
) -> list[dict[str, Any]]:
    """
    Return subscriptions whose contract_id matches and whose breaking_fields
    are affected by check_id.
    """
    out: list[dict[str, Any]] = []
    for sub in load_subscriptions(root):
        if str(sub.get("contract_id", "")) != source_contract_id:
            continue
        bfs = sub.get("breaking_fields") or []
        matched: list[dict[str, Any]] = []
        for bf in bfs:
            if not isinstance(bf, dict):
                continue
            fld = str(bf.get("field", ""))
            if breaking_field_matches_check(fld, check_id):
                matched.append(bf)
        if matched:
            row = dict(sub)
            row["_matched_breaking_fields"] = matched
            out.append(row)
    return out


def subscriber_summary_entries(subscribers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Serializable blast_radius.subscribers list."""
    rows: list[dict[str, Any]] = []
    for s in subscribers:
        mbf = s.get("_matched_breaking_fields") or []
        rows.append(
            {
                "subscriber_id": s.get("subscriber_id"),
                "subscriber_team": s.get("subscriber_team"),
                "fields_consumed": s.get("fields_consumed", []),
                "matched_breaking_fields": [{"field": x.get("field"), "reason": x.get("reason")} for x in mbf],
                "validation_mode": s.get("validation_mode"),
                "contact": s.get("contact"),
            }
        )
    return rows
