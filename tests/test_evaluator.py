from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from automated_builds_pipeline.evaluator import (
    CatalogItem,
    classify_source_disagreement,
    evaluate_hero,
    hydrate_source_fetch_result,
    iter_catalog_items,
    load_catalog_items,
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


def row_for(evaluation, item):
    return next(row for row in evaluation.rows if row["item"] == item)


@pytest.mark.parametrize(
    ("bazaardb", "mobalytics", "bazaar_builds_net", "ceiling", "rationale"),
    [
        (True, True, True, "carry_core_support", "primary_present"),
        (True, True, False, "carry_core_support", "primary_present"),
        (True, False, True, "carry_core_support", "primary_present"),
        (True, False, False, "carry_core_support", "primary_present"),
        (False, True, True, "support_only", "secondary_present_primary_absent"),
        (False, True, False, "support_only", "secondary_present_primary_absent"),
        (False, False, True, "support_only", "secondary_present_primary_absent"),
        (False, False, False, "not_applicable", "all_available_sources_clear"),
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

    row = row_for(evaluation, "Pufferfish")
    assert row["threshold_result"] == "add_candidate"
    assert row["threshold_reason"] == "bazaardb_present_2_of_3_patches"


def test_add_candidate_from_mobalytics_current():
    evaluation = evaluate_hero("Karnok", [], HeroStats(hero="Karnok"), [result("mobalytics_meta_builds", ["Pufferfish"])])

    row = row_for(evaluation, "Pufferfish")
    assert row["threshold_result"] == "add_candidate"
    assert row["threshold_reason"] == "mobalytics_current_build"


def test_add_candidate_from_bazaar_builds_net_two_of_three():
    stats = history_with_windows("Karnok", "bazaar_builds_net", "Pufferfish", [True, False])

    evaluation = evaluate_hero("Karnok", [], stats, [result("bazaar_builds_net", ["Pufferfish"])])

    row = row_for(evaluation, "Pufferfish")
    assert row["threshold_result"] == "add_candidate"
    assert row["threshold_reason"] == "bazaar_builds_net_2_of_3_windows"


def test_add_candidate_from_mixed_current_sources():
    evaluation = evaluate_hero(
        "Karnok",
        [],
        HeroStats(hero="Karnok"),
        [result("bazaardb", ["Pufferfish"]), result("bazaar_builds_net", ["Pufferfish"])],
    )

    row = row_for(evaluation, "Pufferfish")
    assert row["threshold_result"] == "add_candidate"
    assert row["threshold_reason"] == "mobalytics_current_build"


def test_remove_candidate_when_bazaardb_absent_four_patches_and_secondaries_clear():
    stats = history_with_windows("Karnok", "bazaardb", "Old Core", [False, False, False, False])

    evaluation = evaluate_hero("Karnok", [CatalogItem("Old Core", phase="mid")], stats, [])

    row = row_for(evaluation, "Old Core")
    assert row["threshold_result"] == "remove_candidate"
    assert row["threshold_reason"] == "bazaardb_absent_4_patches_21_days"


def test_remove_blocked_when_secondary_present():
    stats = history_with_windows("Karnok", "bazaardb", "Old Core", [False, False, False, False])

    evaluation = evaluate_hero(
        "Karnok",
        [CatalogItem("Old Core", phase="mid")],
        stats,
        [result("mobalytics_meta_builds", ["Old Core"])],
    )

    row = row_for(evaluation, "Old Core")
    assert row["threshold_result"] == "no_change"
    assert row["threshold_reason"] == "secondary_present_bazaardb_absent"


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

    row = row_for(evaluation, "Old Core")
    assert row["threshold_result"] == "blocked"
    assert row["threshold_reason"] == "none"
    assert row["removal_blocked_by"] == ["freeze_removals"]


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

    row = row_for(evaluation, "Old Core")
    assert row["threshold_result"] == "insufficient_history"
    assert row["threshold_reason"] == "not_enough_windows"


def test_insufficient_history_returns_insufficient_history_not_no_change():
    stats = history_with_windows("Karnok", "bazaardb", "Old Core", [False, False])

    evaluation = evaluate_hero("Karnok", [CatalogItem("Old Core", phase="mid")], stats, [])

    row = row_for(evaluation, "Old Core")
    assert row["threshold_result"] == "insufficient_history"
    assert row["threshold_reason"] == "not_enough_windows"


def test_source_quality_gate_sets_support_only_when_bazaardb_absent_and_mobalytics_present():
    evaluation = evaluate_hero(
        "Karnok",
        [],
        HeroStats(hero="Karnok"),
        [result("bazaardb", []), result("mobalytics_meta_builds", ["Pufferfish"])],
    )

    row = row_for(evaluation, "Pufferfish")
    assert row["classification_ceiling"] == "support_only"
    assert row["disagreement"] == "secondary_present_bazaardb_absent"


@pytest.mark.parametrize("phase", ["core", "carry"])
def test_existing_core_and_carry_items_preserved_when_primary_absent_and_secondary_present(phase):
    evaluation = evaluate_hero(
        "Karnok",
        [CatalogItem("Established Item", phase=phase, archetype=f"{phase} archetype")],
        HeroStats(hero="Karnok"),
        [result("bazaardb", []), result("mobalytics_meta_builds", ["Established Item"])],
    )

    row = row_for(evaluation, "Established Item")
    assert row["threshold_result"] == "no_change"
    assert row["threshold_reason"] == "secondary_present_bazaardb_absent"
    assert row["disagreement"] == "secondary_present_bazaardb_absent"


def test_evaluation_output_uses_spec_top_level_shape():
    evaluation = evaluate_hero(
        "Karnok",
        [],
        HeroStats(hero="Karnok"),
        [result("bazaardb", ["Pufferfish"])],
        CuratorState(expected_bazaardb_patch_label=None),
        now=datetime(2026, 5, 5, 12, tzinfo=timezone.utc),
    )

    payload = evaluation.to_dict()

    assert set(payload) == {
        "schema_version",
        "generated_at",
        "run_id",
        "hero",
        "bazaardb_patch",
        "source_health",
        "rows",
    }
    assert "decisions" not in payload
    assert payload["generated_at"] == "2026-05-05T12:00:00Z"
    assert payload["run_id"] == "20260505T120000Z"
    assert payload["bazaardb_patch"] == {
        "label": None,
        "patch_notes_url": None,
        "expected_label": None,
        "matched_expected": True,
    }


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


def test_catalog_walker_handles_real_tracker_shape(tmp_path):
    catalog = {
        "schema_version": 1,
        "hero": "Karnok",
        "game_phases": {
            "early": {
                "day_range": "Day 1-4",
                "universal_utility_items": ["Flying Squirrel", "Waterskin"],
                "economy_items": ["Hunter's Journal"],
            },
            "early_mid": {
                "day_range": "Day 4-7",
                "archetypes": [
                    {
                        "name": "Axe",
                        "phase": "early_mid",
                        "carry_items": ["Battle Axe", "Sawpike"],
                        "support_items": ["Bagpipes"],
                    }
                ],
            },
            "late": {
                "archetypes": [
                    {
                        "name": "Slow - Ammo",
                        "phase": "late",
                        "condition_items": ["Chains", "Tent"],
                        "core_items": ["Chains", "Tent"],
                        "carry_items": ["Shotgun"],
                        "support_items": [],
                    }
                ],
            },
        },
    }

    items = list(iter_catalog_items(catalog))

    by_phase = {}
    for item in items:
        by_phase.setdefault(item.phase, []).append((item.archetype, item.item))

    assert (None, "Flying Squirrel") in by_phase["early"]
    assert (None, "Hunter's Journal") in by_phase["early"]
    assert ("Axe", "Battle Axe") in by_phase["early_mid"]
    assert ("Slow - Ammo", "Chains") in by_phase["late"]
    assert ("Slow - Ammo", "Shotgun") in by_phase["late"]


def test_catalog_walker_handles_legacy_items_list_shape():
    catalog = {"items": [{"item": "Pufferfish", "phase": "early", "archetype": "Axe"}]}

    items = list(iter_catalog_items(catalog))

    assert len(items) == 1
    assert items[0].item == "Pufferfish"
    assert items[0].phase == "early"
    assert items[0].archetype == "Axe"


def test_load_catalog_items_walks_tracker_shape(tmp_path):
    path = tmp_path / "karnok_builds.json"
    path.write_text(
        json.dumps(
            {
                "game_phases": {
                    "early_mid": {
                        "archetypes": [
                            {"name": "Axe", "carry_items": ["Battle Axe"], "support_items": ["Bagpipes"]}
                        ]
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    items = load_catalog_items(path)

    assert {item.item for item in items} == {"Battle Axe", "Bagpipes"}
    assert all(item.phase == "early_mid" for item in items)
    assert all(item.archetype == "Axe" for item in items)
