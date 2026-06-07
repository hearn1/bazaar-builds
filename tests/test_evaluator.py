from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from automated_builds_pipeline.evaluator import (
    CatalogItem,
    classify_source_disagreement,
    evaluate_hero,
    hydrate_source_fetch_result,
    iter_catalog_items,
    load_catalog_items,
)
from automated_builds_pipeline.game_changes import GameChangeSignal, GameChangeSignals
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


def result_with_evidence(
    source: str,
    items: list[ItemWindowEvidence],
    *,
    status: str = "healthy",
    observed_at: str = OBSERVED_AT,
) -> SourceFetchResult:
    return SourceFetchResult(
        observation=WindowObservation(
            window_id=f"{source}:current",
            observed_at=observed_at,
            artifact_ref=f"artifacts/{source}.json",
            items=items,
        ),
        status=status,
        details=[],
    )


def decision_for(evaluation, item):
    return next(decision for decision in evaluation.decisions if decision.item == item)


def row_for(evaluation, item):
    return next(row for row in evaluation.rows if row["item"] == item)


def signals(*records: GameChangeSignal) -> GameChangeSignals:
    return GameChangeSignals(tuple(records))


def signal(
    signal_type: str,
    item: str,
    *,
    signal_id: str = "sig-1",
    replacement_item: str | None = None,
) -> GameChangeSignal:
    return GameChangeSignal(
        id=signal_id,
        type=signal_type,
        item=item,
        hero="Karnok",
        effective_date=date(2026, 5, 1),
        source_url=f"https://example.test/{signal_id}",
        note=f"{signal_type} note",
        replacement_item=replacement_item,
        patch="2026-W18",
        metadata={"severity": "explicit"},
    )


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
    stats = history_with_archetype("Karnok", "bazaardb", "Pufferfish", [True, False], archetype="Anaconda")

    evaluation = evaluate_hero(
        "Karnok",
        [CatalogItem("Anaconda", phase="late", archetype="Anaconda")],
        stats,
        [
            result_with_evidence(
                "bazaardb",
                [ItemWindowEvidence(item="Pufferfish", archetype="Anaconda", archetypes_seen=["Anaconda"])],
            )
        ],
    )

    row = row_for(evaluation, "Pufferfish")
    assert row["threshold_result"] == "add_candidate"
    assert row["threshold_reason"] == "bazaardb_present_2_of_3_patches"


def test_add_candidate_from_strong_current_bazaardb_patch_signal():
    evaluation = evaluate_hero(
        "Karnok",
        [CatalogItem("Anaconda", phase="late", archetype="Anaconda")],
        HeroStats(hero="Karnok"),
        [
            result_with_evidence(
                "bazaardb",
                [
                    ItemWindowEvidence(
                        item="Pufferfish",
                        archetype="Anaconda",
                        archetypes_seen=["Anaconda"],
                        appearances=18,
                        frequency=0.36,
                    )
                ],
            )
        ],
    )

    row = row_for(evaluation, "Pufferfish")
    assert row["threshold_result"] == "add_candidate"
    assert row["threshold_reason"] == "bazaardb_current_patch_strong"
    assert row["within_patch_strength"] == "statistical_strong"
    assert row["current_patch_evidence"]["bazaardb"]["appearances"] == 18


def test_add_candidate_from_mobalytics_current():
    evaluation = evaluate_hero("Karnok", [], HeroStats(hero="Karnok"), [result("mobalytics_meta_builds", ["Pufferfish"])])

    row = row_for(evaluation, "Pufferfish")
    assert row["threshold_result"] == "add_candidate"
    assert row["threshold_reason"] == "mobalytics_current_build"


def test_empty_seed_catalog_anchors_bazaardb_to_current_secondary_evidence():
    evaluation = evaluate_hero(
        "Jules",
        [],
        HeroStats(hero="Jules"),
        [
            result_with_evidence(
                "mobalytics_meta_builds",
                [
                    ItemWindowEvidence(
                        item="Bread Knife",
                        archetype="Farmer's Market",
                        metadata={"hero": "Jules", "editorial_description": "Bread Knife is in the Jules board."},
                    )
                ],
            ),
            result_with_evidence(
                "bazaardb",
                [
                    ItemWindowEvidence(
                        item="Bread Knife",
                        archetype="Farmer's Market / Bread Knife / Pasta",
                        archetypes_seen=["Farmer's Market / Bread Knife / Pasta"],
                        appearances=85,
                        sample_count=85,
                        frequency=1.0,
                        metadata={"section": "CORE ITEMS"},
                    ),
                    ItemWindowEvidence(
                        item="Foreign Item",
                        archetype="Unrelated Build",
                        archetypes_seen=["Unrelated Build"],
                        appearances=100,
                        sample_count=100,
                        frequency=1.0,
                        metadata={"section": "CORE ITEMS"},
                    ),
                ],
            ),
        ],
    )

    items = {row["item"] for row in evaluation.rows}
    assert "Bread Knife" in items
    assert "Foreign Item" not in items
    row = row_for(evaluation, "Bread Knife")
    assert row["threshold_result"] == "add_candidate"
    assert row["phase"] == "late"
    assert row["classification_ceiling"] == "carry_core_support"
    assert row["within_patch_strength"] == "statistical_core"


def test_empty_seed_catalog_does_not_admit_entire_global_bazaardb_archetype_from_one_overlap():
    foreign_archetype = "Waterskin / Messenger Sparrow / Flying Squirrel"
    evaluation = evaluate_hero(
        "Stelle",
        [],
        HeroStats(hero="Stelle"),
        [
            result_with_evidence(
                "bazaar_builds_net",
                [
                    ItemWindowEvidence(
                        item="Messenger Sparrow",
                        archetype="Space Laser Build",
                        archetypes_seen=["Space Laser Build"],
                    )
                ],
            ),
            result_with_evidence(
                "bazaardb",
                [
                    ItemWindowEvidence(
                        item="Waterskin",
                        archetype=foreign_archetype,
                        archetypes_seen=[foreign_archetype],
                        appearances=122,
                        sample_count=122,
                        frequency=1.0,
                        metadata={"section": "CORE ITEMS"},
                    ),
                    ItemWindowEvidence(
                        item="Messenger Sparrow",
                        archetype=foreign_archetype,
                        archetypes_seen=[foreign_archetype],
                        appearances=122,
                        sample_count=122,
                        frequency=1.0,
                        metadata={"section": "CORE ITEMS"},
                    ),
                    ItemWindowEvidence(
                        item="Flying Squirrel",
                        archetype=foreign_archetype,
                        archetypes_seen=[foreign_archetype],
                        appearances=122,
                        sample_count=122,
                        frequency=1.0,
                        metadata={"section": "CORE ITEMS"},
                    ),
                    ItemWindowEvidence(
                        item="Hunter's Journal",
                        archetype=foreign_archetype,
                        archetypes_seen=[foreign_archetype],
                        appearances=29,
                        frequency=0.24,
                        metadata={"section": "SUPPORTING ITEMS"},
                    ),
                    ItemWindowEvidence(
                        item="Runic Claymore",
                        archetype=foreign_archetype,
                        archetypes_seen=[foreign_archetype],
                        appearances=34,
                        frequency=0.28,
                        metadata={"section": "SUPPORTING ITEMS"},
                    ),
                ],
            ),
        ],
    )

    items = {row["item"] for row in evaluation.rows}
    assert "Messenger Sparrow" in items
    assert {"Waterskin", "Flying Squirrel", "Hunter's Journal", "Runic Claymore"}.isdisjoint(items)


def test_add_candidate_from_bazaar_builds_net_two_of_three():
    stats = history_with_windows("Karnok", "bazaar_builds_net", "Pufferfish", [True, False])

    evaluation = evaluate_hero("Karnok", [], stats, [result("bazaar_builds_net", ["Pufferfish"])])

    row = row_for(evaluation, "Pufferfish")
    assert row["threshold_result"] == "add_candidate"
    assert row["threshold_reason"] == "bazaar_builds_net_2_of_3_windows"


def test_add_candidate_from_mixed_current_sources():
    evaluation = evaluate_hero(
        "Karnok",
        [CatalogItem("Anaconda", phase="late", archetype="Anaconda")],
        HeroStats(hero="Karnok"),
        [
            result_with_evidence(
                "bazaardb",
                [ItemWindowEvidence(item="Pufferfish", archetype="Anaconda", archetypes_seen=["Anaconda"])],
            ),
            result("bazaar_builds_net", ["Pufferfish"]),
        ],
    )

    row = row_for(evaluation, "Pufferfish")
    assert row["threshold_result"] == "add_candidate"
    assert row["threshold_reason"] == "mobalytics_current_build"


def test_remove_candidate_when_current_healthy_bazaardb_absence_spans_30_days():
    stats = history_with_absent_windows("Karnok", "bazaardb", "Old Core", [0])

    evaluation = evaluate_hero(
        "Karnok",
        [CatalogItem("Old Core", phase="mid")],
        stats,
        [result("bazaardb", [], observed_at=observed_at_days(30))],
    )

    row = row_for(evaluation, "Old Core")
    assert row["threshold_result"] == "remove_candidate"
    assert row["threshold_reason"] == "bazaardb_absent_30_days"
    assert "4_patches" not in row["threshold_reason"]
    assert "21_days" not in row["threshold_reason"]


def test_support_bucket_retirement_is_item_removal_even_with_core_carry_names():
    stats = history_with_absent_windows("Karnok", "bazaardb", "Support Piece", [0])

    evaluation = evaluate_hero(
        "Karnok",
        [
            CatalogItem(
                "Support Piece",
                phase="core phase",
                archetype="carry archetype",
                bucket="support_items",
            )
        ],
        stats,
        [result("bazaardb", [], observed_at=observed_at_days(30))],
    )

    row = row_for(evaluation, "Support Piece")
    assert row["threshold_result"] == "remove_candidate"
    assert row["catalog_bucket"] == "support_items"
    assert row["retirement_type"] == "support_item"
    assert row["retirement_basis"] == "bazaardb_absent_30_days"
    assert row["actionability"] == "item_removal_candidate"
    assert row["affected_items"] == ["Support Piece"]
    assert row["signal_evidence"] == []


@pytest.mark.parametrize(
    ("bucket", "retirement_type", "review_scope"),
    [
        ("carry_items", "whole_build_review", "whole_build"),
        ("core_items", "core_item_review", "core_item"),
        ("condition_items", "condition_item_review", "condition_item"),
        ("universal_utility_items", "support_like_phase_item", "phase_item"),
        ("economy_items", "support_like_phase_item", "phase_item"),
    ],
)
def test_non_support_buckets_create_review_retirement_candidates(bucket, retirement_type, review_scope):
    stats = history_with_absent_windows("Karnok", "bazaardb", "Retired Item", [0])

    evaluation = evaluate_hero(
        "Karnok",
        [CatalogItem("Retired Item", phase="mid", archetype="Axe", bucket=bucket)],
        stats,
        [result("bazaardb", [], observed_at=observed_at_days(30))],
    )

    row = row_for(evaluation, "Retired Item")
    assert row["threshold_result"] == "retirement_review_candidate"
    assert row["catalog_bucket"] == bucket
    assert row["retirement_type"] == retirement_type
    assert row["retirement_basis"] == "bazaardb_absent_30_days"
    assert row["actionability"] == "review_required"
    assert row["affected_items"] == ["Retired Item"]
    assert row["affected_item_details"] == [
        {
            "item": "Retired Item",
            "catalog_bucket": bucket,
            "phase": "mid",
            "archetype": "Axe",
        }
    ]
    assert row["review_scope"] == review_scope
    assert row["review_priority"] == "normal"


def test_carry_retirement_review_includes_commitment_context_without_deletion_action():
    stats = history_with_absent_windows("Karnok", "bazaardb", "Battle Axe", [0])

    evaluation = evaluate_hero(
        "Karnok",
        [
            CatalogItem("Battle Axe", phase="mid", archetype="Axe", bucket="carry_items"),
            CatalogItem("Sawpike", phase="mid", archetype="Axe", bucket="carry_items"),
            CatalogItem("Hidden Lake", phase="mid", archetype="Axe", bucket="core_items"),
            CatalogItem("Chains", phase="mid", archetype="Axe", bucket="condition_items"),
            CatalogItem("Bagpipes", phase="mid", archetype="Axe", bucket="support_items"),
        ],
        stats,
        [result("bazaardb", [], observed_at=observed_at_days(30))],
    )

    row = row_for(evaluation, "Battle Axe")
    assert row["threshold_result"] == "retirement_review_candidate"
    assert row["retirement_type"] == "whole_build_review"
    assert row["affected_items"] == ["Battle Axe"]
    assert row["affected_build_items"] == {
        "carry_items": ["Battle Axe", "Sawpike"],
        "core_items": ["Hidden Lake"],
        "condition_items": ["Chains"],
    }
    assert row["actionability"] == "review_required"


def test_support_bucket_with_secondary_presence_does_not_use_core_carry_name_heuristic():
    evaluation = evaluate_hero(
        "Karnok",
        [
            CatalogItem(
                "Established Support",
                phase="core phase",
                archetype="carry archetype",
                bucket="support_items",
            )
        ],
        HeroStats(hero="Karnok"),
        [
            result("bazaardb", []),
            result_with_evidence(
                "mobalytics_meta_builds",
                [ItemWindowEvidence(item="Established Support", archetype="carry archetype")],
            ),
        ],
    )

    decision = decision_for(evaluation, "Established Support")
    row = row_for(evaluation, "Established Support")
    assert decision.reason == "secondary_present"
    assert row["threshold_result"] == "no_change"
    assert row["catalog_bucket"] == "support_items"
    assert row["retirement_type"] is None


@pytest.mark.parametrize("bucket", ["carry_items", "core_items", "condition_items"])
def test_commitment_buckets_preserve_existing_classification_when_secondary_present(bucket):
    evaluation = evaluate_hero(
        "Karnok",
        [CatalogItem("Established Item", phase="mid", archetype="Axe", bucket=bucket)],
        HeroStats(hero="Karnok"),
        [result("bazaardb", []), result("mobalytics_meta_builds", ["Established Item"])],
    )

    decision = decision_for(evaluation, "Established Item")
    row = row_for(evaluation, "Established Item")
    assert decision.reason == "primary_absent_secondary_present_preserve_existing_classification"
    assert row["threshold_reason"] == "secondary_present_bazaardb_absent"
    assert row["catalog_bucket"] == bucket


def test_remove_candidate_requires_30_day_absence_span():
    stats = history_with_absent_windows("Karnok", "bazaardb", "Old Core", [0])

    evaluation = evaluate_hero(
        "Karnok",
        [CatalogItem("Old Core", phase="mid")],
        stats,
        [result("bazaardb", [], observed_at=observed_at_days(29))],
    )

    row = row_for(evaluation, "Old Core")
    assert row["threshold_result"] == "no_change"
    assert row["threshold_reason"] == "none"


def test_remove_candidate_requires_two_healthy_absence_windows():
    evaluation = evaluate_hero(
        "Karnok",
        [CatalogItem("Old Core", phase="mid")],
        HeroStats(hero="Karnok"),
        [result("bazaardb", [], observed_at=observed_at_days(30))],
    )

    row = row_for(evaluation, "Old Core")
    assert row["threshold_result"] == "insufficient_history"
    assert row["threshold_reason"] == "not_enough_windows"


@pytest.mark.parametrize("current_results", [[], [result("bazaardb", [], status="unhealthy")]])
def test_remove_candidate_requires_current_healthy_bazaardb_absence(current_results):
    stats = history_with_absent_windows("Karnok", "bazaardb", "Old Core", [0, 30])

    evaluation = evaluate_hero("Karnok", [CatalogItem("Old Core", phase="mid")], stats, current_results)

    row = row_for(evaluation, "Old Core")
    assert row["threshold_result"] == "no_change"
    assert row["threshold_reason"] == "none"


def test_remove_blocked_when_current_healthy_secondary_present():
    stats = history_with_absent_windows("Karnok", "bazaardb", "Old Core", [0])

    evaluation = evaluate_hero(
        "Karnok",
        [CatalogItem("Old Core", phase="mid")],
        stats,
        [
            result("bazaardb", [], observed_at=observed_at_days(30)),
            result("mobalytics_meta_builds", ["Old Core"], observed_at=observed_at_days(30)),
        ],
    )

    row = row_for(evaluation, "Old Core")
    assert row["threshold_result"] == "no_change"
    assert row["threshold_reason"] == "secondary_present_bazaardb_absent"


def test_older_secondary_presence_is_context_not_permanent_retirement_block():
    stats = history_with_absent_windows("Karnok", "bazaardb", "Old Core", [0])
    append_window(
        stats,
        "mobalytics_meta_builds",
        WindowObservation(
            window_id="mobalytics_meta_builds:old",
            observed_at=observed_at_days(1),
            items=[ItemWindowEvidence(item="Old Core", present=True)],
        ),
    )

    evaluation = evaluate_hero(
        "Karnok",
        [CatalogItem("Old Core", phase="mid")],
        stats,
        [result("bazaardb", [], observed_at=observed_at_days(30))],
    )

    row = row_for(evaluation, "Old Core")
    assert row["threshold_result"] == "remove_candidate"
    assert row["threshold_reason"] == "bazaardb_absent_30_days"
    assert row["windows_seen"] == 1
    assert row["first_seen_window"] == "mobalytics_meta_builds:old"


def test_remove_blocked_when_freeze_active_preserves_candidate_evidence():
    stats = history_with_absent_windows("Karnok", "bazaardb", "Old Core", [0])
    state = CuratorState(hero_freezes={"karnok": FreezeWindow("2026-05-06T12:00:00Z")})

    evaluation = evaluate_hero(
        "Karnok",
        [CatalogItem("Old Core", phase="mid")],
        stats,
        [result("bazaardb", [], observed_at=observed_at_days(30))],
        state,
        now=datetime(2026, 5, 5, 12, tzinfo=timezone.utc),
    )

    row = row_for(evaluation, "Old Core")
    assert row["threshold_result"] == "blocked"
    assert row["threshold_reason"] == "none"
    assert row["removal_blocked_by"] == ["freeze_removals"]
    assert row["source_presence"]["bazaardb"] == "absent"
    assert row["current_patch_evidence"]["bazaardb"]["presence"] == "absent"
    assert row["current_patch_evidence"]["bazaardb"]["observed_at"] == observed_at_days(30)


def test_removed_support_signal_creates_candidate_without_stale_absence():
    evaluation = evaluate_hero(
        "Karnok",
        [CatalogItem("Old Support", phase="mid", archetype="Axe", bucket="support_items")],
        HeroStats(hero="Karnok"),
        [result("bazaardb", [])],
        game_change_signals=signals(signal("removed_card", "Old Support", signal_id="removed-old-support")),
    )

    row = row_for(evaluation, "Old Support")
    assert row["threshold_result"] == "remove_candidate"
    assert row["threshold_reason"] == "game_change_removed_card"
    assert row["retirement_basis"] == "game_change_removed_card"
    assert row["catalog_bucket"] == "support_items"
    assert row["actionability"] == "item_removal_candidate"
    assert row["signal_evidence"][0]["id"] == "removed-old-support"
    assert row["signal_evidence"][0]["source_url"] == "https://example.test/removed-old-support"


def test_renamed_support_signal_carries_replacement_context():
    evaluation = evaluate_hero(
        "Karnok",
        [CatalogItem("Old Name", phase="mid", archetype="Axe", bucket="support_items")],
        HeroStats(hero="Karnok"),
        [],
        game_change_signals=signals(
            signal("renamed_card", "Old Name", signal_id="rename-old-name", replacement_item="New Name")
        ),
    )

    row = row_for(evaluation, "Old Name")
    assert row["threshold_result"] == "remove_candidate"
    assert row["threshold_reason"] == "game_change_renamed_card"
    assert row["affected_item_details"][0]["replacement_item"] == "New Name"
    assert row["signal_evidence"][0]["replacement_item"] == "New Name"


def test_carry_invalidation_signal_creates_whole_build_review_candidate():
    evaluation = evaluate_hero(
        "Karnok",
        [
            CatalogItem("Battle Axe", phase="mid", archetype="Axe", bucket="carry_items"),
            CatalogItem("Hidden Lake", phase="mid", archetype="Axe", bucket="core_items"),
            CatalogItem("Small Support", phase="mid", archetype="Axe", bucket="support_items"),
        ],
        HeroStats(hero="Karnok"),
        [],
        game_change_signals=signals(signal("explicit_invalidation", "Battle Axe", signal_id="axe-invalid")),
    )

    row = row_for(evaluation, "Battle Axe")
    assert row["threshold_result"] == "retirement_review_candidate"
    assert row["threshold_reason"] == "game_change_explicit_invalidation"
    assert row["retirement_type"] == "whole_build_review"
    assert row["actionability"] == "review_required"
    assert row["review_priority"] == "high"
    assert row["affected_build_items"] == {
        "carry_items": ["Battle Axe"],
        "core_items": ["Hidden Lake"],
    }


@pytest.mark.parametrize(
    ("bucket", "retirement_type", "review_scope"),
    [
        ("core_items", "core_item_review", "core_item"),
        ("condition_items", "condition_item_review", "condition_item"),
    ],
)
def test_core_and_condition_invalidation_signals_are_high_priority_reviews(bucket, retirement_type, review_scope):
    evaluation = evaluate_hero(
        "Karnok",
        [CatalogItem("Questionable Item", phase="mid", archetype="Axe", bucket=bucket)],
        HeroStats(hero="Karnok"),
        [],
        game_change_signals=signals(signal("explicit_invalidation", "Questionable Item")),
    )

    row = row_for(evaluation, "Questionable Item")
    assert row["threshold_result"] == "retirement_review_candidate"
    assert row["retirement_type"] == retirement_type
    assert row["review_scope"] == review_scope
    assert row["review_priority"] == "high"
    assert row["actionability"] == "review_required"


def test_major_nerf_signal_creates_watchlist_review_not_removal():
    evaluation = evaluate_hero(
        "Karnok",
        [CatalogItem("Nerfed Support", phase="mid", archetype="Axe", bucket="support_items")],
        HeroStats(hero="Karnok"),
        [],
        game_change_signals=signals(signal("major_nerf", "Nerfed Support", signal_id="nerf-support")),
    )

    row = row_for(evaluation, "Nerfed Support")
    assert row["threshold_result"] == "retirement_review_candidate"
    assert row["threshold_reason"] == "game_change_major_nerf"
    assert row["actionability"] == "watchlist_review"
    assert row["review_priority"] == "watchlist"


def test_signal_support_candidate_is_visible_but_freeze_blocked():
    state = CuratorState(hero_freezes={"karnok": FreezeWindow("2026-05-06T12:00:00Z")})

    evaluation = evaluate_hero(
        "Karnok",
        [CatalogItem("Old Support", phase="mid", archetype="Axe", bucket="support_items")],
        HeroStats(hero="Karnok"),
        [result("bazaardb", [])],
        state,
        now=datetime(2026, 5, 5, 12, tzinfo=timezone.utc),
        game_change_signals=signals(signal("removed_card", "Old Support")),
    )

    row = row_for(evaluation, "Old Support")
    assert row["threshold_result"] == "remove_candidate"
    assert row["actionability"] == "freeze_blocked"
    assert row["removal_blocked_by"] == ["freeze_removals"]
    assert row["signal_evidence"][0]["type"] == "removed_card"


def test_unhealthy_bazaardb_window_does_not_count_toward_absence_span():
    stats = HeroStats(hero="Karnok")
    append_window(
        stats,
        "bazaardb",
        WindowObservation(
            window_id="bazaardb:unhealthy",
            observed_at=observed_at_days(0),
            health_status="unhealthy",
            items=[ItemWindowEvidence(item="Old Core", present=False)],
        )
    )

    evaluation = evaluate_hero(
        "Karnok",
        [CatalogItem("Old Core", phase="mid")],
        stats,
        [result("bazaardb", [], observed_at=observed_at_days(30))],
    )

    row = row_for(evaluation, "Old Core")
    assert row["threshold_result"] == "insufficient_history"
    assert row["threshold_reason"] == "not_enough_windows"


@pytest.mark.parametrize("bucket", ["carry_items", "core_items", "condition_items"])
def test_removed_card_signal_on_commitment_bucket_is_review_only(bucket):
    evaluation = evaluate_hero(
        "Karnok",
        [CatalogItem("Commitment Item", phase="mid", archetype="Axe", bucket=bucket)],
        HeroStats(hero="Karnok"),
        [],
        game_change_signals=signals(signal("removed_card", "Commitment Item", signal_id="removed-commitment")),
    )

    row = row_for(evaluation, "Commitment Item")
    assert row["threshold_result"] == "retirement_review_candidate"
    assert row["threshold_reason"] == "game_change_removed_card"
    assert row["actionability"] != "item_removal_candidate"
    assert row["signal_evidence"][0]["id"] == "removed-commitment"


def test_renamed_card_signal_does_not_create_replacement_as_catalog_entry():
    evaluation = evaluate_hero(
        "Karnok",
        [CatalogItem("Old Name", phase="mid", archetype="Axe", bucket="support_items")],
        HeroStats(hero="Karnok"),
        [],
        game_change_signals=signals(
            signal("renamed_card", "Old Name", signal_id="rename-1", replacement_item="New Name")
        ),
    )

    assert "New Name" not in {row["item"] for row in evaluation.rows}


def test_renamed_card_signal_does_not_affect_unrelated_catalog_items():
    evaluation = evaluate_hero(
        "Karnok",
        [
            CatalogItem("Old Name", phase="mid", archetype="Axe", bucket="support_items"),
            CatalogItem("Unrelated Item", phase="mid", archetype="Other Axe", bucket="support_items"),
        ],
        HeroStats(hero="Karnok"),
        [],
        game_change_signals=signals(
            signal("renamed_card", "Old Name", signal_id="rename-1", replacement_item="New Name")
        ),
    )

    unrelated_row = row_for(evaluation, "Unrelated Item")
    assert unrelated_row["signal_evidence"] == []
    assert unrelated_row["threshold_result"] == "no_change"


def test_watchlist_signal_is_review_only():
    evaluation = evaluate_hero(
        "Karnok",
        [CatalogItem("Watched Item", phase="mid", archetype="Axe", bucket="carry_items")],
        HeroStats(hero="Karnok"),
        [],
        game_change_signals=signals(signal("watchlist", "Watched Item", signal_id="watch-1")),
    )

    row = row_for(evaluation, "Watched Item")
    assert row["threshold_result"] == "retirement_review_candidate"
    assert row["threshold_reason"] == "game_change_watchlist"
    assert row["actionability"] == "watchlist_review"
    assert row["review_priority"] == "watchlist"
    assert row["signal_evidence"][0]["id"] == "watch-1"


def test_watchlist_signal_with_low_confidence_remains_review_only():
    watchlist_signal = GameChangeSignal(
        id="watch-uncertain",
        type="watchlist",
        item="Uncertain Item",
        hero="Karnok",
        effective_date=date(2026, 5, 1),
        source_url="https://example.test/watch-uncertain",
        note="Uncertain note.",
        metadata={"confidence": "low", "uncertainty_note": "May have been nerfed."},
    )

    evaluation = evaluate_hero(
        "Karnok",
        [CatalogItem("Uncertain Item", phase="mid", archetype="Axe", bucket="carry_items")],
        HeroStats(hero="Karnok"),
        [],
        game_change_signals=signals(watchlist_signal),
    )

    row = row_for(evaluation, "Uncertain Item")
    assert row["threshold_result"] == "retirement_review_candidate"
    assert row["actionability"] == "watchlist_review"
    assert row["signal_evidence"][0]["metadata"]["confidence"] == "low"
    assert row["signal_evidence"][0]["metadata"]["uncertainty_note"] == "May have been nerfed."


def test_signal_row_includes_secondary_source_disagreement_context():
    evaluation = evaluate_hero(
        "Karnok",
        [CatalogItem("Contested Item", phase="mid", archetype="Axe", bucket="support_items")],
        HeroStats(hero="Karnok"),
        [
            result("bazaardb", []),
            result("mobalytics_meta_builds", ["Contested Item"]),
        ],
        game_change_signals=signals(signal("removed_card", "Contested Item", signal_id="removed-contested")),
    )

    row = row_for(evaluation, "Contested Item")
    assert row["signal_evidence"][0]["id"] == "removed-contested"
    assert row["disagreement"] == "secondary_present_bazaardb_absent"
    assert row["source_presence"]["bazaardb"] == "absent"
    assert row["source_presence"]["mobalytics_meta_builds"] == "present"


def test_renamed_card_signal_suppressed_when_bazaardb_confirms_name_reused_after_effective_date():
    # Regression: Patch 15.0 Burning Rage — renamed_card signal must not fire against a catalog
    # entry when bazaardb (patch-scoped, healthy) confirms the name is active in the current patch.
    burning_rage_renamed = GameChangeSignal(
        id="burning-rage-renamed-p15",
        type="renamed_card",
        item="Burning Rage",
        hero="Karnok",
        effective_date=date(2026, 6, 3),
        replacement_item="Burning Infernal",
        source_url="https://example.test/patch-15-notes",
        note="Burning Rage renamed to Burning Infernal; name reused by new Karnok skill in same patch.",
        patch="15.0",
    )

    evaluation = evaluate_hero(
        "Karnok",
        [CatalogItem("Burning Rage", phase="mid", archetype="Fire Build", bucket="support_items")],
        HeroStats(hero="Karnok"),
        [result("bazaardb", ["Burning Rage"])],
        game_change_signals=signals(burning_rage_renamed),
        now=datetime(2026, 6, 6, 12, tzinfo=timezone.utc),
    )

    row = row_for(evaluation, "Burning Rage")
    assert row["threshold_result"] == "no_change"
    assert row["signal_evidence"] == []


def test_removed_card_signal_suppressed_when_bazaardb_confirms_name_reused_after_effective_date():
    removed_reused = GameChangeSignal(
        id="temporal-strike-removed",
        type="removed_card",
        item="Temporal Strike",
        hero=None,
        effective_date=date(2026, 6, 1),
        source_url="https://example.test/patch-6-notes",
        note="Temporal Strike retired in Patch 6.0; name later reused for a redesigned card.",
    )

    evaluation = evaluate_hero(
        "Karnok",
        [CatalogItem("Temporal Strike", phase="mid", archetype="Strike Build", bucket="support_items")],
        HeroStats(hero="Karnok"),
        [result("bazaardb", ["Temporal Strike"])],
        game_change_signals=signals(removed_reused),
        now=datetime(2026, 6, 6, 12, tzinfo=timezone.utc),
    )

    row = row_for(evaluation, "Temporal Strike")
    assert row["threshold_result"] == "no_change"
    assert row["signal_evidence"] == []


def test_renamed_card_signal_fires_when_bazaardb_shows_item_absent_after_effective_date():
    """When bazaardb confirms the item is absent (true retirement), the signal must fire."""
    burning_rage_renamed = GameChangeSignal(
        id="burning-rage-renamed-p15",
        type="renamed_card",
        item="Burning Rage",
        hero="Karnok",
        effective_date=date(2026, 6, 3),
        replacement_item="Burning Infernal",
        source_url="https://example.test/patch-15-notes",
        note="Burning Rage renamed to Burning Infernal.",
        patch="15.0",
    )

    evaluation = evaluate_hero(
        "Karnok",
        [CatalogItem("Burning Rage", phase="mid", archetype="Fire Build", bucket="support_items")],
        HeroStats(hero="Karnok"),
        [result("bazaardb", [])],
        game_change_signals=signals(burning_rage_renamed),
        now=datetime(2026, 6, 6, 12, tzinfo=timezone.utc),
    )

    row = row_for(evaluation, "Burning Rage")
    assert row["threshold_reason"] == "game_change_renamed_card"
    assert row["threshold_result"] == "remove_candidate"


def test_renamed_card_signal_fires_when_observation_date_precedes_effective_date():
    """A signal whose effective_date is in the future must still fire: the item is legitimately
    present because the change has not yet taken effect."""
    future_signal = GameChangeSignal(
        id="future-rename",
        type="renamed_card",
        item="Active Item",
        hero="Karnok",
        effective_date=date(2026, 7, 1),
        replacement_item="New Name",
        source_url="https://example.test/future",
        note="Will be renamed next patch.",
    )

    evaluation = evaluate_hero(
        "Karnok",
        [CatalogItem("Active Item", phase="mid", archetype="Axe", bucket="support_items")],
        HeroStats(hero="Karnok"),
        [result("bazaardb", ["Active Item"])],
        game_change_signals=signals(future_signal),
        now=datetime(2026, 6, 6, 12, tzinfo=timezone.utc),
    )

    row = row_for(evaluation, "Active Item")
    assert row["threshold_reason"] == "game_change_renamed_card"


@pytest.mark.parametrize("sig_type", ["major_nerf", "watchlist"])
def test_non_retirement_signals_fire_even_when_bazaardb_shows_item_present(sig_type):
    """major_nerf and watchlist signals flag currently-active items; bazaardb presence must not
    suppress them — only renamed_card and removed_card are name-reuse candidates."""
    evaluation = evaluate_hero(
        "Karnok",
        [CatalogItem("Buffed Support", phase="mid", archetype="Axe", bucket="support_items")],
        HeroStats(hero="Karnok"),
        [result("bazaardb", ["Buffed Support"])],
        game_change_signals=signals(signal(sig_type, "Buffed Support")),
        now=datetime(2026, 6, 6, 12, tzinfo=timezone.utc),
    )

    row = row_for(evaluation, "Buffed Support")
    assert row["threshold_reason"] == f"game_change_{sig_type}"


def test_source_quality_gate_sets_support_only_when_bazaardb_absent_and_mobalytics_present():
    evaluation = evaluate_hero(
        "Karnok",
        [CatalogItem("Known Item", phase="late", archetype="Known Build")],
        HeroStats(hero="Karnok"),
        [result("bazaardb", []), result("mobalytics_meta_builds", ["Pufferfish"])],
    )

    row = row_for(evaluation, "Pufferfish")
    assert row["classification_ceiling"] == "support_only"
    assert row["disagreement"] == "secondary_present_bazaardb_absent"


def test_mak_does_not_get_karnok_only_bazaardb_observed_items():
    karnok_archetype = "Karst / Waterskin / Stretch Pants / Wild Boar"
    stats = history_with_archetype(
        "Mak",
        "bazaardb",
        "Karst",
        [True],
        archetype=karnok_archetype,
    )
    evaluation = evaluate_hero(
        "Mak",
        [CatalogItem("Athanor", phase="late", archetype="Athanor")],
        stats,
        [
            result_with_evidence(
                "bazaardb",
                [
                    ItemWindowEvidence(
                        item="Karst",
                        archetype=karnok_archetype,
                        archetypes_seen=[karnok_archetype],
                    ),
                    ItemWindowEvidence(
                        item="Waterskin",
                        archetype=karnok_archetype,
                        archetypes_seen=[karnok_archetype],
                    ),
                    ItemWindowEvidence(
                        item="Stretch Pants",
                        archetype=karnok_archetype,
                        archetypes_seen=[karnok_archetype],
                    ),
                    ItemWindowEvidence(
                        item="Wild Boar",
                        archetype=karnok_archetype,
                        archetypes_seen=[karnok_archetype],
                    ),
                ],
            )
        ],
    )

    assert {"Karst", "Waterskin", "Stretch Pants", "Wild Boar"}.isdisjoint({row["item"] for row in evaluation.rows})


def test_dooley_does_not_get_vanessa_only_observed_items():
    evaluation = evaluate_hero(
        "Dooley",
        [CatalogItem("Companion Core", phase="late", archetype="Companion Core")],
        HeroStats(hero="Dooley"),
        [
            result_with_evidence(
                "mobalytics_build_articles",
                [
                    ItemWindowEvidence(item="Piranha", archetype="tortuga-vanessa", metadata={"hero": "Vanessa"}),
                    ItemWindowEvidence(item="Pufferfish", archetype="tortuga-vanessa", metadata={"hero": "Vanessa"}),
                    ItemWindowEvidence(item="Jellyfish", archetype="tortuga-vanessa", metadata={"hero": "Vanessa"}),
                    ItemWindowEvidence(item="Tortuga", archetype="tortuga-vanessa", metadata={"hero": "Vanessa"}),
                ],
            )
        ],
    )

    assert {"Piranha", "Pufferfish", "Jellyfish", "Tortuga"}.isdisjoint({row["item"] for row in evaluation.rows})


def test_global_bazaardb_observations_require_catalog_context_for_add_candidates():
    matching_archetype = "Known Core / Engine"
    foreign_archetype = "Foreign Core / Other Engine"
    stats = HeroStats(hero="Karnok")
    append_window(
        stats,
        "bazaardb",
        WindowObservation(
            window_id="bazaardb:prior",
            observed_at=observed_at_days(7),
            items=[
                ItemWindowEvidence(
                    item="Safe Support",
                    archetype=matching_archetype,
                    archetypes_seen=[matching_archetype],
                ),
                ItemWindowEvidence(
                    item="Foreign Support",
                    archetype=foreign_archetype,
                    archetypes_seen=[foreign_archetype],
                ),
            ],
        ),
    )

    evaluation = evaluate_hero(
        "Karnok",
        [CatalogItem("Known Core", phase="late", archetype="Known Build")],
        stats,
        [
            result_with_evidence(
                "bazaardb",
                [
                    ItemWindowEvidence(
                        item="Safe Support",
                        archetype=matching_archetype,
                        archetypes_seen=[matching_archetype],
                    ),
                    ItemWindowEvidence(
                        item="Foreign Support",
                        archetype=foreign_archetype,
                        archetypes_seen=[foreign_archetype],
                    ),
                ],
            )
        ],
    )

    assert row_for(evaluation, "Safe Support")["threshold_result"] == "add_candidate"
    assert "Foreign Support" not in {row["item"] for row in evaluation.rows}


@pytest.mark.parametrize("phase", ["core", "carry"])
def test_legacy_phase_names_do_not_preserve_existing_classification_without_bucket(phase):
    evaluation = evaluate_hero(
        "Karnok",
        [CatalogItem("Established Item", phase=phase, archetype=f"{phase} archetype")],
        HeroStats(hero="Karnok"),
        [
            result("bazaardb", []),
            result_with_evidence(
                "mobalytics_meta_builds",
                [ItemWindowEvidence(item="Established Item", archetype=f"{phase} archetype")],
            ),
        ],
    )

    decision = decision_for(evaluation, "Established Item")
    row = row_for(evaluation, "Established Item")
    assert decision.reason == "secondary_present"
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
    return history_with_archetype(hero, source, item, present_values, archetype="Observed Archetype")


def history_with_absent_windows(hero: str, source: str, item: str, days: list[int]) -> HeroStats:
    stats = HeroStats(hero=hero)
    for day in days:
        append_window(
            stats,
            source,
            WindowObservation(
                window_id=f"{source}:day-{day}",
                observed_at=observed_at_days(day),
                items=[ItemWindowEvidence(item=item, present=False)],
            ),
        )
    return stats


def history_with_archetype(
    hero: str,
    source: str,
    item: str,
    present_values: list[bool],
    *,
    archetype: str,
) -> HeroStats:
    stats = HeroStats(hero=hero)
    for index, present in enumerate(present_values, start=1):
        append_window(
            stats,
            source,
            WindowObservation(
                window_id=f"{source}:p{index}",
                observed_at=observed_at_days(index * 7),
                items=[
                    ItemWindowEvidence(
                        item=item,
                        present=present,
                        archetype=archetype,
                        archetypes_seen=[archetype],
                    )
                ],
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
        by_phase.setdefault(item.phase, []).append((item.archetype, item.item, item.bucket))

    assert (None, "Flying Squirrel", "universal_utility_items") in by_phase["early"]
    assert (None, "Hunter's Journal", "economy_items") in by_phase["early"]
    assert ("Axe", "Battle Axe", "carry_items") in by_phase["early_mid"]
    assert ("Slow - Ammo", "Chains", "condition_items") in by_phase["late"]
    assert ("Slow - Ammo", "Shotgun", "carry_items") in by_phase["late"]


def test_catalog_walker_handles_legacy_items_list_shape():
    catalog = {"items": [{"item": "Pufferfish", "phase": "early", "archetype": "Axe", "bucket": "support_items"}]}

    items = list(iter_catalog_items(catalog))

    assert len(items) == 1
    assert items[0].item == "Pufferfish"
    assert items[0].phase == "early"
    assert items[0].archetype == "Axe"
    assert items[0].bucket == "support_items"


def test_catalog_item_from_dict_preserves_bucket():
    item = CatalogItem.from_dict(
        {"item": "Pufferfish", "phase": "early", "archetype": "Axe", "bucket": "support_items"}
    )

    assert item.bucket == "support_items"


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


# ---- inactive signal lifecycle filtering ----


def _inactive_signal(signal_type: str, item: str, status: str, *, signal_id: str = "inactive-sig") -> GameChangeSignal:
    return GameChangeSignal(
        id=signal_id,
        type=signal_type,
        item=item,
        hero="Karnok",
        effective_date=date(2026, 5, 1),
        source_url=f"https://example.test/{signal_id}",
        note=f"{signal_type} inactive",
        metadata={"status": status},
    )


def test_deprecated_removed_card_does_not_create_remove_candidate():
    evaluation = evaluate_hero(
        "Karnok",
        [CatalogItem("Old Support", phase="mid", archetype="Axe", bucket="support_items")],
        HeroStats(hero="Karnok"),
        [result("bazaardb", [])],
        game_change_signals=signals(
            _inactive_signal("removed_card", "Old Support", "deprecated", signal_id="dep-removed-support")
        ),
    )

    row = row_for(evaluation, "Old Support")
    assert row["threshold_result"] != "remove_candidate"
    assert row["signal_evidence"] == []


def test_superseded_removed_card_does_not_create_remove_candidate():
    evaluation = evaluate_hero(
        "Karnok",
        [CatalogItem("Old Support", phase="mid", archetype="Axe", bucket="support_items")],
        HeroStats(hero="Karnok"),
        [result("bazaardb", [])],
        game_change_signals=signals(
            _inactive_signal("removed_card", "Old Support", "superseded", signal_id="sup-removed-support")
        ),
    )

    row = row_for(evaluation, "Old Support")
    assert row["threshold_result"] != "remove_candidate"
    assert row["signal_evidence"] == []


def test_superseded_signal_plus_active_replacement_only_active_drives_output():
    active_sig = signal("removed_card", "Old Support", signal_id="active-removed")
    inactive_sig = _inactive_signal("removed_card", "Old Support", "superseded", signal_id="sup-removed")
    evaluation = evaluate_hero(
        "Karnok",
        [CatalogItem("Old Support", phase="mid", archetype="Axe", bucket="support_items")],
        HeroStats(hero="Karnok"),
        [result("bazaardb", [])],
        game_change_signals=signals(inactive_sig, active_sig),
    )

    signal_rows = [row for row in evaluation.rows if row["item"] == "Old Support" and row["signal_evidence"]]
    assert len(signal_rows) == 1
    assert signal_rows[0]["signal_evidence"][0]["id"] == "active-removed"


def test_deprecated_carry_signal_does_not_create_retirement_review_candidate():
    evaluation = evaluate_hero(
        "Karnok",
        [CatalogItem("Big Axe", phase="mid", archetype="Axe", bucket="carry_items")],
        HeroStats(hero="Karnok"),
        [],
        game_change_signals=signals(
            _inactive_signal("removed_card", "Big Axe", "deprecated", signal_id="dep-carry")
        ),
    )

    row = row_for(evaluation, "Big Axe")
    assert row["threshold_result"] != "retirement_review_candidate"
    assert row["signal_evidence"] == []


def test_inactive_signal_does_not_add_item_to_all_items():
    # An item not in catalog should not appear in evaluation output when only inactive signals reference it.
    evaluation = evaluate_hero(
        "Karnok",
        [CatalogItem("Known Item", phase="mid", archetype="Axe", bucket="support_items")],
        HeroStats(hero="Karnok"),
        [],
        game_change_signals=signals(
            _inactive_signal("removed_card", "Ghost Item", "deprecated", signal_id="dep-ghost")
        ),
    )

    item_names = {row["item"] for row in evaluation.rows}
    assert "Ghost Item" not in item_names


def test_bazaardb_stale_absence_removal_works_with_inactive_signals_present():
    dep = _inactive_signal("removed_card", "Old Support", "deprecated", signal_id="dep-1")
    stats = history_with_absent_windows("Karnok", "bazaardb", "Old Support", [0])

    evaluation = evaluate_hero(
        "Karnok",
        [CatalogItem("Old Support", phase="mid", archetype="Axe", bucket="support_items")],
        stats,
        [result("bazaardb", [], observed_at=observed_at_days(30))],
        game_change_signals=signals(dep),
    )

    row = row_for(evaluation, "Old Support")
    # BazaarDB stale absence still drives removal independently of inactive signals
    assert row["threshold_result"] == "remove_candidate"


def test_active_signals_freeze_behavior_unchanged():
    freeze_state = CuratorState(hero_freezes={"karnok": FreezeWindow("2026-12-31T00:00:00Z")})
    evaluation = evaluate_hero(
        "Karnok",
        [CatalogItem("Old Support", phase="mid", archetype="Axe", bucket="support_items")],
        HeroStats(hero="Karnok"),
        [result("bazaardb", [])],
        freeze_state,
        game_change_signals=signals(signal("removed_card", "Old Support", signal_id="sig-freeze")),
        now=datetime(2026, 6, 6, 12, tzinfo=timezone.utc),
    )

    row = row_for(evaluation, "Old Support")
    # Signal rows keep threshold_result=remove_candidate but show freeze via actionability
    assert row["threshold_result"] == "remove_candidate"
    assert "freeze_removals" in row["removal_blocked_by"]
    assert row["actionability"] == "freeze_blocked"
