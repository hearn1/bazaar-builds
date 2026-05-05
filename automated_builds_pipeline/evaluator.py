"""Threshold evaluation for automated build refresh candidates."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from automated_builds_pipeline.sources.base import HEALTHY, SourceFetchResult
from automated_builds_pipeline.state import CuratorState, load_state
from automated_builds_pipeline.stats import (
    HeroStats,
    ItemWindowEvidence,
    SourceWindowSummary,
    WindowItemStats,
    WindowObservation,
    load_stats,
)

PRIMARY_SOURCE = "bazaardb"
MOBALYTICS_SOURCES = ("mobalytics_meta_builds", "mobalytics_build_articles")
SECONDARY_SOURCES = (*MOBALYTICS_SOURCES, "bazaar_builds_net")
EVALUATED_SOURCES = (PRIMARY_SOURCE, *SECONDARY_SOURCES)

ADD_BAZAARDB_MIN_SEEN = 2
ADD_BAZAARDB_WINDOW_COUNT = 3
ADD_BAZAAR_BUILDS_NET_MIN_SEEN = 2
ADD_BAZAAR_BUILDS_NET_WINDOW_COUNT = 3
REMOVE_BAZAARDB_ABSENT_PATCHES = 4
REMOVE_MIN_ABSENT_DAYS = 21


@dataclass
class CatalogItem:
    item: str
    phase: Optional[str] = None
    archetype: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CatalogItem":
        return cls(
            item=str(data["item"]),
            phase=_optional_str(data.get("phase")),
            archetype=_optional_str(data.get("archetype")),
        )


@dataclass
class SourceDisagreement:
    bazaardb: Optional[bool]
    mobalytics: Optional[bool]
    bazaar_builds_net: Optional[bool]
    classification_ceiling: str
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "bazaardb": self.bazaardb,
            "mobalytics": self.mobalytics,
            "bazaar_builds_net": self.bazaar_builds_net,
            "classification_ceiling": self.classification_ceiling,
            "rationale": self.rationale,
        }


@dataclass
class ThresholdDecision:
    item: str
    action: str
    reason: str
    phase: Optional[str] = None
    archetype: Optional[str] = None
    classification_ceiling: str = "core_or_carry"
    disagreement: Optional[SourceDisagreement] = None
    evidence_refs: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "item": self.item,
            "action": self.action,
            "reason": self.reason,
            "classification_ceiling": self.classification_ceiling,
            "evidence_refs": list(self.evidence_refs),
            "unresolved": list(self.unresolved),
        }
        if self.phase is not None:
            data["phase"] = self.phase
        if self.archetype is not None:
            data["archetype"] = self.archetype
        if self.disagreement is not None:
            data["disagreement"] = self.disagreement.to_dict()
        return data


@dataclass
class EvaluationResult:
    hero: str
    freeze_active: bool
    decisions: list[ThresholdDecision] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "hero": self.hero,
            "freeze_active": self.freeze_active,
            "decisions": [decision.to_dict() for decision in self.decisions],
            "unresolved": list(self.unresolved),
        }


def evaluate_hero(
    hero: str,
    catalog_items: Iterable[CatalogItem | dict[str, Any]],
    stats: HeroStats,
    source_results: Iterable[SourceFetchResult],
    state: Optional[CuratorState] = None,
    *,
    now: Optional[datetime] = None,
) -> EvaluationResult:
    state = state or CuratorState()
    current = _current_index(source_results)
    catalog = _catalog_index(catalog_items)
    freeze = state.freeze_status(hero, now)
    freeze_active = freeze.active
    unresolved: list[str] = []
    if freeze_active:
        unresolved.append(f"freeze_active:{freeze.scope}:{freeze.until}")

    all_items = set(catalog) | set(stats.items)
    for source_result in current.values():
        all_items.update(item.item for item in source_result.observation.items if item.present)

    decisions: list[ThresholdDecision] = []
    for item in sorted(all_items):
        disagreement = classify_source_disagreement(item, current)
        existing = catalog.get(item)
        if existing is None:
            decision = _evaluate_add(item, stats, current, disagreement)
        else:
            decision = _evaluate_existing(item, existing, stats, current, disagreement, freeze_active)
        decisions.append(decision)

    return EvaluationResult(hero=hero, freeze_active=freeze_active, decisions=decisions, unresolved=unresolved)


def classify_source_disagreement(
    item: str,
    current_results: dict[str, SourceFetchResult],
) -> SourceDisagreement:
    bazaardb = _current_presence(item, current_results.get(PRIMARY_SOURCE))
    mobalytics_values = [
        _current_presence(item, current_results.get(source))
        for source in MOBALYTICS_SOURCES
    ]
    mobalytics = _combine_presence(mobalytics_values)
    bazaar_builds_net = _current_presence(item, current_results.get("bazaar_builds_net"))

    if bazaardb is True:
        return SourceDisagreement(bazaardb, mobalytics, bazaar_builds_net, "core_or_carry", "primary_present")
    if bazaardb is False and (mobalytics is True or bazaar_builds_net is True):
        return SourceDisagreement(bazaardb, mobalytics, bazaar_builds_net, "support_only", "secondary_present_primary_absent")
    if bazaardb is False:
        return SourceDisagreement(bazaardb, mobalytics, bazaar_builds_net, "remove_eligible", "all_available_sources_clear")
    if mobalytics is True or bazaar_builds_net is True:
        return SourceDisagreement(bazaardb, mobalytics, bazaar_builds_net, "support_only", "secondary_present_primary_unknown")
    return SourceDisagreement(bazaardb, mobalytics, bazaar_builds_net, "no_change", "primary_unknown")


def hydrate_source_fetch_result(data: dict[str, Any]) -> SourceFetchResult:
    payload = data.get("observation", data)
    if not isinstance(payload, dict):
        raise ValueError("source artifact observation must be an object")
    items = [ItemWindowEvidence.from_dict(item) for item in payload.get("items", [])]
    observation = WindowObservation(
        window_id=str(payload["window_id"]),
        observed_at=str(payload["observed_at"]),
        artifact_ref=_optional_str(payload.get("artifact_ref")),
        health_status=str(data.get("status", payload.get("health_status", HEALTHY))),
        details=[str(detail) for detail in data.get("details", payload.get("details", []))],
        items=items,
    )
    status = str(data.get("status", observation.health_status))
    return SourceFetchResult(
        observation=observation,
        status=status,
        details=list(observation.details),
        patch_label=_optional_str(data.get("patch_label")),
    )


def load_source_artifacts(source_artifacts: Path) -> list[SourceFetchResult]:
    results = []
    for path in sorted(source_artifacts.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"source artifact {path} must contain a JSON object")
        result = hydrate_source_fetch_result(data)
        if result.observation.artifact_ref is None:
            result.observation.artifact_ref = str(path)
        results.append(result)
    return results


def load_catalog_items(path: Path) -> list[CatalogItem]:
    data = json.loads(path.read_text(encoding="utf-8"))
    raw_items = data.get("items", data) if isinstance(data, dict) else data
    if not isinstance(raw_items, list):
        raise ValueError("catalog items must be a list or an object with an items list")
    return [CatalogItem.from_dict(item) for item in raw_items]


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Dry-run automated build threshold evaluation.")
    parser.add_argument("--hero", required=True, help="Hero name, e.g. Karnok")
    parser.add_argument("--stats-dir", type=Path, required=True, help="Directory containing *_stats.json sidecars.")
    parser.add_argument("--state-file", type=Path, required=True, help="Curator state JSON file.")
    parser.add_argument("--source-artifacts", type=Path, required=True, help="Directory containing one JSON output per source.")
    parser.add_argument("--catalog", type=Path, required=True, help="Current hero catalog items JSON.")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    result = evaluate_hero(
        args.hero,
        load_catalog_items(args.catalog),
        load_stats(args.hero, args.stats_dir),
        load_source_artifacts(args.source_artifacts),
        load_state(args.state_file),
    )
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True, ensure_ascii=False))
    return 0


def _evaluate_add(
    item: str,
    stats: HeroStats,
    current: dict[str, SourceFetchResult],
    disagreement: SourceDisagreement,
) -> ThresholdDecision:
    evidence_refs = _evidence_refs(item, current)
    if _healthy_seen_count(stats, current, item, PRIMARY_SOURCE, ADD_BAZAARDB_WINDOW_COUNT) >= ADD_BAZAARDB_MIN_SEEN:
        return ThresholdDecision(item, "add_candidate", "bazaardb_2_of_3", classification_ceiling=disagreement.classification_ceiling, disagreement=disagreement, evidence_refs=evidence_refs)
    if any(_current_presence(item, current.get(source)) is True for source in MOBALYTICS_SOURCES):
        return ThresholdDecision(item, "add_candidate", "mobalytics_current", classification_ceiling=disagreement.classification_ceiling, disagreement=disagreement, evidence_refs=evidence_refs)
    if _healthy_seen_count(stats, current, item, "bazaar_builds_net", ADD_BAZAAR_BUILDS_NET_WINDOW_COUNT) >= ADD_BAZAAR_BUILDS_NET_MIN_SEEN:
        return ThresholdDecision(item, "add_candidate", "bazaar_builds_net_2_of_3", classification_ceiling=disagreement.classification_ceiling, disagreement=disagreement, evidence_refs=evidence_refs)
    if _current_source_support_count(item, current) >= 2:
        return ThresholdDecision(item, "add_candidate", "mixed_current_sources", classification_ceiling=disagreement.classification_ceiling, disagreement=disagreement, evidence_refs=evidence_refs)
    return ThresholdDecision(item, "no_change", "below_add_threshold", classification_ceiling=disagreement.classification_ceiling, disagreement=disagreement, evidence_refs=evidence_refs)


def _evaluate_existing(
    item: str,
    existing: CatalogItem,
    stats: HeroStats,
    current: dict[str, SourceFetchResult],
    disagreement: SourceDisagreement,
    freeze_active: bool,
) -> ThresholdDecision:
    evidence_refs = _evidence_refs(item, current)
    if freeze_active:
        return ThresholdDecision(item, "no_change", "freeze_active", existing.phase, existing.archetype, disagreement.classification_ceiling, disagreement, evidence_refs)

    if _existing_core_or_carry(existing) and disagreement.classification_ceiling == "support_only":
        return ThresholdDecision(item, "no_change", "primary_absent_secondary_present_preserve_existing_classification", existing.phase, existing.archetype, disagreement.classification_ceiling, disagreement, evidence_refs)

    if _archetype_changed(item, existing, current):
        return ThresholdDecision(item, "no_change", "archetype_change_unresolved", existing.phase, existing.archetype, disagreement.classification_ceiling, disagreement, evidence_refs, ["archetype_reshuffle_deferred"])

    remove_status = _remove_status(item, stats, current)
    if remove_status == "remove_candidate":
        return ThresholdDecision(item, "remove_candidate", "bazaardb_absent_4_patches_21_days_secondaries_clear", existing.phase, existing.archetype, disagreement.classification_ceiling, disagreement, evidence_refs)
    if remove_status == "insufficient_history":
        return ThresholdDecision(item, "no_change", "insufficient_history", existing.phase, existing.archetype, disagreement.classification_ceiling, disagreement, evidence_refs)
    if remove_status == "secondary_present":
        return ThresholdDecision(item, "no_change", "secondary_present", existing.phase, existing.archetype, disagreement.classification_ceiling, disagreement, evidence_refs)
    return ThresholdDecision(item, "no_change", "thresholds_not_met", existing.phase, existing.archetype, disagreement.classification_ceiling, disagreement, evidence_refs)


def _remove_status(item: str, stats: HeroStats, current: dict[str, SourceFetchResult]) -> str:
    if any(_current_presence(item, current.get(source)) is True for source in SECONDARY_SOURCES):
        return "secondary_present"
    rows = _healthy_rows_with_current(stats, current, item, PRIMARY_SOURCE)
    if len(rows) < REMOVE_BAZAARDB_ABSENT_PATCHES:
        return "insufficient_history"
    recent = rows[-REMOVE_BAZAARDB_ABSENT_PATCHES:]
    if any(row.present for row in recent):
        return "no_change"
    observed = [_parse_time(row.observed_at) for row in recent]
    if (max(observed) - min(observed)).days < REMOVE_MIN_ABSENT_DAYS:
        return "no_change"
    if any(_source_has_present_history(stats, source, item) for source in SECONDARY_SOURCES):
        return "secondary_present"
    return "remove_candidate"


def _healthy_seen_count(
    stats: HeroStats,
    current: dict[str, SourceFetchResult],
    item: str,
    source: str,
    limit: int,
) -> int:
    rows = _healthy_rows_with_current(stats, current, item, source)
    return sum(1 for row in rows[-limit:] if row.present)


def _healthy_rows_with_current(
    stats: HeroStats,
    current: dict[str, SourceFetchResult],
    item: str,
    source: str,
) -> list[WindowItemStats]:
    summaries = _healthy_source_summaries(stats, current, source)
    history = {row.window_id: row for row in stats.item_history(item, source)}
    current_result = current.get(source)
    if current_result and current_result.status == HEALTHY:
        observed_items = {row.item: row for row in current_result.observation.items}
        observed_item = observed_items.get(item)
        history[current_result.observation.window_id] = WindowItemStats(
            window_id=current_result.observation.window_id,
            observed_at=current_result.observation.observed_at,
            present=bool(observed_item and observed_item.present),
            phase=observed_item.phase if observed_item else None,
            archetype=observed_item.archetype if observed_item else None,
            appearances=observed_item.appearances if observed_item else None,
            sample_count=observed_item.sample_count if observed_item else None,
            frequency=observed_item.frequency if observed_item else None,
            rank=observed_item.rank if observed_item else None,
            archetypes_seen=list(observed_item.archetypes_seen) if observed_item else [],
            evidence_refs=list(observed_item.evidence_refs) if observed_item else [],
            metadata=dict(observed_item.metadata) if observed_item else {},
        )
    return [
        history.get(summary.window_id)
        or WindowItemStats(summary.window_id, summary.observed_at, False)
        for summary in summaries
    ]


def _healthy_source_summaries(
    stats: HeroStats,
    current: dict[str, SourceFetchResult],
    source: str,
) -> list[SourceWindowSummary]:
    rows = [
        row
        for row in stats.source_windows.get(source, [])
        if row.health_status == HEALTHY
    ]
    current_result = current.get(source)
    if current_result and current_result.status == HEALTHY:
        rows = [row for row in rows if row.window_id != current_result.observation.window_id]
        rows.append(
            SourceWindowSummary(
                window_id=current_result.observation.window_id,
                observed_at=current_result.observation.observed_at,
                health_status=HEALTHY,
                artifact_ref=current_result.observation.artifact_ref,
                details=list(current_result.observation.details),
            )
        )
    return rows


def _current_index(source_results: Iterable[SourceFetchResult]) -> dict[str, SourceFetchResult]:
    indexed: dict[str, SourceFetchResult] = {}
    for result in source_results:
        source = result.observation.window_id.split(":", 1)[0]
        indexed[source] = result
    return indexed


def _catalog_index(catalog_items: Iterable[CatalogItem | dict[str, Any]]) -> dict[str, CatalogItem]:
    indexed = {}
    for item in catalog_items:
        row = CatalogItem.from_dict(item) if isinstance(item, dict) else item
        indexed[row.item] = row
    return indexed


def _current_presence(item: str, result: Optional[SourceFetchResult]) -> Optional[bool]:
    if result is None or result.status != HEALTHY:
        return None
    return any(row.item == item and row.present for row in result.observation.items)


def _combine_presence(values: list[Optional[bool]]) -> Optional[bool]:
    if any(value is True for value in values):
        return True
    if any(value is False for value in values):
        return False
    return None


def _current_source_support_count(item: str, current: dict[str, SourceFetchResult]) -> int:
    groups = [
        _current_presence(item, current.get(PRIMARY_SOURCE)) is True,
        any(_current_presence(item, current.get(source)) is True for source in MOBALYTICS_SOURCES),
        _current_presence(item, current.get("bazaar_builds_net")) is True,
    ]
    return sum(1 for value in groups if value)


def _source_has_present_history(stats: HeroStats, source: str, item: str) -> bool:
    return any(row.present for row in stats.item_history(item, source))


def _evidence_refs(item: str, current: dict[str, SourceFetchResult]) -> list[str]:
    refs: list[str] = []
    for result in current.values():
        for row in result.observation.items:
            if row.item == item:
                refs.extend(row.evidence_refs)
                if result.observation.artifact_ref:
                    refs.append(result.observation.artifact_ref)
    return sorted(set(refs))


def _existing_core_or_carry(item: CatalogItem) -> bool:
    value = f"{item.phase or ''} {item.archetype or ''}".casefold()
    return "core" in value or "carry" in value


def _archetype_changed(item: str, existing: CatalogItem, current: dict[str, SourceFetchResult]) -> bool:
    if not existing.archetype:
        return False
    current_archetypes = {
        row.archetype
        for result in current.values()
        if result.status == HEALTHY
        for row in result.observation.items
        if row.item == item and row.present and row.archetype
    }
    return bool(current_archetypes and existing.archetype not in current_archetypes)


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    return str(value)


if __name__ == "__main__":
    raise SystemExit(main())
