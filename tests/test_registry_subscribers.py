"""ContractRegistry subscriber matching on a synthetic root."""
from __future__ import annotations

import sys
from pathlib import Path
from textwrap import dedent

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from contracts.registry import subscribers_for_violation  # noqa: E402


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
