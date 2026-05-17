"""No-LLM deterministic archetype classifier.

Implements the same ``classify_archetype()`` interface as ``LLMClassifier`` so the
pipeline/diff layers can use it as a drop-in duck-typed classifier. Surface is always
computed via the shared ``llm._surface_for`` so the two classifiers cannot drift.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from automated_builds_pipeline.known_items import load_known_items_file
from automated_builds_pipeline.llm import ItemClassification, _surface_for

CARRY_SUFFIX_RE = re.compile(r"\s+(build|builds|run|deck)s?\s*$", re.IGNORECASE)
CORE_HIGH_WINDOWS = 3
CORE_MED_WINDOWS = 2
BAZAARDB = "bazaardb"


class DeterministicClassifier:
    known_items: set[str]

    def __init__(self, known_items_path: Optional[Path] = None) -> None:
        self.known_items = load_known_items_file(known_items_path)

    def classify_archetype(
        self,
        hero: str,
        phase: Optional[str],
        archetype: Optional[str],
        existing_buckets: dict[str, list[str]],
        candidate_items: list[dict[str, Any]],
        evidence_summary: dict[str, Any],
        mobalytics_description: Optional[str],
    ) -> list[ItemClassification]:
        carry = _extract_carry_candidate(archetype or "", self.known_items, candidate_items)
        return [_classify_row(row, self.known_items, carry) for row in candidate_items]


def _make(item: str, classification: str, confidence: str, rationale: str) -> ItemClassification:
    return ItemClassification(
        item,
        classification,
        confidence,
        rationale,
        _surface_for(classification, confidence),
    )


def _extract_carry_candidate(
    archetype: str,
    known_items: set[str],
    candidate_items: list[dict[str, Any]],
) -> Optional[str]:
    candidate_names = {row.get("item") for row in candidate_items}
    stripped = CARRY_SUFFIX_RE.sub("", archetype).strip()
    for phrase in [stripped, stripped.split()[0] if stripped else ""]:
        if phrase and phrase in known_items and phrase in candidate_names:
            return phrase
    return None


def _classify_row(
    row: dict[str, Any],
    known_items: set[str],
    carry_candidate: Optional[str],
) -> ItemClassification:
    item = str(row.get("item", ""))
    if item not in known_items:
        return _make(item, "invalid", "low", "Item not in known_items")
    ceiling = row.get("classification_ceiling", "carry_core_support")
    if ceiling == "not_applicable":
        return _make(item, "invalid", "low", "All sources absent (not_applicable ceiling)")
    src = row.get("source_presence", {})
    bazaardb_present = src.get(BAZAARDB) == "present"
    source_count = sum(1 for v in src.values() if v == "present")
    windows = int(row.get("windows_seen") or 0)

    if item == carry_candidate and bazaardb_present and ceiling == "carry_core_support":
        return _make(item, "carry", "high", f"Archetype name match: {item}")
    if bazaardb_present and windows >= CORE_HIGH_WINDOWS and ceiling == "carry_core_support":
        return _make(item, "core", "high", f"bazaardb present, {windows} windows")
    if bazaardb_present and windows >= CORE_MED_WINDOWS:
        cls = "support" if ceiling == "support_only" else "core"
        return _make(item, cls, "medium", f"bazaardb present, {windows} windows")
    if source_count >= 1 and windows >= 1:
        if windows == 1 and source_count == 1:
            return _make(item, "support", "low", "Single-window single-source signal")
        return _make(item, "support", "medium", f"{source_count} sources, {windows} windows")
    return _make(item, "support", "low", "Weak signal")
