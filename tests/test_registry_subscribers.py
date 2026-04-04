"""ContractRegistry subscriber matching on a synthetic root."""
from __future__ import annotations

import sys
from pathlib import Path
from textwrap import dedent

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from contracts.registry import (  # noqa: E402
    breaking_field_matches_check,
    subscribers_for_violation,
)


def test_subscribers_for_violation_matches_breaking_field(tmp_path: Path) -> None:
    reg = tmp_path / "contract_registry"
    reg.mkdir(parents=True)
    yaml_text = dedent(
        """
        subscriptions:
          - contract_id: demo-contract
            subscriber_id: downstream-svc
            subscriber_team: platform
            fields_consumed: [doc_id, extracted_facts]
            breaking_fields:
              - field: extracted_facts.confidence
                reason: scale must stay 0-1
            validation_mode: ENFORCE
            contact: team@example.com
        """
    ).strip()
    (reg / "subscriptions.yaml").write_text(yaml_text + "\n", encoding="utf-8")

    matched = subscribers_for_violation(
        tmp_path,
        "demo-contract",
        "week3.extracted_facts.confidence.range",
    )
    assert len(matched) == 1
    assert matched[0]["subscriber_id"] == "downstream-svc"
    mbf = matched[0].get("_matched_breaking_fields") or []
    assert any(x.get("field") == "extracted_facts.confidence" for x in mbf)


def test_subscribers_for_violation_no_match_wrong_contract(tmp_path: Path) -> None:
    reg = tmp_path / "contract_registry"
    reg.mkdir(parents=True)
    (reg / "subscriptions.yaml").write_text(
        dedent(
            """
            subscriptions:
              - contract_id: other-contract
                subscriber_id: x
                subscriber_team: t
                fields_consumed: [a]
                breaking_fields:
                  - field: a
                    reason: r
                validation_mode: AUDIT
                contact: c@x.com
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    assert subscribers_for_violation(tmp_path, "demo-contract", "week3.x") == []


def test_breaking_field_runner_schema_required_prefix_only() -> None:
    assert breaking_field_matches_check("runner.schema.required", "runner.schema.required.doc_id") is True
    assert breaking_field_matches_check("runner.schema.required", "week3.doc_id.required") is False


def test_breaking_field_cross_ingest_prefix() -> None:
    assert breaking_field_matches_check("cross.ingest", "cross.ingest.week3_extractions.line_2") is True
    assert breaking_field_matches_check("cross.ingest", "cross.week4.doc_id.as_lineage_node") is False


def test_repo_subscriptions_match_week4_snapshot_clause() -> None:
    matched = subscribers_for_violation(
        ROOT,
        "week4-brownfield-lineage-snapshots",
        "week4.snapshot.non_empty",
    )
    sids = {m.get("subscriber_id") for m in matched}
    assert "week6-synthesis-consumer" in sids
    assert "week7-data-contract-enforcer" in sids


def test_repo_subscriptions_langsmith_to_week7() -> None:
    matched = subscribers_for_violation(ROOT, "langsmith-trace-runs", "langsmith.total_tokens.sum")
    assert any(m.get("subscriber_id") == "week7-data-contract-enforcer" for m in matched)


def test_repo_subscriptions_cross_system_to_week7() -> None:
    matched = subscribers_for_violation(
        ROOT,
        "cross-system-dependencies",
        "cross.week4.doc_id.as_lineage_node",
    )
    assert len(matched) == 1
    assert matched[0].get("subscriber_id") == "week7-data-contract-enforcer"
