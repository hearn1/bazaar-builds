"""Shared classifier result types for build formulation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

VALID_CLASSIFICATIONS = {"carry", "core", "support", "invalid"}
VALID_CONFIDENCES = {"high", "medium", "low"}


@dataclass(frozen=True)
class ItemClassification:
    item: str
    classification: str
    confidence: str
    rationale: str
    surface: str

    @classmethod
    def from_dict(cls, data: dict[str, Any], known_items: set[str]) -> "ItemClassification":
        item = str(data.get("item", "")).strip()
        classification = str(data.get("classification", "invalid")).strip().lower()
        confidence = str(data.get("confidence", "low")).strip().lower()
        rationale = str(data.get("rationale", "")).strip()
        if classification not in VALID_CLASSIFICATIONS:
            classification = "invalid"
        if confidence not in VALID_CONFIDENCES:
            confidence = "low"
        if item not in known_items:
            classification = "invalid"
            rationale = rationale or "Unknown item name returned by classifier."
        return cls(
            item=item,
            classification=classification,
            confidence=confidence,
            rationale=rationale,
            surface=surface_for(classification, confidence),
        )

    def to_diff_dict(self) -> dict[str, str]:
        # The llm_* keys are the existing v1 artifact/applier contract. They now
        # carry deterministic classifier output; newer names ride alongside them.
        return {
            "item": self.item,
            "classification": self.classification,
            "rationale": self.rationale,
            "confidence": self.confidence,
            "llm_classification": self.classification,
            "llm_rationale": self.rationale,
            "llm_confidence": self.confidence,
            "surface": self.surface,
        }


def validate_classifications(items: list[dict[str, Any]], known_items: set[str]) -> list[ItemClassification]:
    return [ItemClassification.from_dict(item, known_items) for item in items]


def surface_for(classification: str, confidence: str) -> str:
    if classification == "invalid" or confidence == "low":
        return "suppressed"
    if confidence == "medium":
        return "weaker_signal"
    return "top_line"
