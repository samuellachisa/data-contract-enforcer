#!/usr/bin/env python3
"""
ViolationAttributor (Week 7).

Reads violation_log/violations.jsonl and enriches each violation with:
- blame_chain (ranked candidates, commit hash, author, timestamp, confidence_score)
- blast_radius (affected_nodes/pipelines + estimated_records)
- attribution_context (git work tree, lineage freshness warnings; see ATTRIBUTION_OPERATIONS.md)

Operational assumptions and monorepo notes: ``contracts/ATTRIBUTION_OPERATIONS.md``.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from contracts.registry import subscriber_summary_entries, subscribers_for_violation


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git_date_to_iso8601(ai: str) -> str:
    """Normalize git %ai (e.g. '2026-03-30 23:31:21 +0300') to ISO-8601 without mangling the timezone."""
    parts = ai.strip().split()
    if len(parts) >= 2:
        base = f"{parts[0]}T{parts[1]}"
        if len(parts) >= 3 and re.match(r"^[+-]\d{4}$", parts[2]):
            z = parts[2]
            return base + z[:3] + ":" + z[3:]
        return base
    return ai.strip()


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip().lstrip("\ufeff")
            if not line:
                continue
            if line.startswith("#") or line.startswith("//"):
                continue
            yield json.loads(line)


def _load_latest_lineage(root: Path, lineage_jsonl: Path | None = None) -> tuple[dict[str, Any] | None, Path | None]:
    """Last JSONL object is treated as the active snapshot (document freshness there, not file mtime)."""
    p = (lineage_jsonl.expanduser().resolve() if lineage_jsonl is not None else (root / "outputs" / "week4" / "lineage_snapshots.jsonl").resolve())
    if not p.exists():
        return None, None
    rows = list(_iter_jsonl(p))
    return (rows[-1] if rows else None), p


def violations_from_validation_report(report_path: Path) -> list[dict[str, Any]]:
    """Turn a ValidationRunner JSON report into attributor-ready violation rows (manual integration step)."""
    data = json.loads(report_path.read_text(encoding="utf-8"))
    contract_id = str(data.get("contract_id") or "")
    ts = str(data.get("run_timestamp") or _now_iso())
    out: list[dict[str, Any]] = []
    raw_results = data.get("results", [])
    if not isinstance(raw_results, list):
        return out
    for r in raw_results:
        if not isinstance(r, dict):
            continue
        if str(r.get("status")) not in {"FAIL", "ERROR"}:
            continue
        out.append(
            {
                "violation_id": str(uuid.uuid4()),
                "type": "contract_violation",
                "check_id": str(r.get("check_id", "")),
                "detected_at": ts,
                "message": str(r.get("message", "")),
                "source_contract_id": contract_id,
                "records_failing": int(r.get("records_failing") or 0),
                "severity": str(r.get("severity", "HIGH")),
                "blame_hint": {"file": "src/week3/extractor.py", "line_start": 1, "line_end": 120},
            }
        )
    return out


def _index_lineage(lineage: dict[str, Any] | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not lineage:
        return {}, []
    nodes = {n["node_id"]: n for n in lineage.get("nodes", [])}
    edges = lineage.get("edges", [])
    return nodes, edges


def _guess_failing_dataset(violation: dict[str, Any]) -> str:
    check_id = str(violation.get("check_id", ""))
    if check_id.startswith("week3."):
        return "week3"
    if check_id.startswith("week5."):
        return "week5"
    if check_id.startswith("week1."):
        return "week1"
    if check_id.startswith("week2.") or check_id.startswith("cross.week2"):
        return "week2"
    if check_id.startswith("week4.") or check_id.startswith("cross.week4"):
        return "week4"
    if check_id.startswith("langsmith."):
        return "langsmith"
    if check_id.startswith("cross."):
        return "cross"
    if violation.get("type") == "llm_output_schema":
        return "week2"
    if violation.get("type") == "langsmith_trace_schema":
        return "langsmith"
    return "unknown"


def _find_downstream_start_node(nodes: dict[str, Any], edges: list[dict[str, Any]], dataset: str) -> str | None:
    if dataset == "week3":
        # Start from a pipeline that is downstream of the extractor.
        for nid in nodes:
            if "pipeline::week3-document-refinery" in nid:
                return nid
        return None
    if dataset == "week5":
        # No dedicated week5 pipeline node in the synthetic graph; use the cartographer file as a boundary.
        for nid in nodes:
            if "pipeline::week4-lineage-generation" in nid:
                return nid
        return None
    if dataset == "week2":
        for nid in nodes:
            if "file::src/week4/cartographer.py" in nid:
                return nid
        return None
    if dataset == "week1":
        for nid, n in nodes.items():
            if str(n.get("type")) == "FILE" and "week1" in nid:
                return nid
        return None
    if dataset == "week4":
        for nid in nodes:
            if "file::src/week4/cartographer.py" in nid:
                return nid
        return None
    if dataset == "langsmith":
        for nid in nodes:
            if "pipeline::week3-document-refinery" in nid:
                return nid
        return None
    if dataset == "cross":
        for nid in nodes:
            if "pipeline::week3-document-refinery" in nid:
                return nid
        return None
    return None


def _resolve_start_node(
    violation: dict[str, Any], nodes: dict[str, Any], edges: list[dict[str, Any]]
) -> str:
    """Map failing check to a lineage node to traverse upstream from (schema-element → graph anchor)."""
    check_id = str(violation.get("check_id", ""))
    dataset = _guess_failing_dataset(violation)

    explicit: dict[str, str] = {
        "week3.extracted_facts.confidence.range": "pipeline::week3-document-refinery",
        "week3.extracted_facts.entity_refs.relationship": "pipeline::week3-document-refinery",
        "week3.extracted_facts.non_empty": "pipeline::week3-document-refinery",
        "week5.payload.jsonschema": "pipeline::week4-lineage-generation",
        "week5.sequence_number.monotonic": "pipeline::week4-lineage-generation",
        "cross.week2.target_ref.in_week1_code_refs": "pipeline::week4-lineage-generation",
        "cross.week4.doc_id.as_lineage_node": "pipeline::week3-document-refinery",
    }
    if check_id in explicit and explicit[check_id] in nodes:
        return explicit[check_id]

    if check_id.startswith("week3."):
        for nid in nodes:
            if nid == "pipeline::week3-document-refinery":
                return nid
    if check_id.startswith("week5."):
        for nid in nodes:
            if "pipeline::week4-lineage-generation" in nid:
                return nid
    if check_id.startswith("cross.week2"):
        for nid in nodes:
            if "file::src/week4/cartographer.py" in nid:
                return nid
    if check_id.startswith("cross.week4"):
        for nid in nodes:
            if nid == "pipeline::week3-document-refinery":
                return nid

    found = _find_downstream_start_node(nodes, edges, dataset)
    if found:
        return found
    return next(iter(nodes.keys()), "")


def _bfs_upstream_files(
    nodes: dict[str, Any], edges: list[dict[str, Any]], start_node: str
) -> list[tuple[str, int]]:
    """
    Traverse upstream by following edges where edge.target == current.
    Return ranked candidate file nodes with hop distance (hop count >= 0).
    """
    # Build reverse adjacency: target -> sources
    rev: dict[str, list[str]] = {}
    for e in edges:
        src = e.get("source")
        tgt = e.get("target")
        if src and tgt:
            rev.setdefault(str(tgt), []).append(str(src))

    q: list[tuple[str, int]] = [(start_node, 0)]
    seen: set[str] = {start_node}
    candidates: list[tuple[str, int]] = []

    while q:
        cur, hops = q.pop(0)
        for nxt in rev.get(cur, []):
            if nxt in seen:
                continue
            seen.add(nxt)
            nxt_node = nodes.get(nxt, {})
            if str(nxt_node.get("type")) == "FILE":
                candidates.append((nxt, hops + 1))
            # Keep traversing even if we didn't hit FILE yet.
            q.append((nxt, hops + 1))

    # Prefer closest upstream files.
    candidates.sort(key=lambda x: x[1])
    # Must be at least 1 and at most 5 candidates.
    return candidates[:5] if candidates else []


def _blast_radius_from_file(nodes: dict[str, Any], edges: list[dict[str, Any]], file_node_id: str) -> dict[str, Any]:
    fwd: dict[str, list[str]] = {}
    for e in edges:
        src = e.get("source")
        tgt = e.get("target")
        if src and tgt:
            fwd.setdefault(str(src), []).append(str(tgt))

    q: list[tuple[str, int]] = [(file_node_id, 0)]
    seen = {file_node_id}
    affected_files: set[str] = set()
    affected_pipes: set[str] = set()
    contamination_depth = 0

    while q:
        cur, depth = q.pop(0)
        contamination_depth = max(contamination_depth, depth)
        for nxt in fwd.get(cur, []):
            if nxt in seen:
                continue
            seen.add(nxt)
            n = nodes.get(nxt, {})
            if str(n.get("type")) == "FILE":
                affected_files.add(nxt)
            if str(n.get("type")) in {"PIPELINE", "MODEL"}:
                affected_pipes.add(nxt)
            q.append((nxt, depth + 1))

    return {
        "affected_nodes": sorted(list(affected_files)),
        "affected_pipelines": sorted(list(affected_pipes)),
        "estimated_records": 0,
        "contamination_depth": contamination_depth,
    }


def _default_blame_line_range(check_id: str) -> tuple[int, int]:
    """When blame_hint is absent, use a small window at the top of the producer file."""
    if "confidence" in check_id or "extract" in check_id:
        return 1, 120
    if check_id.startswith("week5."):
        return 1, 80
    if check_id.startswith("week1."):
        return 1, 60
    return 1, 80


def _git_work_tree_root(project_root: Path) -> Path:
    """
    Git commands must run with cwd at the repository root. In monorepos, ``project_root``
    may be a package subdirectory; ``git rev-parse --show-toplevel`` discovers the root.

    Override: set ``CONTRACT_ENFORCER_GIT_TOPLEVEL`` to an absolute path when auto-detection
    is wrong (sparse checkouts, nested clones, or unconventional layouts).
    """
    override = os.environ.get("CONTRACT_ENFORCER_GIT_TOPLEVEL", "").strip()
    if override:
        p = Path(override).expanduser().resolve()
        if p.is_dir():
            return p
    pr = project_root.resolve()
    import subprocess

    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(pr),
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        if out:
            return Path(out).resolve()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        pass
    return pr


def _relative_path_for_git(git_root: Path, file_path: Path) -> str | None:
    """Path as git expects (posix, relative to work tree), or None if outside the tree."""
    try:
        abs_f = file_path.resolve()
        abs_g = git_root.resolve()
        rel = abs_f.relative_to(abs_g)
        return rel.as_posix()
    except (ValueError, OSError):
        return None


def _normalize_graph_path(rel: str) -> str:
    """Normalize lineage / node path strings (strip ``file::``, unify slashes)."""
    s = (rel or "").strip().replace("\\", "/")
    if s.startswith("file::"):
        s = s[6:].lstrip("/")
    return s


def _path_from_file_node(file_node_id: str, metadata: dict[str, Any]) -> str:
    """Prefer explicit metadata path; fall back to ``file::relative/path`` node id convention."""
    meta = metadata or {}
    p = str(meta.get("path") or meta.get("file_path") or "").strip()
    if p:
        return _normalize_graph_path(p)
    return _normalize_graph_path(file_node_id)


def _parse_lineage_captured_at(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _build_attribution_context(
    lineage: dict[str, Any] | None,
    lineage_source: Path | None,
    project_root: Path,
) -> dict[str, Any]:
    """
    Shared per-run metadata: where git thinks the repo root is, and lineage freshness hints.
    ``CONTRACT_ATTRIBUTOR_LINEAGE_MAX_AGE_DAYS`` (default 14): warn when ``captured_at`` is older; set to 0 to disable.
    """
    git_root = _git_work_tree_root(project_root)
    raw_age = os.environ.get("CONTRACT_ATTRIBUTOR_LINEAGE_MAX_AGE_DAYS", "14").strip()
    try:
        max_age_days = int(raw_age) if raw_age else 14
    except ValueError:
        max_age_days = 14

    snap: dict[str, Any] = {
        "snapshot_path": str(lineage_source.resolve()) if lineage_source and lineage_source.exists() else None,
        "captured_at": None,
        "git_commit": None,
        "warnings": [],
    }
    if not lineage:
        snap["warnings"].append("no_lineage_snapshot_loaded_upstream_traversal_may_use_static_fallbacks")
        return {
            "git_work_tree": str(git_root),
            "project_root": str(project_root.resolve()),
            "lineage_snapshot": snap,
        }

    snap["captured_at"] = lineage.get("captured_at")
    gc = lineage.get("git_commit")
    snap["git_commit"] = gc if isinstance(gc, str) else None

    if max_age_days > 0:
        dt = _parse_lineage_captured_at(snap["captured_at"])
        if snap["captured_at"] and dt is None:
            snap["warnings"].append("lineage_captured_at_unparseable_freshness_unknown")
        elif not snap["captured_at"]:
            snap["warnings"].append("lineage_captured_at_missing_freshness_not_validated")
        elif dt is not None:
            age_days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0
            if age_days > float(max_age_days):
                snap["warnings"].append(
                    f"lineage_snapshot_older_than_{max_age_days}_days_recommend_refreshing_week4_export"
                )

    return {
        "git_work_tree": str(git_root),
        "project_root": str(project_root.resolve()),
        "lineage_snapshot": snap,
    }


def _git_blame_commit_hash(git_root: Path, rel_path: str, line_start: int, line_end: int) -> str | None:
    import subprocess

    if not _git_available():
        return None
    try:
        out = subprocess.check_output(
            ["git", "blame", "-L", f"{line_start},{line_end}", "-t", "--", rel_path],
            cwd=str(git_root),
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    hashes: list[str] = []
    for line in out.splitlines():
        parts = line.strip().split()
        if not parts:
            continue
        h = parts[0]
        if len(h) == 40 and re.match(r"^[0-9a-f]+$", h, re.I):
            hashes.append(h.lower())
    if not hashes:
        return None
    return max(set(hashes), key=lambda x: hashes.count(x))


def _git_show_commit(git_root: Path, commit_hash: str) -> dict[str, str]:
    import subprocess

    try:
        out = subprocess.check_output(
            ["git", "show", "-s", "--format=%H|%an|%ai|%s", commit_hash],
            cwd=str(git_root),
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        h, an, ai, msg = out.split("|", 3)
        return {
            "commit_hash": h,
            "author": an,
            "commit_timestamp": _git_date_to_iso8601(ai),
            "commit_message": msg,
        }
    except Exception:
        return {}


def _merge_blame_into_commit(
    project_root: Path,
    rel_path: str,
    check_id: str,
    hint: dict[str, Any] | None,
    base: dict[str, str | float],
) -> dict[str, str | float]:
    hint = hint or {}
    ls = int(hint["line_start"]) if hint.get("line_start") is not None else _default_blame_line_range(check_id)[0]
    le = int(hint["line_end"]) if hint.get("line_end") is not None else _default_blame_line_range(check_id)[1]
    git_root = _git_work_tree_root(project_root)
    abs_file = (project_root / rel_path).resolve()
    git_rel = _relative_path_for_git(git_root, abs_file)
    if not git_rel:
        return base
    bh = _git_blame_commit_hash(git_root, git_rel, ls, le)
    if not bh:
        return base
    details = _git_show_commit(git_root, bh)
    if not details:
        return base
    ts = _git_date_to_iso8601(str(details["commit_timestamp"]))
    try:
        commit_dt = datetime.fromisoformat(ts).timestamp()
        age_days = max(0.0, (datetime.now().timestamp() - commit_dt) / 86400.0)
    except Exception:
        age_days = float(base.get("days_since_commit", 0.0))
    return {
        "commit_hash": details.get("commit_hash", str(base.get("commit_hash"))),
        "author": details.get("author", str(base.get("author"))),
        "commit_timestamp": ts,
        "commit_message": details.get("commit_message", str(base.get("commit_message"))),
        "days_since_commit": age_days,
    }


def _days_since_file_mtime(file_path: Path) -> float:
    try:
        import os

        st = os.stat(file_path)
        age_s = max(0.0, (datetime.now().timestamp() - st.st_mtime))
        return age_s / 86400.0
    except Exception:
        return 30.0


def _git_available() -> bool:
    import subprocess

    try:
        subprocess.run(["git", "--version"], cwd=str(_REPO), capture_output=True, check=True)
        # Also ensure this folder is inside a git worktree
        subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=str(_REPO), capture_output=True, check=True)
        return True
    except Exception:
        return False


def _commit_for_file(file_path: Path, *, project_root: Path | None = None) -> dict[str, str | float]:
    """
    Try git log for last 14 days; if git is unavailable, return synthetic commit data.
    Uses the true git work tree (monorepo-safe) and paths relative to that root.
    """
    root = project_root if project_root is not None else _REPO
    if not _git_available():
        return {
            "commit_hash": "0" * 40,
            "author": "unknown",
            "commit_timestamp": _now_iso(),
            "commit_message": "git unavailable; synthetic candidate",
            "days_since_commit": _days_since_file_mtime(file_path),
        }

    import subprocess

    git_root = _git_work_tree_root(root)
    git_rel = _relative_path_for_git(git_root, file_path)
    if not git_rel:
        return {
            "commit_hash": "0" * 40,
            "author": "unknown",
            "commit_timestamp": _now_iso(),
            "commit_message": "file outside git work tree; synthetic candidate (check CONTRACT_ENFORCER_GIT_TOPLEVEL)",
            "days_since_commit": _days_since_file_mtime(file_path),
        }

    try:
        # Prefer last commit that touched it.
        cmd = [
            "git",
            "log",
            "--follow",
            "--since=14 days ago",
            "--format=%H|%an|%ae|%ai|%s",
            "--",
            git_rel,
        ]
        out = subprocess.check_output(cmd, cwd=str(git_root), stderr=subprocess.STDOUT, text=True).strip()
        if not out:
            return {
                "commit_hash": "0" * 40,
                "author": "unknown",
                "commit_timestamp": _now_iso(),
                "commit_message": "no recent git commit found; synthetic candidate",
                "days_since_commit": _days_since_file_mtime(file_path),
            }
        first = out.splitlines()[0]
        commit_hash, an, _ae, ai, msg = first.split("|", 4)
        # days since commit
        try:
            iso_ai = _git_date_to_iso8601(ai)
            commit_dt = datetime.fromisoformat(iso_ai.replace("Z", "+00:00")).timestamp()
            age_days = max(0.0, (datetime.now().timestamp() - commit_dt) / 86400.0)
        except Exception:
            age_days = _days_since_file_mtime(file_path)
        return {
            "commit_hash": commit_hash,
            "author": an,
            "commit_timestamp": _git_date_to_iso8601(ai),
            "commit_message": msg,
            "days_since_commit": age_days,
        }
    except Exception:
        return {
            "commit_hash": "0" * 40,
            "author": "unknown",
            "commit_timestamp": _now_iso(),
            "commit_message": "git error; synthetic candidate",
            "days_since_commit": _days_since_file_mtime(file_path),
        }


def _confidence_score(days_since_commit: float, hop_count: int) -> float:
    base = 1.0 - (days_since_commit * 0.1)
    base -= 0.2 * max(0, hop_count)
    return max(0.0, round(base, 4))


# Cap git blame subprocesses per violation (upstream list is already max 5).
_MAX_BLAME_MERGE_RANKS = 5


def attribute_violations(
    input_path: Path,
    output_path: Path,
    *,
    lineage_jsonl: Path | None = None,
    subscriptions_yaml: Path | None = None,
) -> None:
    lineage, lineage_path = _load_latest_lineage(_REPO, lineage_jsonl)
    attribution_context = _build_attribution_context(lineage, lineage_path, _REPO)
    nodes, edges = _index_lineage(lineage)

    violations = list(_iter_jsonl(input_path))
    out_lines: list[str] = []
    if not output_path.exists():
        output_path.parent.mkdir(parents=True, exist_ok=True)

    for v in violations:
        start = _resolve_start_node(v, nodes, edges) or next(iter(nodes.keys()), "")
        upstream_files = _bfs_upstream_files(nodes, edges, start) or []

        if not upstream_files:
            # Fallback: pick any FILE node
            upstream_files = [(nid, 0) for nid, n in nodes.items() if str(n.get("type")) == "FILE"][:1]

        blame_chain = []
        blast: dict[str, Any] = {
            "subscribers": [],
            "affected_nodes": [],
            "affected_pipelines": [],
            "estimated_records": 0,
            "contamination_depth": 0,
            "registry_query": {"source_contract_id": "", "check_id": "", "matched_count": 0},
        }

        cid = str(v.get("source_contract_id", "") or "")
        chk = str(v.get("check_id", "") or "")
        reg_subs = subscribers_for_violation(_REPO, cid, chk, subscriptions_yaml=subscriptions_yaml)
        blast["subscribers"] = subscriber_summary_entries(reg_subs)
        blast["registry_query"] = {"source_contract_id": cid, "check_id": chk, "matched_count": len(reg_subs)}

        for rank, (file_node_id, hops) in enumerate(upstream_files, start=1):
            node = nodes.get(file_node_id, {})
            rel = _path_from_file_node(str(file_node_id), node if isinstance(node, dict) else {})
            file_path = (_REPO / rel) if rel else (_REPO / "src" / "unknown.py")

            commit = _commit_for_file(file_path, project_root=_REPO)
            # blame_hint (line range) applies to the primary producer only; other ranks use
            # _default_blame_line_range(check_id) inside _merge_blame_into_commit.
            if rel and rank <= _MAX_BLAME_MERGE_RANKS:
                hint = v.get("blame_hint") if rank == 1 else None
                commit = _merge_blame_into_commit(_REPO, rel, str(v.get("check_id", "")), hint, commit)
            days_since = float(commit.get("days_since_commit", 30.0))
            confidence_score = _confidence_score(days_since, hops)

            if rank == 1:
                lineage_blast = _blast_radius_from_file(nodes, edges, file_node_id)
                est = v.get("records_failing")
                if est is None:
                    est = v.get("estimated_records")
                try:
                    lineage_blast["estimated_records"] = int(est) if est is not None else 0
                except (TypeError, ValueError):
                    lineage_blast["estimated_records"] = 0
                blast["affected_nodes"] = lineage_blast.get("affected_nodes", [])
                blast["affected_pipelines"] = lineage_blast.get("affected_pipelines", [])
                blast["estimated_records"] = lineage_blast.get("estimated_records", 0)
                blast["contamination_depth"] = lineage_blast.get("contamination_depth", 0)
                blast["lineage_enrichment"] = {
                    "forward_reachable_files": lineage_blast.get("affected_nodes", []),
                    "forward_reachable_pipelines": lineage_blast.get("affected_pipelines", []),
                }

            blame_chain.append(
                {
                    "rank": rank,
                    "file_path": str(rel).replace("\\", "/") if rel else str(file_path).replace("\\", "/"),
                    "commit_hash": str(commit["commit_hash"]),
                    "author": str(commit["author"]),
                    "commit_timestamp": str(commit["commit_timestamp"]),
                    "commit_message": str(commit["commit_message"]),
                    "confidence_score": confidence_score,
                }
            )

        enriched = dict(v)
        enriched["detected_at"] = str(enriched.get("detected_at", _now_iso()))
        enriched.setdefault("severity", "CRITICAL" if "range" in str(v.get("check_id", "")) else "HIGH")
        enriched.setdefault("source_contract_id", v.get("source_contract_id", "unknown"))
        enriched["sentinel_ingest_version"] = "1.0"
        enriched["blame_chain"] = blame_chain[:5] if blame_chain else [
            {
                "rank": 1,
                "file_path": "unknown",
                "commit_hash": "0" * 40,
                "author": "unknown",
                "commit_timestamp": _now_iso(),
                "commit_message": "fallback candidate",
                "confidence_score": 0.1,
            }
        ]
        enriched["blast_radius"] = blast
        enriched["attribution_context"] = dict(attribution_context)

        out_lines.append(json.dumps(enriched, ensure_ascii=False))

    output_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="ViolationAttributor (Week 7)")
    parser.add_argument("--input", type=Path, default=None, help="violations.jsonl (default if no --violation).")
    parser.add_argument(
        "--violation",
        type=Path,
        default=None,
        help="ValidationRunner JSON report (e.g. validation_reports/violated.json); converts FAIL/ERROR rows to violations.",
    )
    parser.add_argument("--lineage", type=Path, default=None, help="Override lineage JSONL (default: outputs/week4/lineage_snapshots.jsonl).")
    parser.add_argument("--registry", type=Path, default=None, help="Override contract_registry/subscriptions.yaml path.")
    parser.add_argument("--output", type=Path, default=_REPO / "violation_log" / "violations_with_blame.jsonl")
    parser.add_argument("--violation-id", type=str, default=None, help="Optional single violation id filter.")
    args = parser.parse_args()

    reg_path = args.registry.expanduser().resolve() if args.registry else None
    lin_path = args.lineage.expanduser().resolve() if args.lineage else None

    if args.violation is not None:
        vpath = args.violation.expanduser().resolve()
        if not vpath.is_file():
            raise SystemExit(f"--violation not a file: {vpath}")
        converted = violations_from_validation_report(vpath)
        if not converted:
            raise SystemExit(f"No FAIL/ERROR results in validation report: {vpath}")
        input_path = _REPO / "violation_log" / "_from_validation_report.jsonl"
        input_path.write_text("\n".join(json.dumps(v, ensure_ascii=False) for v in converted) + "\n", encoding="utf-8")
        violations = converted
    else:
        input_path = (args.input or (_REPO / "violation_log" / "violations.jsonl")).expanduser().resolve()
        violations = list(_iter_jsonl(input_path))
    if args.violation_id:
        violations = [v for v in violations if str(v.get("violation_id")) == args.violation_id]
        if not violations:
            raise SystemExit(f"No violation found with id={args.violation_id}")

        tmp = _REPO / "violation_log" / "_tmp_violation.jsonl"
        tmp.write_text("\n".join(json.dumps(v) for v in violations) + "\n", encoding="utf-8")
        attribute_violations(tmp, args.output, lineage_jsonl=lin_path, subscriptions_yaml=reg_path)
        try:
            tmp.unlink()
        except Exception:
            pass
    else:
        attribute_violations(
            input_path,
            args.output,
            lineage_jsonl=lin_path,
            subscriptions_yaml=reg_path,
        )

    # Print one enriched violation for quick demo / evaluator parsing.
    with args.output.open("r", encoding="utf-8") as f:
        first = next((ln.strip() for ln in f if ln.strip()), "")
        if first:
            print(first)


if __name__ == "__main__":
    main()

