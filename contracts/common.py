"""Shared helpers for Week 7 contract tooling."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass
class CheckResult:
    check_id: str
    column_name: str
    check_type: str
    status: str
    actual_value: str
    expected: str
    severity: str
    records_failing: int
    sample_failing: list[str]
    message: str


def baselines_path(root: Path) -> Path:
    return root / "schema_snapshots" / "baselines.json"


def load_baselines(root: Path) -> dict[str, Any]:
    p = baselines_path(root)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def save_baselines(root: Path, data: dict[str, Any]) -> None:
    p = baselines_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def load_jsonl_with_issues(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Parse JSONL tolerating bad lines. Skips blank lines and lines starting with # or //.
    Each issue: line_no, kind (json_decode | not_object), detail.
    """
    rows: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line_no, raw in enumerate(f, start=1):
            line = raw.strip()
            if not line:
                continue
            if line.startswith("#") or line.startswith("//"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                issues.append({"line_no": line_no, "kind": "json_decode", "detail": str(exc)})
                continue
            if not isinstance(obj, dict):
                issues.append(
                    {
                        "line_no": line_no,
                        "kind": "not_object",
                        "detail": f"expected object, got {type(obj).__name__}",
                    }
                )
                continue
            rows.append(obj)
    return rows, issues


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Strict JSONL: raises on first invalid line (unchanged semantics; no comment skipping)."""
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def jsonl_snapshot_id(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yaml.dump(
            data,
            f,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
            width=120,
        )


def iso_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
