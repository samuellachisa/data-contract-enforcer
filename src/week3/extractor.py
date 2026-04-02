"""Week 3 Document Refinery — fact and entity extractor.

Contract requirements enforced here:
  - extracted_facts[*].confidence must be a float in [0.0, 1.0] (not scaled to 0–100)
  - extracted_facts[*].entity_refs must only reference entity_id values present in
    the same record's entities[] array
"""
from __future__ import annotations

import uuid
from typing import Any


def _clamp_confidence(value: float) -> float:
    """Clamp confidence to [0.0, 1.0].

    If the value is on the 0–100 scale (> 1.0), normalise it back to a probability.
    This prevents the scale-drift violation caught by week3.extracted_facts.confidence.range.
    """
    if value > 1.0:
        normalised = value / 100.0
        return round(min(max(normalised, 0.0), 1.0), 4)
    return round(min(max(value, 0.0), 1.0), 4)


def _validate_entity_refs(facts: list[dict[str, Any]], entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove any entity_ref that does not resolve to a known entity_id.

    This prevents the referential-integrity violation caught by
    week3.extracted_facts.entity_refs.relationship.
    """
    known_ids = {e["entity_id"] for e in entities if "entity_id" in e}
    cleaned: list[dict[str, Any]] = []
    for fact in facts:
        valid_refs = [ref for ref in fact.get("entity_refs", []) if ref in known_ids]
        cleaned.append({**fact, "entity_refs": valid_refs})
    return cleaned


def extract_document(_path: str) -> dict[str, Any]:
    """Return a contract-compliant extraction record for the given document path.

    In production this would call an LLM; here it returns a minimal valid stub
    that satisfies all Week 3 contract checks.
    """
    entity_id = str(uuid.uuid4())
    entities: list[dict[str, Any]] = [
        {"entity_id": entity_id, "name": "Example Entity", "type": "ORG", "canonical_value": "example"}
    ]
    raw_facts: list[dict[str, Any]] = [
        {
            "fact_id": str(uuid.uuid4()),
            "text": "Stub fact extracted from document.",
            "entity_refs": [entity_id],
            "confidence": _clamp_confidence(0.85),
            "page_ref": 1,
            "source_excerpt": "stub excerpt",
        }
    ]
    facts = _validate_entity_refs(raw_facts, entities)
    return {"facts": facts, "entities": entities}
