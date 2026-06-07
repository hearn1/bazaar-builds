"""Threshold evaluation for automated build refresh candidates."""

from __future__ import annotations

import argparse
import dataclasses
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from automated_builds_pipeline.game_changes import GameChangeSignal, GameChangeSignals
from automated_builds_pipeline.sources.base import HEALTHY, SourceFetchResult
from automated_builds_pipeline.state import CuratorState, load_state
from automated_builds_pipeline.stats import (
    HeroStats,
    ItemWindowEvidence,
    SUPPORTED_SOURCES,
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
BAZAARDB_STRONG_APPEARANCES = 10
BAZAARDB_STRONG_FREQUENCY = 0.20
BAZAARDB_CORE_SAMPLE = 20
REMOVE_BAZAARDB_ABSENT_WINDOWS = 2
REMOVE_MIN_ABSENT_DAYS = 30
REASON_MAP = {
    "bazaardb_current_strong": "bazaardb_current_patch_strong",
    "bazaardb_2_of_3": "bazaardb_present_2_of_3_patches",
    "mobalytics_current": "mobalytics_current_build",
    "bazaar_builds_net_2_of_3": "bazaar_builds_net_2_of_3_windows",
    "mixed_current_sources": "mobalytics_current_build",
    "bazaardb_absent_30_days_secondaries_clear": "bazaardb_absent_30_days",
    "secondary_present": "secondary_present_bazaardb_absent",
    "primary_absent_secondary_present_preserve_existing_classification": "secondary_present_bazaardb_absent",
    "insufficient_history": "not_enough_windows",
    "freeze_active": "none",
    "archetype_change_unresolved": "none",
    "below_add_threshold": "none",
    "thresholds_not_met": "none",
    "game_change_removed_card": "game_change_removed_card",
    "game_change_renamed_card": "game_change_renamed_card",
    "game_change_explicit_invalidation": "game_change_explicit_invalidation",
    "game_change_major_nerf": "game_change_major_nerf",
    "game_change_watchlist": "game_change_watchlist",
}


@dataclass
class CatalogItem:
    item: str
    phase: Optional[str] = None
    archetype: Optional[str] = None
    bucket: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CatalogItem":
        return cls(
            item=str(data["item"]),
            phase=_optional_str(data.get("phase")),
            archetype=_optional_str(data.get("archetype")),
            bucket=_optional_str(data.get("bucket")),
        )


@dataclass(frozen=True)
class CatalogContext:
    hero: str
    items: frozenset[str]
    archetypes: frozenset[str]
    anchor_mode: str = "catalog"


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

    @property
    def label(self) -> str:
        if self.rationale in {"secondary_present_primary_absent", "secondary_present_primary_unknown"}:
            return "secondary_present_bazaardb_absent"
        if self.rationale == "primary_present" and (self.mobalytics is False or self.bazaar_builds_net is False):
            return "bazaardb_present_secondary_absent"
        return "none"


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
    blocked_by: list[str] = field(default_factory=list)
    signal_evidence: list[dict[str, Any]] = field(default_factory=list)
    actionability_override: Optional[str] = None
    review_priority: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "item": self.item,
            "threshold_result": self.threshold_result,
            "threshold_reason": self.threshold_reason,
            "classification_ceiling": self.classification_ceiling,
            "evidence_refs": _evidence_ref_objects(self.evidence_refs),
            "removal_blocked_by": self.removal_blocked_by,
            "disagreement": self.disagreement.label if self.disagreement else "none",
        }

    @property
    def threshold_result(self) -> str:
        if self.reason == "freeze_active":
            return "blocked"
        if self.reason == "insufficient_history":
            return "insufficient_history"
        return self.action

    @property
    def threshold_reason(self) -> str:
        return REASON_MAP.get(self.reason, "none")

    @property
    def removal_blocked_by(self) -> list[str]:
        blocked_by = list(self.blocked_by)
        if self.reason == "freeze_active":
            blocked_by.append("freeze_removals")
        if self.reason in {
            "secondary_present",
            "primary_absent_secondary_present_preserve_existing_classification",
        }:
            blocked_by.extend(MOBALYTICS_SOURCES)
            blocked_by.append("bazaar_builds_net")
        return blocked_by


@dataclass
class EvaluationResult:
    hero: str
    freeze_active: bool
    generated_at: str
    run_id: str
    bazaardb_patch: Optional[dict[str, Any]] = None
    source_health: list[dict[str, Any]] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[ThresholdDecision] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "generated_at": self.generated_at,
            "run_id": self.run_id,
            "hero": self.hero,
            "bazaardb_patch": self.bazaardb_patch,
            "source_health": list(self.source_health),
            "rows": list(self.rows),
        }


def evaluate_hero(
    hero: str,
    catalog_items: Iterable[CatalogItem | dict[str, Any]],
    stats: HeroStats,
    source_results: Iterable[SourceFetchResult],
    state: Optional[CuratorState] = None,
    *,
    now: Optional[datetime] = None,
    game_change_signals: Optional[GameChangeSignals] = None,
) -> EvaluationResult:
    state = state or CuratorState()
    _now = now or datetime.now(timezone.utc)
    generated_at = _format_utc(_now)
    observation_date = _now.date()
    current = _current_index(source_results)
    catalog_items_list = list(catalog_items)
    catalog = _catalog_index(catalog_items_list)
    context = _catalog_context(hero, catalog.values())
    commitment_index = _archetype_commitment_index(catalog_items_list)
    catalog_item_index = _catalog_item_index(catalog_items_list)
    current = _hero_scoped_current(current, context)
    signals = game_change_signals or GameChangeSignals()
    freeze = state.freeze_status(hero, now)
    freeze_active = freeze.active
    unresolved: list[str] = []
    if freeze_active:
        unresolved.append(f"freeze_active:{freeze.scope}:{freeze.until}")

    all_items = set(catalog) | _stats_items_for_context(stats, context)
    all_items.update(signal.item for signal in signals.for_hero(hero) if signal.item in catalog)
    for source_result in current.values():
        all_items.update(item.item for item in source_result.observation.items if item.present)

    decisions: list[ThresholdDecision] = []
    rows: list[dict[str, Any]] = []
    for item in sorted(all_items):
        disagreement = classify_source_disagreement(item, current)
        existing = catalog.get(item)
        if existing is None:
            decision = _evaluate_add(item, stats, current, disagreement, context)
            if decision.threshold_result == "add_candidate":
                parts = _observed_archetype_labels(item, current)
                observed_phase = _observed_phase(item, current) or _default_candidate_phase(decision, existing)
                resolved = _resolve_cluster_archetypes(parts, observed_phase, commitment_index)
                if resolved:
                    for rphase, rname in resolved:
                        routed = dataclasses.replace(decision, phase=rphase, archetype=rname)
                        decisions.append(routed)
                        rows.append(_row_dict(hero, routed, existing, current, stats, catalog_item_index))
                    continue
        else:
            decision = _evaluate_existing(item, existing, stats, current, disagreement, freeze_active)
            for signal_decision in _evaluate_game_change_signals(
                hero,
                item,
                existing,
                disagreement,
                current,
                freeze_active,
                signals,
                observation_date,
            ):
                decisions.append(signal_decision)
                rows.append(_row_dict(hero, signal_decision, existing, current, stats, catalog_item_index))
        decisions.append(decision)
        rows.append(_row_dict(hero, decision, existing, current, stats, catalog_item_index))

    return EvaluationResult(
        hero=hero,
        freeze_active=freeze_active,
        generated_at=generated_at,
        run_id=_run_id(generated_at),
        bazaardb_patch=_bazaardb_patch(current.get(PRIMARY_SOURCE), state),
        source_health=_source_health(current),
        rows=rows,
        decisions=decisions,
        unresolved=unresolved,
    )


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
        return SourceDisagreement(bazaardb, mobalytics, bazaar_builds_net, "carry_core_support", "primary_present")
    if bazaardb is False and (mobalytics is True or bazaar_builds_net is True):
        return SourceDisagreement(bazaardb, mobalytics, bazaar_builds_net, "support_only", "secondary_present_primary_absent")
    if bazaardb is False:
        return SourceDisagreement(bazaardb, mobalytics, bazaar_builds_net, "not_applicable", "all_available_sources_clear")
    if mobalytics is True or bazaar_builds_net is True:
        return SourceDisagreement(bazaardb, mobalytics, bazaar_builds_net, "support_only", "secondary_present_primary_unknown")
    return SourceDisagreement(bazaardb, mobalytics, bazaar_builds_net, "not_applicable", "primary_unknown")


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
    if isinstance(data, list):
        return [CatalogItem.from_dict(item) for item in data]
    if not isinstance(data, dict):
        raise ValueError("catalog items must be a list or an object")
    return list(iter_catalog_items(data))


def iter_catalog_items(catalog: dict[str, Any]) -> Iterable[CatalogItem]:
    """Walk a tracker catalog (game_phases shape) plus back-compat fixture shapes."""
    game_phases = catalog.get("game_phases")
    if isinstance(game_phases, dict):
        for phase_name, phase_data in game_phases.items():
            if not isinstance(phase_data, dict):
                continue
            for archetype in phase_data.get("archetypes", []) or []:
                if not isinstance(archetype, dict):
                    continue
                arch_name = archetype.get("name")
                for bucket in ("carry_items", "core_items", "support_items", "condition_items"):
                    for item in archetype.get(bucket, []) or []:
                        if item:
                            yield CatalogItem(item=str(item), phase=phase_name, archetype=arch_name, bucket=bucket)
            for bucket in ("universal_utility_items", "economy_items"):
                for item in phase_data.get(bucket, []) or []:
                    if item:
                        yield CatalogItem(item=str(item), phase=phase_name, archetype=None, bucket=bucket)

    raw_items = catalog.get("items")
    if isinstance(raw_items, list):
        for item in raw_items:
            if isinstance(item, dict) and item.get("item"):
                yield CatalogItem.from_dict(item)

    for archetype in catalog.get("archetypes", []) or []:
        if not isinstance(archetype, dict):
            continue
        phase = archetype.get("phase")
        arch_name = archetype.get("archetype") or archetype.get("tag") or archetype.get("name")
        for bucket in ("carry_items", "core_items", "support_items", "condition_items"):
            for item in archetype.get(bucket, []) or []:
                if item:
                    yield CatalogItem(item=str(item), phase=phase, archetype=arch_name, bucket=bucket)


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
    context: CatalogContext,
) -> ThresholdDecision:
    evidence_refs = _evidence_refs(item, current)
    classification_ceiling = _add_classification_ceiling(item, disagreement, current, context)
    if _current_bazaardb_strength(item, current) in {"statistical_core", "statistical_strong"}:
        return ThresholdDecision(item, "add_candidate", "bazaardb_current_strong", classification_ceiling=classification_ceiling, disagreement=disagreement, evidence_refs=evidence_refs)
    if _healthy_seen_count(
        stats,
        current,
        item,
        PRIMARY_SOURCE,
        ADD_BAZAARDB_WINDOW_COUNT,
        context=context,
        require_scoped_history=True,
    ) >= ADD_BAZAARDB_MIN_SEEN:
        return ThresholdDecision(item, "add_candidate", "bazaardb_2_of_3", classification_ceiling=classification_ceiling, disagreement=disagreement, evidence_refs=evidence_refs)
    if any(_current_presence(item, current.get(source)) is True for source in MOBALYTICS_SOURCES):
        return ThresholdDecision(item, "add_candidate", "mobalytics_current", classification_ceiling=classification_ceiling, disagreement=disagreement, evidence_refs=evidence_refs)
    if _healthy_seen_count(stats, current, item, "bazaar_builds_net", ADD_BAZAAR_BUILDS_NET_WINDOW_COUNT) >= ADD_BAZAAR_BUILDS_NET_MIN_SEEN:
        return ThresholdDecision(item, "add_candidate", "bazaar_builds_net_2_of_3", classification_ceiling=classification_ceiling, disagreement=disagreement, evidence_refs=evidence_refs)
    if _current_source_support_count(item, current) >= 2:
        return ThresholdDecision(item, "add_candidate", "mixed_current_sources", classification_ceiling=classification_ceiling, disagreement=disagreement, evidence_refs=evidence_refs)
    return ThresholdDecision(item, "no_change", "below_add_threshold", classification_ceiling=classification_ceiling, disagreement=disagreement, evidence_refs=evidence_refs)


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

    if _preserve_existing_bucket_on_secondary_presence(existing) and disagreement.classification_ceiling == "support_only":
        return ThresholdDecision(item, "no_change", "primary_absent_secondary_present_preserve_existing_classification", existing.phase, existing.archetype, disagreement.classification_ceiling, disagreement, evidence_refs)

    if _archetype_changed(item, existing, current):
        return ThresholdDecision(item, "no_change", "archetype_change_unresolved", existing.phase, existing.archetype, disagreement.classification_ceiling, disagreement, evidence_refs, ["archetype_reshuffle_deferred"])

    remove_status = _remove_status(item, stats, current)
    if remove_status == "remove_candidate":
        return ThresholdDecision(item, _retirement_action(existing), "bazaardb_absent_30_days_secondaries_clear", existing.phase, existing.archetype, disagreement.classification_ceiling, disagreement, evidence_refs)
    if remove_status == "insufficient_history":
        return ThresholdDecision(item, "no_change", "insufficient_history", existing.phase, existing.archetype, disagreement.classification_ceiling, disagreement, evidence_refs)
    if remove_status == "secondary_present":
        return ThresholdDecision(item, "no_change", "secondary_present", existing.phase, existing.archetype, disagreement.classification_ceiling, disagreement, evidence_refs)
    return ThresholdDecision(item, "no_change", "thresholds_not_met", existing.phase, existing.archetype, disagreement.classification_ceiling, disagreement, evidence_refs)


def _remove_status(item: str, stats: HeroStats, current: dict[str, SourceFetchResult]) -> str:
    if _current_presence(item, current.get(PRIMARY_SOURCE)) is not False:
        return "no_change"
    if any(_current_presence(item, current.get(source)) is True for source in SECONDARY_SOURCES):
        return "secondary_present"
    rows = _healthy_rows_with_current(stats, current, item, PRIMARY_SOURCE)
    last_present_index = max((index for index, row in enumerate(rows) if row.present), default=-1)
    absence_rows = [row for row in rows[last_present_index + 1 :] if not row.present]
    if len(absence_rows) < REMOVE_BAZAARDB_ABSENT_WINDOWS:
        return "insufficient_history"
    observed = [_parse_time(row.observed_at) for row in absence_rows]
    if (max(observed) - min(observed)).days < REMOVE_MIN_ABSENT_DAYS:
        return "no_change"
    return "remove_candidate"


def _evaluate_game_change_signals(
    hero: str,
    item: str,
    existing: CatalogItem,
    disagreement: SourceDisagreement,
    current: dict[str, SourceFetchResult],
    freeze_active: bool,
    signals: GameChangeSignals,
    observation_date: Optional[date] = None,
) -> list[ThresholdDecision]:
    decisions: list[ThresholdDecision] = []
    for signal in signals.matching(hero=hero, item=item):
        # Scope renamed/removed signals by effective_date: if a healthy bazaardb run
        # confirms the item is present on or after the signal's effective_date, the name
        # has been reused by a new card and the retirement signal must not fire.
        # Comparison basis: signal.effective_date vs. pipeline observation_date (now.date()).
        if (
            signal.type in {"renamed_card", "removed_card"}
            and disagreement.bazaardb is True
            and observation_date is not None
            and signal.effective_date <= observation_date
        ):
            continue
        action = _signal_retirement_action(signal, existing)
        if action is None:
            continue
        blocked_by = ["freeze_removals"] if freeze_active and action == "remove_candidate" else []
        decisions.append(
            ThresholdDecision(
                item,
                action,
                f"game_change_{signal.type}",
                existing.phase,
                existing.archetype,
                disagreement.classification_ceiling,
                disagreement,
                _signal_evidence_refs(signal, current),
                blocked_by=blocked_by,
                signal_evidence=[_signal_evidence(signal)],
                actionability_override=_signal_actionability(signal, existing, freeze_active),
                review_priority=_signal_review_priority(signal, existing),
            )
        )
    return decisions


def _signal_retirement_action(signal: GameChangeSignal, existing: CatalogItem) -> Optional[str]:
    role = _retirement_role(existing.bucket)
    if signal.type in {"removed_card", "renamed_card"} and role["actionability"] == "item_removal_candidate":
        return "remove_candidate"
    if signal.type in {"removed_card", "renamed_card", "explicit_invalidation", "major_nerf", "watchlist"}:
        return "retirement_review_candidate"
    return None


def _signal_actionability(signal: GameChangeSignal, existing: CatalogItem, freeze_active: bool) -> str:
    role = _retirement_role(existing.bucket)
    if freeze_active and signal.type in {"removed_card", "renamed_card"} and role["actionability"] == "item_removal_candidate":
        return "freeze_blocked"
    if signal.type in {"major_nerf", "watchlist"}:
        return "watchlist_review"
    if signal.type == "explicit_invalidation":
        return "review_required"
    return role["actionability"]


def _signal_review_priority(signal: GameChangeSignal, existing: CatalogItem) -> str:
    if signal.type == "explicit_invalidation" and existing.bucket in {"carry_items", "core_items", "condition_items"}:
        return "high"
    if signal.type in {"major_nerf", "watchlist"}:
        return "watchlist"
    return "normal"


def _signal_evidence(signal: GameChangeSignal) -> dict[str, Any]:
    return signal.to_dict()


def _signal_evidence_refs(signal: GameChangeSignal, current: dict[str, SourceFetchResult]) -> list[str]:
    refs = _evidence_refs(signal.item, current)
    refs.append(f"game_changes:{signal.id}")
    refs.append(signal.source_url)
    return sorted(set(refs))


def _healthy_seen_count(
    stats: HeroStats,
    current: dict[str, SourceFetchResult],
    item: str,
    source: str,
    limit: int,
    *,
    context: Optional[CatalogContext] = None,
    require_scoped_history: bool = False,
) -> int:
    rows = _healthy_rows_with_current(stats, current, item, source)
    if require_scoped_history and context is not None:
        rows = [row for row in rows if _history_row_matches_context(item, row, source, context)]
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


def _catalog_context(hero: str, catalog_items: Iterable[CatalogItem]) -> CatalogContext:
    return CatalogContext(
        hero=hero,
        items=frozenset(item.item for item in catalog_items if item.item),
        archetypes=frozenset(item.archetype for item in catalog_items if item.archetype),
    )


def _hero_scoped_current(
    current: dict[str, SourceFetchResult],
    context: CatalogContext,
) -> dict[str, SourceFetchResult]:
    anchor_context = _seed_anchor_context(context.hero, current) if _empty_context(context) else context
    scoped = {}
    for source, result in current.items():
        items = [
            row
            for row in result.observation.items
            if _current_row_matches_context(row, source, anchor_context)
        ]
        observation = WindowObservation(
            window_id=result.observation.window_id,
            observed_at=result.observation.observed_at,
            items=items,
            artifact_ref=result.observation.artifact_ref,
            health_status=result.observation.health_status,
            details=list(result.observation.details),
        )
        scoped[source] = SourceFetchResult(
            observation=observation,
            status=result.status,
            details=list(result.details),
            patch_label=result.patch_label,
        )
    return scoped


def _empty_context(context: CatalogContext) -> bool:
    return not context.items and not context.archetypes


def _seed_anchor_context(hero: str, current: dict[str, SourceFetchResult]) -> CatalogContext:
    """Build first-catalog anchors from hero-scoped secondary sources.

    Empty seed catalogs have no curator-authored archetypes yet, so primary
    bazaardb rows need a deterministic cross-source anchor instead of the normal
    existing-catalog context. Mobalytics/bazaar-builds rows are already fetched
    for the target hero and can safely constrain the primary source surface.
    """
    items: set[str] = set()
    archetypes: set[str] = set()
    for source, result in current.items():
        if source == PRIMARY_SOURCE or result.status != HEALTHY:
            continue
        for row in result.observation.items:
            if not row.present:
                continue
            metadata_hero = _metadata_hero(row.metadata)
            if metadata_hero and metadata_hero.casefold() != hero.casefold():
                continue
            items.add(row.item)
            if row.archetype:
                archetypes.add(row.archetype)
            archetypes.update(value for value in row.archetypes_seen if value)
    return CatalogContext(hero=hero, items=frozenset(items), archetypes=frozenset(archetypes), anchor_mode="seed")


def _stats_items_for_context(stats: HeroStats, context: CatalogContext) -> set[str]:
    items: set[str] = set()
    for item, history in stats.items.items():
        for source, source_history in history.per_source.items():
            if source != PRIMARY_SOURCE:
                items.add(item)
                continue
            if any(_history_row_matches_context(item, row, source, context) for row in source_history.per_window):
                items.add(item)
                break
    return items


def _current_row_matches_context(row: ItemWindowEvidence, source: str, context: CatalogContext) -> bool:
    metadata_hero = _metadata_hero(row.metadata)
    if metadata_hero and metadata_hero.casefold() != context.hero.casefold():
        return False
    if source != PRIMARY_SOURCE:
        return True
    if context.anchor_mode == "seed":
        return _evidence_matches_seed_context(row.item, row.archetype, row.archetypes_seen, context)
    return _evidence_matches_catalog_context(row.item, row.archetype, row.archetypes_seen, context)


def _history_row_matches_context(
    item: str,
    row: WindowItemStats,
    source: str,
    context: CatalogContext,
) -> bool:
    if not row.present:
        return True
    metadata_hero = _metadata_hero(row.metadata)
    if metadata_hero and metadata_hero.casefold() != context.hero.casefold():
        return False
    if source != PRIMARY_SOURCE:
        return True
    return _evidence_matches_catalog_context(item, row.archetype, row.archetypes_seen, context)


def _evidence_matches_catalog_context(
    item: str,
    archetype: Optional[str],
    archetypes_seen: Iterable[str],
    context: CatalogContext,
) -> bool:
    if item in context.items:
        return True
    archetype_values = [value for value in [archetype, *list(archetypes_seen)] if value]
    for value in archetype_values:
        if value in context.archetypes:
            return True
        if any(part in context.items for part in _archetype_parts(value)):
            return True
    return False


def _evidence_matches_seed_context(
    item: str,
    archetype: Optional[str],
    archetypes_seen: Iterable[str],
    context: CatalogContext,
) -> bool:
    if item in context.items:
        return True
    archetype_values = [value for value in [archetype, *list(archetypes_seen)] if value]
    if any(value in context.archetypes for value in archetype_values):
        return True
    for value in archetype_values:
        if len(set(_archetype_parts(value)) & set(context.items)) >= 2:
            return True
    return False


def _archetype_parts(value: str) -> list[str]:
    return [part.strip() for part in value.replace(",", "/").split("/") if part.strip()]


def _archetype_commitment_index(
    catalog_items: Iterable[CatalogItem],
) -> dict[tuple[Optional[str], Optional[str]], frozenset[str]]:
    """Build a map of (phase, archetype_name) -> frozenset of carry+core item names.

    Used by the cluster-routing resolver to find existing archetypes whose
    commitment buckets overlap (≥2 items) with a new bazaardb slash-label.
    """
    index: dict[tuple[Optional[str], Optional[str]], set[str]] = {}
    for ci in catalog_items:
        if ci.archetype and ci.bucket in {"carry_items", "core_items"}:
            key = (ci.phase, ci.archetype)
            index.setdefault(key, set()).add(ci.item)
    return {key: frozenset(items) for key, items in index.items()}


def _catalog_item_index(
    catalog_items: Iterable[CatalogItem],
) -> dict[tuple[Optional[str], Optional[str]], list[CatalogItem]]:
    index: dict[tuple[Optional[str], Optional[str]], list[CatalogItem]] = {}
    for item in catalog_items:
        index.setdefault((item.phase, item.archetype), []).append(item)
    return index


def _observed_archetype_labels(item: str, current: dict[str, SourceFetchResult]) -> frozenset[str]:
    """Collect all archetype label parts seen for *item* across healthy sources."""
    parts: set[str] = set()
    for result in current.values():
        if result.status != HEALTHY:
            continue
        for row in result.observation.items:
            if row.item != item:
                continue
            for label in [row.archetype, *list(row.archetypes_seen)]:
                if label:
                    parts.update(_archetype_parts(label))
    return frozenset(parts)


def _resolve_cluster_archetypes(
    parts: frozenset[str],
    phase: Optional[str],
    commitment_index: dict[tuple[Optional[str], Optional[str]], frozenset[str]],
) -> list[tuple[Optional[str], Optional[str]]]:
    """Return catalog (phase, name) tuples whose carry+core items overlap ≥2 with *parts*.

    Prefer same-phase matches; fall back to cross-phase only when no same-phase
    candidate with ≥2 overlap exists.
    """
    if not parts:
        return []

    def matches(pred: Any) -> list[tuple[Optional[str], Optional[str]]]:
        return [
            (ph, name)
            for (ph, name), commit in commitment_index.items()
            if pred(ph) and len(parts & commit) >= 2
        ]

    same_phase = matches(lambda ph: ph == phase)
    return same_phase or matches(lambda ph: True)


def _metadata_hero(metadata: dict[str, Any]) -> Optional[str]:
    value = metadata.get("hero")
    return str(value) if value else None


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


def _evidence_refs(item: str, current: dict[str, SourceFetchResult]) -> list[str]:
    refs: list[str] = []
    for result in current.values():
        for row in result.observation.items:
            if row.item == item:
                refs.extend(row.evidence_refs)
                if result.observation.artifact_ref:
                    refs.append(result.observation.artifact_ref)
    return sorted(set(refs))


def _row_dict(
    hero: str,
    decision: ThresholdDecision,
    existing: Optional[CatalogItem],
    current: dict[str, SourceFetchResult],
    stats: HeroStats,
    catalog_item_index: dict[tuple[Optional[str], Optional[str]], list[CatalogItem]],
) -> dict[str, Any]:
    source_presence = _source_presence(decision.item, current)
    current_patch_evidence = _current_patch_evidence(decision.item, current)
    history = _history_summary(decision.item, stats)
    phase = decision.phase or _observed_phase(decision.item, current) or _default_candidate_phase(decision, existing)
    retirement = _retirement_metadata(decision, existing, catalog_item_index)
    return {
        "hero": hero,
        "phase": phase,
        "archetype": decision.archetype or _observed_archetype(decision.item, current),
        "archetype_status": _archetype_status(decision, existing),
        "item": decision.item,
        "catalog_bucket": existing.bucket if existing else None,
        "catalog_membership": "present" if existing else "missing",
        "source_presence": source_presence,
        "current_patch_evidence": current_patch_evidence,
        "within_patch_strength": _within_patch_strength(decision.item, current),
        "current_source_support_count": _current_source_support_count(decision.item, current),
        "canonical_presence": _canonical_presence(decision, source_presence),
        "classification_ceiling": decision.classification_ceiling,
        "threshold_result": decision.threshold_result,
        "threshold_reason": decision.threshold_reason,
        "removal_blocked_by": decision.removal_blocked_by,
        "disagreement": decision.disagreement.label if decision.disagreement else "none",
        "windows_seen": history["windows_seen"],
        "first_seen_window": history["first_seen_window"],
        "last_seen_window": history["last_seen_window"],
        "sample_count_latest": _latest_numeric(current_patch_evidence, "sample_count"),
        "appearances_latest": _latest_numeric(current_patch_evidence, "appearances"),
        "frequency_latest": _latest_numeric(current_patch_evidence, "frequency"),
        "mobalytics_description": _mobalytics_description(decision.item, current),
        "classifier_input_required": (
            decision.threshold_result == "add_candidate"
            and decision.classification_ceiling in {"carry_core_support", "support_only"}
        ),
        "llm_input_required": (
            decision.threshold_result == "add_candidate"
            and decision.classification_ceiling in {"carry_core_support", "support_only"}
        ),
        "evidence_refs": _evidence_ref_objects(decision.evidence_refs),
        **retirement,
    }


def _archetype_status(decision: ThresholdDecision, existing: Optional[CatalogItem]) -> str:
    if existing:
        return "existing"
    if decision.threshold_result == "add_candidate":
        return "candidate_new"
    return "unknown"


def _source_presence(item: str, current: dict[str, SourceFetchResult]) -> dict[str, str]:
    presence = {}
    for source in sorted(SUPPORTED_SOURCES):
        result = current.get(source)
        if result is None:
            presence[source] = "skipped"
        elif result.status != HEALTHY:
            presence[source] = result.status
        else:
            presence[source] = "present" if _current_presence(item, result) is True else "absent"
    return presence


def _current_patch_evidence(item: str, current: dict[str, SourceFetchResult]) -> dict[str, dict[str, Any]]:
    evidence: dict[str, dict[str, Any]] = {}
    for source in sorted(SUPPORTED_SOURCES):
        result = current.get(source)
        if result is None:
            evidence[source] = {"presence": "skipped"}
            continue
        if result.status != HEALTHY:
            evidence[source] = {"presence": result.status, "window_id": result.observation.window_id}
            continue
        observed = next((row for row in result.observation.items if row.item == item and row.present), None)
        payload: dict[str, Any] = {
            "presence": "present" if observed else "absent",
            "window_id": result.observation.window_id,
            "observed_at": result.observation.observed_at,
        }
        if observed:
            payload.update(
                _drop_none(
                    {
                        "item": observed.item,
                        "phase": observed.phase,
                        "archetype": observed.archetype,
                        "appearances": observed.appearances,
                        "sample_count": observed.sample_count,
                        "frequency": observed.frequency,
                        "rank": observed.rank,
                        "archetypes_seen": list(observed.archetypes_seen),
                        "evidence_refs": list(observed.evidence_refs),
                        "metadata": dict(observed.metadata),
                    }
                )
            )
        evidence[source] = payload
    return evidence


def _within_patch_strength(item: str, current: dict[str, SourceFetchResult]) -> str:
    bazaardb_strength = _current_bazaardb_strength(item, current)
    if bazaardb_strength in {"statistical_core", "statistical_strong"}:
        return bazaardb_strength
    if any(_current_presence(item, current.get(source)) is True for source in MOBALYTICS_SOURCES):
        return "editorial_confirmed"
    if _current_source_support_count(item, current) >= 2:
        return "multi_source_current"
    if bazaardb_strength != "none":
        return bazaardb_strength
    if any(_current_presence(item, current.get(source)) is True for source in EVALUATED_SOURCES):
        return "single_source_thin"
    return "none"


def _current_bazaardb_strength(item: str, current: dict[str, SourceFetchResult]) -> str:
    result = current.get(PRIMARY_SOURCE)
    if result is None or result.status != HEALTHY:
        return "none"
    observed = next((row for row in result.observation.items if row.item == item and row.present), None)
    if observed is None:
        return "none"
    section = str(observed.metadata.get("section") or "").upper()
    if section == "CORE ITEMS":
        sample = observed.sample_count or observed.appearances or 0
        return "statistical_core" if sample >= BAZAARDB_CORE_SAMPLE else "statistical_strong"
    appearances = observed.appearances or 0
    frequency = observed.frequency or 0.0
    if appearances >= BAZAARDB_STRONG_APPEARANCES and frequency >= BAZAARDB_STRONG_FREQUENCY:
        return "statistical_strong"
    return "single_source_thin"


def _add_classification_ceiling(
    item: str,
    disagreement: SourceDisagreement,
    current: dict[str, SourceFetchResult],
    context: CatalogContext,
) -> str:
    if disagreement.classification_ceiling != "support_only":
        return disagreement.classification_ceiling
    if not _empty_context(context):
        return disagreement.classification_ceiling
    if any(_current_presence(item, current.get(source)) is True for source in MOBALYTICS_SOURCES):
        return "carry_core_support"
    return disagreement.classification_ceiling


def _history_summary(item: str, stats: HeroStats) -> dict[str, Any]:
    windows_seen = 0
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None
    for source in sorted(SUPPORTED_SOURCES):
        for row in stats.item_history(item, source):
            if not row.present:
                continue
            windows_seen += 1
            first_seen = first_seen or row.window_id
            last_seen = row.window_id
    return {
        "windows_seen": windows_seen,
        "first_seen_window": first_seen,
        "last_seen_window": last_seen,
    }


def _mobalytics_description(item: str, current: dict[str, SourceFetchResult]) -> str:
    descriptions = []
    for source in MOBALYTICS_SOURCES:
        result = current.get(source)
        if result is None or result.status != HEALTHY:
            continue
        for row in result.observation.items:
            if row.item != item or not row.present:
                continue
            description = row.metadata.get("editorial_description")
            if description:
                descriptions.append(str(description))
    return "\n\n".join(descriptions)


def _latest_numeric(evidence: dict[str, dict[str, Any]], key: str) -> Optional[float | int]:
    for source in (PRIMARY_SOURCE, *MOBALYTICS_SOURCES, "bazaar_builds_net"):
        value = evidence.get(source, {}).get(key)
        if isinstance(value, (int, float)):
            return value
    return None


def _canonical_presence(decision: ThresholdDecision, source_presence: dict[str, str]) -> str:
    if source_presence[PRIMARY_SOURCE] == "present" or decision.threshold_result == "add_candidate":
        return "present"
    if decision.threshold_result in {"remove_candidate", "archetype_remove_candidate", "retirement_review_candidate"}:
        return "absent"
    if decision.disagreement and decision.disagreement.label != "none":
        return "disputed_present"
    if any(presence == "present" for presence in source_presence.values()):
        return "present"
    if any(presence in {"unknown", "unhealthy"} for presence in source_presence.values()):
        return "unknown"
    return "unknown"


def _observed_archetype(item: str, current: dict[str, SourceFetchResult]) -> Optional[str]:
    for result in current.values():
        if result.status != HEALTHY:
            continue
        for row in result.observation.items:
            if row.item == item and row.archetype:
                return row.archetype
    return None


def _observed_phase(item: str, current: dict[str, SourceFetchResult]) -> Optional[str]:
    for result in current.values():
        if result.status != HEALTHY:
            continue
        for row in result.observation.items:
            if row.item == item and row.phase:
                return row.phase
    return None


def _default_candidate_phase(decision: ThresholdDecision, existing: Optional[CatalogItem]) -> Optional[str]:
    if existing is None and decision.threshold_result == "add_candidate":
        return "late"
    return None


def _source_health(current: dict[str, SourceFetchResult]) -> list[dict[str, Any]]:
    return [
        {
            "source": source,
            "status": result.status,
            "window_id": result.observation.window_id,
            "checked_at": result.observation.observed_at,
            "details": list(result.details or result.observation.details),
        }
        for source, result in sorted(current.items())
    ]


def _bazaardb_patch(result: Optional[SourceFetchResult], state: CuratorState) -> Optional[dict[str, Any]]:
    if result is None:
        return None
    expected = state.expected_bazaardb_patch_label
    return {
        "label": result.patch_label,
        "patch_notes_url": None,
        "expected_label": expected,
        "matched_expected": expected is None or result.patch_label == expected,
    }


def _evidence_ref_objects(refs: list[str]) -> list[dict[str, Optional[str]]]:
    objects = []
    for ref in refs:
        source = _source_from_ref(ref)
        objects.append({"source": source, "artifact_ref": ref, "summary": ref})
    return objects


def _source_from_ref(ref: str) -> str:
    for source in sorted(SUPPORTED_SOURCES):
        if ref.startswith(source) or f"/{source}" in ref or f"\\{source}" in ref:
            return source
    return "in_house_tracker"


def _format_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _run_id(generated_at: str) -> str:
    return generated_at.replace("-", "").replace(":", "")


def _preserve_existing_bucket_on_secondary_presence(item: CatalogItem) -> bool:
    return item.bucket in {"carry_items", "core_items", "condition_items"}


def _retirement_action(item: CatalogItem) -> str:
    role = _retirement_role(item.bucket)
    if role["actionability"] == "item_removal_candidate":
        return "remove_candidate"
    return "retirement_review_candidate"


def _retirement_metadata(
    decision: ThresholdDecision,
    existing: Optional[CatalogItem],
    catalog_item_index: dict[tuple[Optional[str], Optional[str]], list[CatalogItem]],
) -> dict[str, Any]:
    if existing is None or decision.threshold_result not in {
        "remove_candidate",
        "archetype_remove_candidate",
        "retirement_review_candidate",
    }:
        return {
            "retirement_type": None,
            "retirement_basis": None,
            "actionability": None,
            "affected_items": [],
            "affected_item_details": [],
            "affected_build_items": {},
            "review_scope": None,
            "review_priority": None,
            "signal_evidence": [],
        }
    role = _retirement_role(existing.bucket)
    actionability = decision.actionability_override or role["actionability"]
    affected_build_items = (
        _affected_build_items(existing, catalog_item_index)
        if actionability != "item_removal_candidate"
        else {}
    )
    affected_detail = {
        "item": decision.item,
        "catalog_bucket": existing.bucket,
        "phase": existing.phase,
        "archetype": existing.archetype,
    }
    replacement = _replacement_item(decision.signal_evidence)
    if replacement:
        affected_detail["replacement_item"] = replacement
    return {
        "retirement_type": role["retirement_type"],
        "retirement_basis": decision.threshold_reason,
        "actionability": actionability,
        "affected_items": [decision.item],
        "affected_item_details": [affected_detail],
        "affected_build_items": affected_build_items,
        "review_scope": role["review_scope"],
        "review_priority": decision.review_priority or "normal",
        "signal_evidence": list(decision.signal_evidence),
    }


def _replacement_item(signal_evidence: list[dict[str, Any]]) -> Optional[str]:
    for signal in signal_evidence:
        replacement = signal.get("replacement_item")
        if replacement:
            return str(replacement)
    return None


def _retirement_role(bucket: Optional[str]) -> dict[str, str]:
    roles = {
        "support_items": {
            "retirement_type": "support_item",
            "actionability": "item_removal_candidate",
            "review_scope": "support_item",
        },
        "carry_items": {
            "retirement_type": "whole_build_review",
            "actionability": "review_required",
            "review_scope": "whole_build",
        },
        "core_items": {
            "retirement_type": "core_item_review",
            "actionability": "review_required",
            "review_scope": "core_item",
        },
        "condition_items": {
            "retirement_type": "condition_item_review",
            "actionability": "review_required",
            "review_scope": "condition_item",
        },
        "universal_utility_items": {
            "retirement_type": "support_like_phase_item",
            "actionability": "review_required",
            "review_scope": "phase_item",
        },
        "economy_items": {
            "retirement_type": "support_like_phase_item",
            "actionability": "review_required",
            "review_scope": "phase_item",
        },
    }
    return roles.get(
        bucket,
        {
            "retirement_type": "catalog_item",
            "actionability": "item_removal_candidate",
            "review_scope": "catalog_item",
        },
    )


def _affected_build_items(
    existing: CatalogItem,
    catalog_item_index: dict[tuple[Optional[str], Optional[str]], list[CatalogItem]],
) -> dict[str, list[str]]:
    if existing.archetype is None:
        return {
            existing.bucket or "catalog_items": [existing.item],
        }
    grouped: dict[str, list[str]] = {
        "carry_items": [],
        "core_items": [],
        "condition_items": [],
    }
    for item in catalog_item_index.get((existing.phase, existing.archetype), []):
        if item.bucket in grouped and item.item not in grouped[item.bucket]:
            grouped[item.bucket].append(item.item)
    if existing.bucket in grouped and existing.item not in grouped[existing.bucket]:
        grouped[existing.bucket].append(existing.item)
    return {bucket: items for bucket, items in grouped.items() if items}


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


def _drop_none(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if value is not None}


if __name__ == "__main__":
    raise SystemExit(main())
