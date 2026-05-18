"""Deterministic archetype classifier for patch-local build formulation."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from automated_builds_pipeline.classification import ItemClassification, surface_for
from automated_builds_pipeline.known_items import load_known_items_file

CARRY_SUFFIX_RE = re.compile(r"\s+(build|builds|run|deck)s?\s*$", re.IGNORECASE)
BAZAARDB = "bazaardb"
MOBALYTICS = ("mobalytics_meta_builds", "mobalytics_build_articles")
CORE_SAMPLE_FLOOR = 20
STRONG_APPEARANCE_FLOOR = 10
STRONG_FREQUENCY_FLOOR = 0.20


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
        surface_for(classification, confidence),
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
    evidence = row.get("current_patch_evidence", {})
    bazaardb_evidence = evidence.get(BAZAARDB, {}) if isinstance(evidence, dict) else {}
    strength = str(row.get("within_patch_strength") or "single_source_thin")
    mobalytics_present = any(src.get(source) == "present" for source in MOBALYTICS)
    bdb_section = _metadata_value(bazaardb_evidence, "section").upper()

    if item == carry_candidate and bazaardb_present and ceiling == "carry_core_support":
        return _make(item, "carry", "high", f"Archetype name match: {item}")
    if item == carry_candidate and mobalytics_present and ceiling == "carry_core_support":
        return _make(item, "carry", "high", f"Editorial archetype name match: {item}")
    if ceiling == "not_applicable":
        return _make(item, "invalid", "low", "All sources absent (not_applicable ceiling)")

    if bazaardb_present and ceiling == "carry_core_support":
        if bdb_section == "CORE ITEMS" or _bazaardb_core_sample(bazaardb_evidence):
            return _make(item, "core", "high", _strength_rationale("bazaardb core-section signal", row))
        if _bazaardb_strong_support(bazaardb_evidence):
            return _make(item, "support", "high", _strength_rationale("strong current bazaardb support signal", row))
        return _make(item, "support", "medium", _strength_rationale("current bazaardb signal", row))

    if mobalytics_present and ceiling == "carry_core_support":
        if _mobalytics_rank(evidence, item) <= 2:
            return _make(item, "core", "high", _strength_rationale("current Mobalytics editorial signal", row))
        return _make(item, "support", "high", _strength_rationale("current Mobalytics editorial signal", row))

    if source_count >= 2:
        return _make(item, "support", "medium", _strength_rationale("same-window multi-source support", row))
    if strength in {"statistical_strong", "editorial_confirmed"}:
        return _make(item, "support", "medium", _strength_rationale("current-patch evidence signal", row))
    return _make(item, "support", "low", "Thin current-patch signal")


def _bazaardb_core_sample(evidence: dict[str, Any]) -> bool:
    sample = _int_value(evidence.get("sample_count"))
    frequency = _float_value(evidence.get("frequency"))
    return sample >= CORE_SAMPLE_FLOOR and frequency >= 1.0


def _bazaardb_strong_support(evidence: dict[str, Any]) -> bool:
    appearances = _int_value(evidence.get("appearances"))
    frequency = _float_value(evidence.get("frequency"))
    return appearances >= STRONG_APPEARANCE_FLOOR and frequency >= STRONG_FREQUENCY_FLOOR


def _mobalytics_rank(evidence: dict[str, Any], item: str) -> int:
    ranks = []
    for source in MOBALYTICS:
        row = evidence.get(source, {}) if isinstance(evidence, dict) else {}
        if row.get("presence") == "present" and row.get("item", item) == item:
            rank = _int_value(row.get("rank"))
            if rank:
                ranks.append(rank)
    return min(ranks, default=999)


def _metadata_value(evidence: dict[str, Any], key: str) -> str:
    metadata = evidence.get("metadata", {})
    if not isinstance(metadata, dict):
        return ""
    return str(metadata.get(key) or "")


def _strength_rationale(prefix: str, row: dict[str, Any]) -> str:
    strength = row.get("within_patch_strength")
    sources = row.get("current_source_support_count")
    suffix = f"; strength={strength}" if strength else ""
    if sources:
        suffix = f"{suffix}; current_sources={sources}"
    return f"{prefix}{suffix}"


def _int_value(value: Any) -> int:
    return value if isinstance(value, int) else 0


def _float_value(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0
