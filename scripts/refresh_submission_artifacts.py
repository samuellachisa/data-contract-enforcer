#!/usr/bin/env python3
"""
Deduplicate violation_log/violations.jsonl after repeated ai_extensions runs.

Keeps comment lines. For type llm_output_schema, drops duplicate (check_id, verdict_id) rows.

Usage (from repo root):
  python scripts/refresh_submission_artifacts.py
  python scripts/refresh_submission_artifacts.py --path violation_log/violations.jsonl
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def dedupe_violations(path: Path) -> tuple[int, int]:
    if not path.exists():
        return 0, 0
    raw = path.read_text(encoding="utf-8").splitlines()
    header: list[str] = []
    bodies: list[str] = []
    for line in raw:
        s = line.strip()
        if not s:
            continue
        if s.startswith("#") or s.startswith("//"):
            header.append(line)
            continue
        bodies.append(line)

    seen_llm: set[tuple[str | None, str | None]] = set()
    kept: list[str] = []
    dropped = 0
    for line in bodies:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            kept.append(line)
            continue
        t = obj.get("type")
        if t == "llm_output_schema":
            key = (obj.get("check_id"), obj.get("verdict_id"))
            if key in seen_llm:
                dropped += 1
                continue
            seen_llm.add(key)
        kept.append(line)

    out_lines = header + kept
    path.write_text("\n".join(out_lines) + ("\n" if out_lines else ""), encoding="utf-8")
    return len(bodies), dropped


def main() -> None:
    parser = argparse.ArgumentParser(description="Deduplicate violations.jsonl for submission hygiene.")
    parser.add_argument(
        "--path",
        type=Path,
        default=None,
        help="Path to violations.jsonl (default: <repo>/violation_log/violations.jsonl)",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    p = args.path or (root / "violation_log" / "violations.jsonl")
    n_before, dropped = dedupe_violations(p)
    print(f"Deduped {p}: lines={n_before}, dropped_duplicate_llm={dropped}")


if __name__ == "__main__":
    main()
