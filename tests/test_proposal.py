from __future__ import annotations

from automated_builds_pipeline.diff import partition_diff
from automated_builds_pipeline.proposal import render_proposal


def test_renderer_outputs_all_empty_sections_and_freeze_line():
    diff = {
        "hero": "Karnok",
        "source_window": {"start": "2026-05-01", "end": "2026-05-05", "n_windows_history": 3},
        "freeze_state": {"removals_frozen": True, "patch_label": "2026-W18"},
        "source_health": [],
        "proposed_changes": {
            "archetype_updates": [],
            "archetype_additions": [],
            "archetype_removal_candidates": [],
            "item_removal_candidates": [],
            "archetype_reshuffles": [],
        },
        "weaker_signals": [],
        "noise": [],
    }

    markdown = render_proposal(diff)

    assert "## Pipeline State" in markdown
    assert "Removals frozen" in markdown
    assert "## Existing Archetype Updates" in markdown
    assert "## New Archetype Candidates" in markdown
    assert "## Removal Candidates" in markdown
    assert "## Weaker Signals" in markdown
    assert "## Noise / No Evidence" in markdown
    assert markdown.count("No items in this section.") == 5


def test_renderer_includes_update_addition_removal_weaker_and_noise():
    item = {"item": "Pufferfish", "llm_classification": "core", "llm_confidence": "high", "llm_rationale": "bazaardb confirms", "evidence_refs": [{"summary": "bazaardb:puffer"}]}
    diff = {
        "hero": "Karnok",
        "source_window": {"start": "2026-05-01", "end": "2026-05-05", "n_windows_history": 3},
        "freeze_state": {"removals_frozen": False, "patch_label": None},
        "source_health": [{"source": "bazaardb", "status": "healthy", "window_id": "w1"}],
        "proposed_changes": {
            "archetype_updates": [{"phase": "early", "archetype": "Axe", "missing_items": [item]}],
            "archetype_additions": [{"candidate_phase": "late", "tag": "Wide", "candidate_core": [item], "candidate_support": []}],
            "archetype_removal_candidates": [{"phase": "late", "archetype": "Old", "reason": "absent"}],
            "item_removal_candidates": [{"phase": "mid", "archetype": "Axe", "item": "Old Core", "reason": "absent", "removal_blocked_by": ["mobalytics_meta_builds"]}],
            "archetype_reshuffles": [],
        },
        "weaker_signals": [{"phase": "early", "archetype": "Axe", **item}],
        "noise": [{"reason": "reshuffle_deferred", "item": "Yo-Yo"}],
    }

    markdown = render_proposal(diff)

    assert "Pufferfish" in markdown
    assert "Secondary/blocking context: mobalytics_meta_builds" in markdown
    assert "reshuffle_deferred" in markdown


def test_renderer_warns_when_invalid_classifier_items_suggest_stale_name_list():
    base_changes = {
        "archetype_updates": [],
        "archetype_additions": [],
        "archetype_removal_candidates": [],
        "item_removal_candidates": [],
        "archetype_reshuffles": [],
    }
    diff = {
        "hero": "Karnok",
        "source_window": {"start": "2026-05-01", "end": "2026-05-05", "n_windows_history": 3},
        "freeze_state": {"removals_frozen": False, "patch_label": None},
        "source_health": [],
        "proposed_changes": dict(base_changes),
        "weaker_signals": [],
        "noise": [
            {"reason": "invalid_classifier_item", "item": "Starfish", "archetype": "Aquatic"},
            {"reason": "invalid_classifier_item", "item": "Atlas Stone", "archetype": "Aquatic"},
            {"reason": "secondary_present_bazaardb_absent", "count": 3, "summary": True},
        ],
    }

    markdown = render_proposal(diff)

    assert "2 item name(s) were dropped as invalid_classifier_item" in markdown
    assert "card_cache_names.txt may be stale" in markdown

    # Summary rollups are counted too.
    diff_summary = {
        **diff,
        "proposed_changes": dict(base_changes),
        "noise": [{"reason": "invalid_classifier_item", "count": 5, "summary": True}],
    }
    assert "5 item name(s) were dropped as invalid_classifier_item" in render_proposal(diff_summary)

    # No invalid items → no warning.
    diff_clean = {**diff, "proposed_changes": dict(base_changes), "noise": []}
    assert "invalid_classifier_item" not in render_proposal(diff_clean)


def test_renderer_warns_for_no_llm_shadow_and_lists_pending_candidates():
    item = {
        "item": "Pufferfish",
        "llm_classification": "classification_pending",
        "llm_confidence": "none",
        "llm_rationale": "Observation-only shadow output.",
        "evidence_refs": [{"summary": "bazaardb:puffer"}],
    }
    diff = {
        "hero": "Karnok",
        "classification_mode": "no_llm_shadow",
        "semantic_classification": False,
        "classifier_provider": "none",
        "source_window": {"start": "2026-05-01", "end": "2026-05-05", "n_windows_history": 3},
        "freeze_state": {"removals_frozen": False, "patch_label": None},
        "source_health": [],
        "proposed_changes": {
            "archetype_updates": [{"phase": "early", "archetype": "Axe", "missing_items": [item]}],
            "archetype_additions": [
                {
                    "candidate_phase": "late",
                    "tag": "Wide",
                    "candidate_core": [],
                    "candidate_support": [],
                    "candidate_pending": [item],
                }
            ],
            "archetype_removal_candidates": [],
            "item_removal_candidates": [],
            "archetype_reshuffles": [],
        },
        "weaker_signals": [],
        "noise": [],
    }

    markdown = render_proposal(diff)

    assert "Classification mode: no_llm_shadow" in markdown
    assert "Classifier provider: none" in markdown
    assert "operational evidence only" in markdown
    assert "Pending semantic classification:" in markdown
    assert "classification_pending (none)" in markdown


def test_renderer_shows_retirement_review_affected_commitment_pieces():
    diff = {
        "hero": "Karnok",
        "source_window": {"start": "2026-05-01", "end": "2026-05-05", "n_windows_history": 3},
        "freeze_state": {"removals_frozen": False, "patch_label": None},
        "source_health": [],
        "proposed_changes": {
            "archetype_updates": [],
            "archetype_additions": [],
            "archetype_removal_candidates": [
                {
                    "phase": "mid",
                    "archetype": "Axe",
                    "item": "Battle Axe",
                    "reason": "bazaardb_absent_30_days",
                    "retirement_type": "whole_build_review",
                    "affected_items": ["Battle Axe"],
                    "affected_build_items": {
                        "carry_items": ["Battle Axe", "Sawpike"],
                        "core_items": ["Hidden Lake"],
                        "condition_items": ["Chains"],
                    },
                }
            ],
            "item_removal_candidates": [],
            "archetype_reshuffles": [],
        },
        "weaker_signals": [],
        "noise": [],
    }

    markdown = render_proposal(diff)

    assert "### Retirement Reviews" in markdown
    assert (
        "mid / Axe / Battle Axe | bucket: unknown | type: whole_build_review | "
        "basis: bazaardb_absent_30_days | action: review | affected: Battle Axe"
    ) in markdown
    assert "carry_items: Battle Axe, Sawpike" in markdown
    assert "core_items: Hidden Lake" in markdown
    assert "condition_items: Chains" in markdown


def test_renderer_shows_game_change_signal_metadata_for_retirements():
    diff = {
        "hero": "Karnok",
        "source_window": {"start": "2026-05-01", "end": "2026-05-05", "n_windows_history": 3},
        "freeze_state": {"removals_frozen": False, "patch_label": None},
        "source_health": [],
        "proposed_changes": {
            "archetype_updates": [],
            "archetype_additions": [],
            "archetype_removal_candidates": [],
            "item_removal_candidates": [
                {
                    "phase": "mid",
                    "archetype": "Axe",
                    "item": "Old Support",
                    "reason": "game_change_renamed_card",
                    "signal_evidence": [
                        {
                            "id": "rename-old-support",
                            "type": "renamed_card",
                            "effective_date": "2026-05-01",
                            "replacement_item": "New Support",
                            "source_url": "https://example.test/signal",
                        }
                    ],
                }
            ],
            "archetype_reshuffles": [],
        },
        "weaker_signals": [],
        "noise": [],
    }

    markdown = render_proposal(diff)

    assert "Old Support | bucket: unknown | type: review | basis: game_change_renamed_card | action: review" in markdown
    assert "Signal: rename-old-support; renamed_card; 2026-05-01; replacement: New Support; https://example.test/signal" in markdown


def test_renderer_shows_compact_retirement_ux_fields_and_blocked_state():
    diff = {
        "hero": "Karnok",
        "source_window": {"start": "2026-05-01", "end": "2026-05-05", "n_windows_history": 3},
        "freeze_state": {"removals_frozen": True, "patch_label": "2026-W18"},
        "source_health": [],
        "proposed_changes": {
            "archetype_updates": [],
            "archetype_additions": [],
            "archetype_removal_candidates": [],
            "item_removal_candidates": [
                {
                    "phase": "mid",
                    "archetype": "Axe",
                    "item": "Old Support",
                    "catalog_bucket": "support_items",
                    "retirement_type": "support_item",
                    "retirement_basis": "game_change_removed_card",
                    "actionability": "freeze_blocked",
                    "reason": "game_change_removed_card",
                    "source_presence": {
                        "bazaardb": "absent",
                        "mobalytics_meta_builds": "present",
                        "bazaar_builds_net": "absent",
                    },
                    "current_patch_evidence": {
                        "bazaardb": {"presence": "absent", "window_id": "bazaardb:2026-W18"}
                    },
                    "removal_blocked_by": ["mobalytics_meta_builds", "freeze_removals"],
                    "freeze_blocked": True,
                    "signal_evidence": [{"id": "removed-old-support", "type": "removed_card", "effective_date": "2026-05-01"}],
                }
            ],
            "archetype_reshuffles": [],
        },
        "weaker_signals": [],
        "noise": [],
    }

    markdown = render_proposal(diff)

    assert (
        "- mid / Axe / Old Support | bucket: support_items | type: support_item | "
        "basis: game_change_removed_card | action: freeze_blocked | current BazaarDB: absent (bazaardb:2026-W18) | "
        "secondary blocker: mobalytics_meta_builds | freeze: blocked | signal: removed-old-support"
    ) in markdown
    assert "Secondary/blocking context: mobalytics_meta_builds, freeze_removals" in markdown
    assert "Removals frozen." in markdown


def test_partitioned_proposal_views_filter_additions_and_retirements():
    item = {
        "item": "Pufferfish",
        "llm_classification": "core",
        "llm_confidence": "high",
        "llm_rationale": "bazaardb confirms",
    }
    diff = {
        "hero": "Karnok",
        "source_window": {"start": "2026-05-01", "end": "2026-05-05", "n_windows_history": 3},
        "freeze_state": {"removals_frozen": False, "patch_label": None},
        "source_health": [],
        "proposed_changes": {
            "archetype_updates": [{"phase": "early_mid", "archetype": "Axe", "missing_items": [item]}],
            "archetype_additions": [],
            "archetype_removal_candidates": [
                {
                    "phase": "late",
                    "archetype": "Axe",
                    "item": "Battle Axe",
                    "catalog_bucket": "carry_items",
                    "retirement_type": "whole_build_review",
                    "retirement_basis": "bazaardb_absent_30_days",
                    "actionability": "review_required",
                    "affected_items": ["Battle Axe"],
                }
            ],
            "item_removal_candidates": [],
            "archetype_reshuffles": [],
        },
        "weaker_signals": [{"phase": "early_mid", "archetype": "Axe", **item}],
        "noise": [],
    }

    parts = partition_diff(diff)
    addition_markdown = render_proposal(parts.additions)
    retirement_markdown = render_proposal(parts.retirements)

    assert "Pufferfish" in addition_markdown
    assert "Battle Axe" not in addition_markdown
    assert "Battle Axe" in retirement_markdown
    assert "Pufferfish" not in retirement_markdown
    assert "### Retirement Reviews" in retirement_markdown
