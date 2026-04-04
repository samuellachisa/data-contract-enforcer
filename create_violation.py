#!/usr/bin/env python3
"""
Inject the canonical Week 3 scale-change violation

Reads outputs/week3/extractions.jsonl, multiplies each extracted_facts[].confidence by 100,
writes outputs/week3/extractions_violated.jsonl.

Usage (repo root):
  python create_violation.py
  python create_violation.py --source outputs/week3/extractions.jsonl --out outputs/week3/extractions_violated.jsonl
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description="Inject confidence scale violation (0-1 -> 0-100).")
    parser.add_argument("--source", type=Path, default=_REPO / "outputs" / "week3" / "extractions.jsonl")
    parser.add_argument("--out", type=Path, default=_REPO / "outputs" / "week3" / "extractions_violated.jsonl")
    args = parser.parse_args()
    src = args.source.expanduser().resolve()
    out = args.out.expanduser().resolve()
    if not src.is_file():
        raise SystemExit(f"Missing source file: {src}")

    records: list[dict] = []
    with src.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            r = json.loads(line)
            for fact in r.get("extracted_facts", []) or []:
                if "confidence" in fact and fact["confidence"] is not None:
                    try:
                        fact["confidence"] = round(float(fact["confidence"]) * 100, 1)
                    except (TypeError, ValueError):
                        pass
            records.append(r)

    out.parent.mkdir(parents=True, exist_ok=True)
    meta = (
        f"# injection_note: true | injected_at: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} "
        f"| type: scale_change | source: {src.name}\n"
    )
    out.write_text(meta, encoding="utf-8")
    with out.open("a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"Wrote {len(records)} records to {out}")


if __name__ == "__main__":
    main()
