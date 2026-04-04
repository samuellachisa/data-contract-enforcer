"""Unit tests for ViolationAttributor graph traversal helpers (no git)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from contracts.attributor import (  # noqa: E402
    _blast_radius_from_file,
    _bfs_upstream_files,
    _confidence_score,
    _guess_failing_dataset,
    _index_lineage,
    _resolve_start_node,
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
