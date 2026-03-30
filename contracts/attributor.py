#!/usr/bin/env python3
"""
ViolationAttributor (Week 7).

Reads violation_log/violations.jsonl and enriches each violation with:
- blame_chain (ranked candidates, commit hash, author, timestamp, confidence_score)
- blast_radius (affected_nodes/pipelines + estimated_records)
"""
from __future__ import annotations

import argparse
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

_REPO = Path(__file__).resolve().parents[1]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line.startswith("#") or line.startswith("//"):
                continue
            yield json.loads(line)


def _load_latest_lineage(root: Path) -> dict[str, Any] | None:
    p = root / "outputs" / "week4" / "lineage_snapshots.jsonl"
    if not p.exists():
        return None
    rows = list(_iter_jsonl(p))
    return rows[-1] if rows else None


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
    if violation.get("type") == "llm_output_schema":
        return "week2"
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
        # Synthetic lineage graph doesn't include week2; use cartographer.
        for nid in nodes:
            if "file::src/week4/cartographer.py" in nid:
                return nid
        return None
    return None


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

    q = [file_node_id]
    seen = {file_node_id}
    affected_files: set[str] = set()
    affected_pipes: set[str] = set()

    while q:
        cur = q.pop(0)
        for nxt in fwd.get(cur, []):
            if nxt in seen:
                continue
            seen.add(nxt)
            n = nodes.get(nxt, {})
            if str(n.get("type")) == "FILE":
                affected_files.add(nxt)
            if str(n.get("type")) in {"PIPELINE", "MODEL"}:
                affected_pipes.add(nxt)
            q.append(nxt)

    return {
        "affected_nodes": sorted(list(affected_files)),
        "affected_pipelines": sorted(list(affected_pipes)),
        "estimated_records": 0,
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


def _commit_for_file(file_path: Path) -> dict[str, str | float]:
    """
    Try git log for last 14 days; if git is unavailable, return synthetic commit data.
    """
    if not _git_available():
        return {
            "commit_hash": "0" * 40,
            "author": "unknown",
            "commit_timestamp": _now_iso(),
            "commit_message": "git unavailable; synthetic candidate",
            "days_since_commit": _days_since_file_mtime(file_path),
        }

    import subprocess

    rel = str(file_path).replace("\\", "/")
    try:
        # Prefer last commit that touched it.
        cmd = [
            "git",
            "log",
            "--follow",
            "--since=14 days ago",
            "--format=%H|%an|%ae|%ai|%s",
            "--",
            rel,
        ]
        out = subprocess.check_output(cmd, cwd=str(_REPO), stderr=subprocess.STDOUT, text=True).strip()
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
            commit_dt = datetime.fromisoformat(ai.replace("Z", "+00:00")).timestamp()
            age_days = max(0.0, (datetime.now().timestamp() - commit_dt) / 86400.0)
        except Exception:
            age_days = _days_since_file_mtime(file_path)
        return {
            "commit_hash": commit_hash,
            "author": an,
            "commit_timestamp": ai.replace(" ", "T"),
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


def attribute_violations(input_path: Path, output_path: Path) -> None:
    lineage = _load_latest_lineage(_REPO)
    nodes, edges = _index_lineage(lineage)

    violations = list(_iter_jsonl(input_path))
    out_lines: list[str] = []
    if not output_path.exists():
        output_path.parent.mkdir(parents=True, exist_ok=True)

    for v in violations:
        dataset = _guess_failing_dataset(v)
        start = _find_downstream_start_node(nodes, edges, dataset) or next(iter(nodes.keys()), "")
        upstream_files = _bfs_upstream_files(nodes, edges, start) or []

        if not upstream_files:
            # Fallback: pick any FILE node
            upstream_files = [(nid, 0) for nid, n in nodes.items() if str(n.get("type")) == "FILE"][:1]

        blame_chain = []
        blast = {"affected_nodes": [], "affected_pipelines": [], "estimated_records": 0}

        for rank, (file_node_id, hops) in enumerate(upstream_files, start=1):
            node = nodes.get(file_node_id, {})
            rel = str(node.get("metadata", {}).get("path") or node.get("metadata", {}).get("file_path") or "")
            file_path = (_REPO / rel) if rel else (_REPO / "src" / "unknown.py")

            commit = _commit_for_file(file_path)
            days_since = float(commit.get("days_since_commit", 30.0))
            confidence_score = _confidence_score(days_since, hops)

            if rank == 1:
                blast = _blast_radius_from_file(nodes, edges, file_node_id)

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

        out_lines.append(json.dumps(enriched, ensure_ascii=False))

    output_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="ViolationAttributor (Week 7)")
    parser.add_argument("--input", type=Path, default=_REPO / "violation_log" / "violations.jsonl")
    parser.add_argument("--output", type=Path, default=_REPO / "violation_log" / "violations_with_blame.jsonl")
    parser.add_argument("--violation-id", type=str, default=None, help="Optional single violation id filter.")
    args = parser.parse_args()

    input_path: Path = args.input
    violations = list(_iter_jsonl(input_path))
    if args.violation_id:
        violations = [v for v in violations if str(v.get("violation_id")) == args.violation_id]
        if not violations:
            raise SystemExit(f"No violation found with id={args.violation_id}")

        tmp = _REPO / "violation_log" / "_tmp_violation.jsonl"
        tmp.write_text("\n".join(json.dumps(v) for v in violations) + "\n", encoding="utf-8")
        attribute_violations(tmp, args.output)
        try:
            tmp.unlink()
        except Exception:
            pass
    else:
        attribute_violations(input_path, args.output)

    # Print one enriched violation for quick demo / evaluator parsing.
    with args.output.open("r", encoding="utf-8") as f:
        first = next((ln.strip() for ln in f if ln.strip()), "")
        if first:
            print(first)


if __name__ == "__main__":
    main()

