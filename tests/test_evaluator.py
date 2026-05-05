from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from automated_builds_pipeline.evaluator import (
    CatalogItem,
    classify_source_disagreement,
    evaluate_hero,
    hydrate_source_fetch_result,
)
from automated_builds_pipeline.sources.base import SourceFetchResult
from automated_builds_pipeline.state import CuratorState, FreezeWindow
from automated_builds_pipeline.stats import HeroStats, ItemWindowEvidence, WindowObservation, append_window


OBSERVED_AT = "2026-05-05T12:00:00Z"


def result(source: str, items: list[str], *, status: str = "healthy", observed_at: str = OBSERVED_AT) -> SourceFetchResult:
    return SourceFetchResult(
        observation=WindowObservation(
            window_id=f"{source}:current",
            observed_at=observed_at,
            artifact_ref=f"artifacts/{source}.json",
            items=[ItemWindowEvidence(item=item, archetype="Observed Archetype", evidence_refs=[f"{source}:{item}"]) for item in items],
        ),
        status=status,
        details=[],
    )


def decision_for(evaluation, item):
    return next(decision for decision in evaluation.decisions if decision.item == item)


@pytest.mark.parametrize(
    ("bazaardb", "mobalytics", "bazaar_builds_net", "ceiling", "rationale"),
    [
        (True, True, True, "core_or_carry", "primary_present"),
        (True, True, False, "core_or_carry", "primary_present"),
        (True, False, True, "core_or_carry", "primary_present"),
        (True, False, False, "core_or_carry", "primary_present"),
        (False, True, True, "support_only", "secondary_present_primary_absent"),
        (False, True, False, "support_only", "secondary_present_primary_absent"),
        (False, False, True, "support_only", "secondary_present_primary_absent"),
        (False, False, False, "remove_eligible", "all_available_sources_clear"),
    ],
)
def test_source_disagreement_precedence_table(bazaardb, mobalytics, bazaar_builds_net, ceiling, rationale):
    item = "Test Item"
    current = {
        "bazaardb": result("bazaardb", [item] if bazaardb else []),
        "mobalytics_meta_builds": result("mobalytics_meta_builds", [item] if mobalytics else []),
        "bazaar_builds_net": result("bazaar_builds_net", [item] if bazaar_builds_net else []),
    }

    disagreement = classify_source_disagreement(item, current)

    assert disagreement.classification_ceiling == ceiling
    assert disagreement.rationale == rationale


def test_add_candidate_from_bazaardb_two_of_three():
    stats = history_with_windows("Karnok", "bazaardb", "Pufferfish", [True, False])

    evaluation = evaluate_hero("Karnok", [], stats, [result("bazaardb", ["Pufferfish"])])

    decision = decision_for(evaluation, "Pufferfish")
    assert decision.action == "add_candidate"
    assert decision.reason == "bazaardb_2_of_3"


def test_add_candidate_from_mobalytics_current():
    evaluation = evaluate_hero("Karnok", [], HeroStats(hero="Karnok"), [result("mobalytics_meta_builds", ["Pufferfish"])])

    decision = decision_for(evaluation, "Pufferfish")
    assert decision.action == "add_candidate"
    assert decision.reason == "mobalytics_current"


def test_add_candidate_from_bazaar_builds_net_two_of_three():
    stats = history_with_windows("Karnok", "bazaar_builds_net", "Pufferfish", [True, False])

    evaluation = evaluate_hero("Karnok", [], stats, [result("bazaar_builds_net", ["Pufferfish"])])

    decision = decision_for(evaluation, "Pufferfish")
    assert decision.action == "add_candidate"
    assert decision.reason == "bazaar_builds_net_2_of_3"


def test_add_candidate_from_mixed_current_sources():
    evaluation = evaluate_hero(
        "Karnok",
        [],
        HeroStats(hero="Karnok"),
        [result("bazaardb", ["Pufferfish"]), result("bazaar_builds_net", ["Pufferfish"])],
    )

    decision = decision_for(evaluation, "Pufferfish")
    assert decision.action == "add_candidate"
    assert decision.reason == "mixed_current_sources"


def test_remove_candidate_when_bazaardb_absent_four_patches_and_secondaries_clear():
    stats = history_with_windows("Karnok", "bazaardb", "Old Core", [False, False, False, False])

    evaluation = evaluate_hero("Karnok", [CatalogItem("Old Core", phase="mid")], stats, [])

    decision = decision_for(evaluation, "Old Core")
    assert decision.action == "remove_candidate"
    assert decision.reason == "bazaardb_absent_4_patches_21_days_secondaries_clear"


def test_remove_blocked_when_secondary_present():
    stats = history_with_windows("Karnok", "bazaardb", "Old Core", [False, False, False, False])

    evaluation = evaluate_hero(
        "Karnok",
        [CatalogItem("Old Core", phase="mid")],
        stats,
        [result("mobalytics_meta_builds", ["Old Core"])],
    )

    decision = decision_for(evaluation, "Old Core")
    assert decision.action == "no_change"
    assert decision.reason == "secondary_present"


def test_remove_blocked_when_freeze_active():
    stats = history_with_windows("Karnok", "bazaardb", "Old Core", [False, False, False, False])
    state = CuratorState(hero_freezes={"karnok": FreezeWindow("2026-05-06T12:00:00Z")})

    evaluation = evaluate_hero(
        "Karnok",
        [CatalogItem("Old Core", phase="mid")],
        stats,
        [],
        state,
        now=datetime(2026, 5, 5, 12, tzinfo=timezone.utc),
    )

    decision = decision_for(evaluation, "Old Core")
    assert decision.action == "no_change"
    assert decision.reason == "freeze_active"


def test_unhealthy_bazaardb_window_does_not_count_toward_absence_streak():
    stats = HeroStats(hero="Karnok")
    for index, healthy in enumerate([True, True, False, True], start=1):
        append_window(
            stats,
            "bazaardb",
            WindowObservation(
                window_id=f"bazaardb:p{index}",
                observed_at=observed_at_days(index * 7),
                health_status="healthy" if healthy else "unhealthy",
                items=[ItemWindowEvidence(item="Old Core", present=False)],
            ),
        )

    evaluation = evaluate_hero("Karnok", [CatalogItem("Old Core", phase="mid")], stats, [])

    decision = decision_for(evaluation, "Old Core")
    assert decision.reason == "insufficient_history"


def test_insufficient_history_returns_insufficient_history_not_no_change():
    stats = history_with_windows("Karnok", "bazaardb", "Old Core", [False, False])

    evaluation = evaluate_hero("Karnok", [CatalogItem("Old Core", phase="mid")], stats, [])

    decision = decision_for(evaluation, "Old Core")
    assert decision.action == "no_change"
    assert decision.reason == "insufficient_history"


def test_source_quality_gate_sets_support_only_when_bazaardb_absent_and_mobalytics_present():
    evaluation = evaluate_hero(
        "Karnok",
        [],
        HeroStats(hero="Karnok"),
        [result("bazaardb", []), result("mobalytics_meta_builds", ["Pufferfish"])],
    )

    decision = decision_for(evaluation, "Pufferfish")
    assert decision.classification_ceiling == "support_only"
    assert decision.disagreement.rationale == "secondary_present_primary_absent"


@pytest.mark.parametrize("phase", ["core", "carry"])
def test_existing_core_and_carry_items_preserved_when_primary_absent_and_secondary_present(phase):
    evaluation = evaluate_hero(
        "Karnok",
        [CatalogItem("Established Item", phase=phase, archetype=f"{phase} archetype")],
        HeroStats(hero="Karnok"),
        [result("bazaardb", []), result("mobalytics_meta_builds", ["Established Item"])],
    )

    decision = decision_for(evaluation, "Established Item")
    assert decision.action == "no_change"
    assert decision.reason == "primary_absent_secondary_present_preserve_existing_classification"
    assert decision.disagreement.rationale == "secondary_present_primary_absent"


def test_source_artifact_hydration_accepts_prefetched_output_shape():
    hydrated = hydrate_source_fetch_result(
        {
            "status": "healthy",
            "patch_label": "Apr 29",
            "observation": {
                "window_id": "bazaardb:Apr 29",
                "observed_at": OBSERVED_AT,
                "items": [{"item": "Pufferfish", "present": True}],
            },
        }
    )

    assert hydrated.patch_label == "Apr 29"
    assert hydrated.observation.items[0].item == "Pufferfish"


def history_with_windows(hero: str, source: str, item: str, present_values: list[bool]) -> HeroStats:
    stats = HeroStats(hero=hero)
    for index, present in enumerate(present_values, start=1):
        append_window(
            stats,
            source,
            WindowObservation(
                window_id=f"{source}:p{index}",
                observed_at=observed_at_days(index * 7),
                items=[ItemWindowEvidence(item=item, present=present)],
            ),
        )
    return stats


def observed_at_days(days: int) -> str:
    return (datetime(2026, 4, 1, 12, tzinfo=timezone.utc) + timedelta(days=days)).isoformat().replace("+00:00", "Z")
