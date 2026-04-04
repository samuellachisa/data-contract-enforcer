"""Unit tests for ViolationAttributor graph traversal helpers (no git)."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from contracts.attributor import (  # noqa: E402
    _blast_radius_from_file,
    _bfs_upstream_files,
    _build_attribution_context,
    _confidence_score,
    _guess_failing_dataset,
    _index_lineage,
    _normalize_graph_path,
    _path_from_file_node,
    _relative_path_for_git,
    _resolve_start_node,
    violations_from_validation_report,
)


def _nodes_dict(items: list[tuple[str, str]]) -> dict[str, dict]:
    """(node_id, type) -> nodes map."""
    return {nid: {"node_id": nid, "type": t} for nid, t in items}


def test_bfs_upstream_files_finds_closest_file() -> None:
    # file -> pipeline -> start (edges: source -> target)
    file_id = "file::src/producer.py"
    pipe_id = "pipeline::week3-document-refinery"
    start = pipe_id
    nodes = _nodes_dict([(file_id, "FILE"), (pipe_id, "PIPELINE")])
    edges = [{"source": file_id, "target": pipe_id}]
    ranked = _bfs_upstream_files(nodes, edges, start)
    assert ranked
    assert ranked[0][0] == file_id
    assert ranked[0][1] == 1  # one hop upstream


def test_bfs_upstream_files_caps_at_five() -> None:
    pipe = "pipeline::p"
    files = [f"file::f{i}" for i in range(8)]
    nodes = _nodes_dict([(pipe, "PIPELINE")] + [(f, "FILE") for f in files])
    edges = [{"source": f, "target": pipe} for f in files]
    ranked = _bfs_upstream_files(nodes, edges, pipe)
    assert len(ranked) == 5


def test_blast_radius_from_file_forward() -> None:
    f = "file::a.py"
    p = "pipeline::p1"
    m = "model::m1"
    nodes = _nodes_dict([(f, "FILE"), (p, "PIPELINE"), (m, "MODEL")])
    edges = [
        {"source": f, "target": p},
        {"source": p, "target": m},
    ]
    br = _blast_radius_from_file(nodes, edges, f)
    assert p in br["affected_pipelines"] or m in br["affected_pipelines"]
    assert br["contamination_depth"] >= 1


def test_confidence_score_decreases_with_hops_and_age() -> None:
    young_close = _confidence_score(days_since_commit=1.0, hop_count=0)
    old_far = _confidence_score(days_since_commit=10.0, hop_count=3)
    assert young_close > old_far


def test_guess_failing_dataset_prefixes() -> None:
    assert _guess_failing_dataset({"check_id": "week3.doc_id.required"}) == "week3"
    assert _guess_failing_dataset({"check_id": "week5.payload.jsonschema"}) == "week5"
    assert _guess_failing_dataset({"check_id": "langsmith.tokens"}) == "langsmith"


def test_resolve_start_node_week3_explicit() -> None:
    pipe = "pipeline::week3-document-refinery"
    nodes = _nodes_dict([(pipe, "PIPELINE"), ("file::x", "FILE")])
    edges: list[dict] = []
    v = {"check_id": "week3.extracted_facts.confidence.range"}
    assert _resolve_start_node(v, nodes, edges) == pipe


def test_index_lineage_empty() -> None:
    n, e = _index_lineage(None)
    assert n == {} and e == []


def test_normalize_graph_path_strips_file_prefix() -> None:
    assert _normalize_graph_path("file::src/week1/x.py") == "src/week1/x.py"
    assert _normalize_graph_path("") == ""


def test_path_from_file_node_prefers_metadata_then_node_id() -> None:
    assert _path_from_file_node("file::ignored.py", {"path": "src/real.py"}) == "src/real.py"
    assert _path_from_file_node("file::src/fallback.py", {}) == "src/fallback.py"


def test_relative_path_for_git_inside_and_outside_tree(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    inner = root / "pkg" / "a.txt"
    inner.parent.mkdir(parents=True)
    inner.write_text("x", encoding="utf-8")
    assert _relative_path_for_git(root, inner) == "pkg/a.txt"

    outside = tmp_path / "other" / "b.txt"
    outside.parent.mkdir(parents=True)
    outside.write_text("y", encoding="utf-8")
    assert _relative_path_for_git(root, outside) is None


def test_build_attribution_context_warns_when_lineage_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONTRACT_ATTRIBUTOR_LINEAGE_MAX_AGE_DAYS", "1")
    lin_file = tmp_path / "lineage.jsonl"
    lin_file.write_text("{}", encoding="utf-8")
    old = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ctx = _build_attribution_context({"captured_at": old, "git_commit": "a" * 40}, lin_file, tmp_path)
    warns = ctx["lineage_snapshot"]["warnings"]
    assert any("older_than" in w for w in warns)


def test_build_attribution_context_no_snapshot(tmp_path: Path) -> None:
    ctx = _build_attribution_context(None, None, tmp_path)
    assert any("no_lineage_snapshot" in w for w in ctx["lineage_snapshot"]["warnings"])


def test_violations_from_validation_report_skips_non_list_results(tmp_path: Path) -> None:
    p = tmp_path / "rep.json"
    p.write_text(json.dumps({"contract_id": "x", "results": "broken"}), encoding="utf-8")
    assert violations_from_validation_report(p) == []


def test_violations_from_validation_report_skips_non_dict_rows(tmp_path: Path) -> None:
    p = tmp_path / "rep.json"
    p.write_text(
        json.dumps(
            {
                "contract_id": "x",
                "run_timestamp": "2026-01-01T00:00:00Z",
                "results": [None, "x", {"status": "FAIL", "check_id": "c1", "message": "m", "severity": "HIGH", "records_failing": 1}],
            }
        ),
        encoding="utf-8",
    )
    out = violations_from_validation_report(p)
    assert len(out) == 1
    assert out[0]["check_id"] == "c1"
