#!/usr/bin/env python3
"""
Inject the canonical Week 3 scale-change violation

Reads outputs/week3/extractions.jsonl, multiplies each extracted_facts[].confidence by 100
(for values in the unit interval 0–1 only — avoids turning 35 into 3500 if the source was already scaled),
writes outputs/week3/extractions_violated.jsonl.

This file alone does not update validation_reports/ or violation_log/. For enforceable FAIL rows, run
contracts/runner.py with --data outputs/week3/extractions_violated.jsonl (see README.md).

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


def _apply_scale(fact: dict) -> str:
    """
    Returns: "scaled" | "already_scaled" | "skip" (missing / non-numeric / in-unit but zero).
    """
    if "confidence" not in fact or fact["confidence"] is None:
        return "skip"
    try:
        c = float(fact["confidence"])
    except (TypeError, ValueError):
        return "skip"
    if c > 1.0:
        return "already_scaled"
    if c < 0.0:
        return "skip"
    if c == 0.0:
        return "skip"
    fact["confidence"] = round(c * 100, 1)
    return "scaled"


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
    scaled = 0
    already = 0
    with src.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            r = json.loads(line)
            for fact in r.get("extracted_facts", []) or []:
                outcome = _apply_scale(fact)
                if outcome == "scaled":
                    scaled += 1
                elif outcome == "already_scaled":
                    already += 1
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
    print(f"Scaled {scaled} fact confidence value(s) from (0,1] to 0-100 scale.")
    if already:
        print(
            f"Note: {already} fact(s) already had confidence > 1 (left unchanged; "
            "contract may still FAIL on range if those values exceed 1.0)."
        )
    if scaled == 0 and already == 0:
        raise SystemExit(
            "No confidence values were scaled: need numeric confidence in (0, 1] on extracted_facts "
            f"(got {len(records)} JSONL row(s)). Restore clean `outputs/week3/extractions.jsonl` "
            "(e.g. `py -3 scripts/seed_outputs.py`) or fix your --source file.\n"
            "Reminder: this script only writes the JSONL file; run contracts/runner.py on "
            "`extractions_violated.jsonl` to produce validation_reports with FAIL rows."
        )


if __name__ == "__main__":
    main()
