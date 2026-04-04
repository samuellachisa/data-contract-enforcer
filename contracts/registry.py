"""
ContractRegistry (Tier 1): load contract_registry/subscriptions.yaml and query subscribers.

Tier-1 blast radius lists explicit downstream subscribers from YAML. ViolationAttributor merges
this with Tier-2 lineage-derived reachability; see explain_subscription_blast().
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_REPO_BOOT = Path(__file__).resolve().parents[1]
if str(_REPO_BOOT) not in sys.path:
    sys.path.insert(0, str(_REPO_BOOT))

import yaml

from contracts.common import load_yaml, repo_root

_VALIDATION_MODES = frozenset({"AUDIT", "WARN", "ENFORCE"})


def registry_path(root: Path) -> Path:
    return root / "contract_registry" / "subscriptions.yaml"


def load_subscriptions(root: Path, *, subscriptions_yaml: Path | None = None) -> list[dict[str, Any]]:
    p = subscriptions_yaml if subscriptions_yaml is not None else registry_path(root)
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
        "cross.ingest.",
        "cross.week2.",
        "cross.week4.",
        "week1.",
        "week2.",
        "week3.",
        "week4.",
        "week5.",
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
    cid = check_id.strip()
    if bf == "runner.schema.required" and cid.startswith("runner.schema.required."):
        return True
    if bf == "cross.ingest" and cid.startswith("cross.ingest."):
        return True
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
    *,
    subscriptions_yaml: Path | None = None,
) -> list[dict[str, Any]]:
    """
    Return subscriptions whose contract_id matches and whose breaking_fields
    are affected by check_id.
    """
    out: list[dict[str, Any]] = []
    for sub in load_subscriptions(root, subscriptions_yaml=subscriptions_yaml):
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


def discovered_contract_ids(root: Path) -> set[str]:
    """Contract `id` values from generated_contracts/*.yaml (after generator run)."""
    out: set[str] = set()
    gdir = root / "generated_contracts"
    if gdir.is_dir():
        for p in gdir.glob("*.yaml"):
            try:
                data = yaml.safe_load(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(data, dict) and data.get("id"):
                out.add(str(data["id"]))
    # Synthetic id from ValidationRunner.run_cross_system_validation (not a generated Bitol file).
    out.add("cross-system-dependencies")
    return out


def validate_subscriptions(
    root: Path, *, require_known_contract_ids: bool = True
) -> tuple[bool, list[str]]:
    """
    Validate subscriptions.yaml shape and optional cross-check against generated_contracts/*.yaml.
    Returns (ok, messages) where messages include notes and any validation errors.
    """
    lines: list[str] = []
    bad = 0

    known: set[str] = set()
    if require_known_contract_ids:
        known = discovered_contract_ids(root)
        if not known:
            lines.append(
                "registry: note: no generated_contracts/*.yaml ids found; contract_id cross-check skipped."
            )

    subs = load_subscriptions(root, subscriptions_yaml=None)
    if not subs:
        lines.append("registry: subscriptions list is empty or file missing.")
        return False, lines

    for i, sub in enumerate(subs):
        prefix = f"registry: subscription[{i}]"
        if not isinstance(sub, dict):
            lines.append(f"{prefix}: expected mapping, got {type(sub).__name__}.")
            bad += 1
            continue
        cid = str(sub.get("contract_id", "")).strip()
        if not cid:
            lines.append(f"{prefix}: missing contract_id.")
            bad += 1
        elif known and cid not in known:
            lines.append(f"{prefix}: contract_id {cid!r} not found in generated_contracts/*.yaml.")
            bad += 1
        sid = str(sub.get("subscriber_id", "")).strip()
        if not sid:
            lines.append(f"{prefix}: missing subscriber_id.")
            bad += 1
        team = str(sub.get("subscriber_team", "")).strip()
        if not team:
            lines.append(f"{prefix}: missing subscriber_team.")
            bad += 1
        fc = sub.get("fields_consumed")
        if not isinstance(fc, list) or not fc or not all(isinstance(x, str) and x.strip() for x in fc):
            lines.append(f"{prefix}: fields_consumed must be a non-empty list of non-empty strings.")
            bad += 1
        mode = str(sub.get("validation_mode", "")).strip().upper()
        if mode not in _VALIDATION_MODES:
            lines.append(
                f"{prefix}: validation_mode must be one of {sorted(_VALIDATION_MODES)}, got {sub.get('validation_mode')!r}."
            )
            bad += 1
        contact = str(sub.get("contact", "")).strip()
        if not contact:
            lines.append(f"{prefix}: missing contact.")
            bad += 1
        bfs = sub.get("breaking_fields")
        if not isinstance(bfs, list) or not bfs:
            lines.append(f"{prefix}: breaking_fields must be a non-empty list.")
            bad += 1
        else:
            for j, bf in enumerate(bfs):
                if not isinstance(bf, dict):
                    lines.append(f"{prefix}: breaking_fields[{j}] must be an object.")
                    bad += 1
                    continue
                fld = str(bf.get("field", "")).strip()
                if not fld:
                    lines.append(f"{prefix}: breaking_fields[{j}].field is required.")
                    bad += 1
                reason = str(bf.get("reason", "")).strip()
                if not reason:
                    lines.append(f"{prefix}: breaking_fields[{j}].reason is required.")
                    bad += 1

    return bad == 0, lines


def explain_subscription_blast(sub: dict[str, Any]) -> str:
    """Plain-language note: Tier-1 registry vs Tier-2 lineage blast radius."""
    sid = sub.get("subscriber_id", "?")
    cid = sub.get("contract_id", "?")
    fc = sub.get("fields_consumed") or []
    return (
        f"Tier-1 registry: consumer `{sid}` depends on contract `{cid}` (fields consumed: {list(fc)}). "
        "ViolationAttributor augments blast radius using lineage graphs (Tier-2) for affected files and pipelines."
    )


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


def main() -> None:
    ok, lines = validate_subscriptions(repo_root())
    for line in lines:
        print(line, file=sys.stderr if not ok else sys.stdout)
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
