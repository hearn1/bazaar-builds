from __future__ import annotations

import json

from automated_builds_pipeline.classification import ItemClassification
from automated_builds_pipeline.deterministic_classifier import DeterministicClassifier
from automated_builds_pipeline.diff import build_arg_parser, generate_diff, main
from automated_builds_pipeline.evaluator import EvaluationResult


class StaticClassifier:
    def __init__(self, classifications):
        self.classifications = classifications
        self.calls = []

    def classify_archetype(self, *args):
        self.calls.append(args)
        return list(self.classifications)


def evaluation(rows):
    return EvaluationResult(
        hero="Karnok",
        freeze_active=False,
        generated_at="2026-05-05T12:00:00Z",
        run_id="run1",
        bazaardb_patch={"label": "2026-W18"},
        source_health=[{"source": "bazaardb", "status": "healthy", "window_id": "bazaardb:2026-W18", "checked_at": "2026-05-05"}],
        rows=rows,
    )


def add_row(item, *, phase="early", archetype="Axe", ceiling="carry_core_support", existing=False):
    return {
        "hero": "Karnok",
        "phase": phase,
        "archetype": archetype,
        "archetype_status": "existing" if existing else "candidate_new",
        "item": item,
        "catalog_membership": "missing",
        "source_presence": {"bazaardb": "present"},
        "classification_ceiling": ceiling,
        "threshold_result": "add_candidate",
        "threshold_reason": "bazaardb_present_2_of_3_patches",
        "removal_blocked_by": [],
        "llm_input_required": True,
        "evidence_refs": [{"source": "bazaardb", "summary": f"bazaardb:{item}"}],
    }


def deferred_row(item, threshold_result, threshold_reason, *, phase="early", archetype="Axe"):
    return {
        "phase": phase,
        "archetype": archetype,
        "item": item,
        "threshold_result": threshold_result,
        "threshold_reason": threshold_reason,
    }


def test_diff_generator_mock_mode_populates_shape():
    rows = [
        add_row("New Core", existing=True),
        add_row("New Archetype Item", phase="late", archetype="Wide"),
        {"phase": "mid", "archetype": "Old", "item": "Old Core", "threshold_result": "remove_candidate", "threshold_reason": "bazaardb_absent_30_days", "evidence_refs": []},
    ]
    catalog = {"items": [{"item": "Existing", "phase": "early", "archetype": "Axe"}]}

    diff = generate_diff("Karnok", evaluation(rows), catalog, StaticClassifier([]), mock_mode=True)

    assert diff["schema_version"] == 1
    assert diff["classification_mode"] == "mock"
    assert diff["semantic_classification"] is False
    assert diff["classifier_provider"] == "none"
    assert set(diff["proposed_changes"]) == {
        "archetype_updates",
        "archetype_additions",
        "archetype_removal_candidates",
        "item_removal_candidates",
        "archetype_reshuffles",
    }
    update_item = diff["proposed_changes"]["archetype_updates"][0]["missing_items"][0]
    assert update_item["item"] == "New Core"
    assert update_item["llm_classification"] == "support"
    assert update_item["llm_confidence"] == "low"
    assert update_item["classification_ceiling"] == "carry_core_support"
    assert update_item["threshold_result"] == "add_candidate"
    assert update_item["threshold_reason"] == "bazaardb_present_2_of_3_patches"
    assert update_item["source_presence"] == {"bazaardb": "present"}
    addition = diff["proposed_changes"]["archetype_additions"][0]
    assert addition["candidate_core"] == []
    assert addition["candidate_support"][0]["item"] == "New Archetype Item"
    assert "candidate_pending" not in addition
    assert diff["proposed_changes"]["item_removal_candidates"][0]["item"] == "Old Core"


def test_diff_routes_support_retirements_to_item_removal_candidates():
    rows = [
        {
            "phase": "mid",
            "archetype": "Axe",
            "item": "Old Support",
            "threshold_result": "remove_candidate",
            "threshold_reason": "bazaardb_absent_30_days",
            "catalog_bucket": "support_items",
            "retirement_type": "support_item",
            "retirement_basis": "bazaardb_absent_30_days",
            "actionability": "item_removal_candidate",
            "affected_items": ["Old Support"],
            "signal_evidence": [],
            "source_presence": {
                "bazaardb": "absent",
                "mobalytics_meta_builds": "absent",
                "bazaar_builds_net": "absent",
            },
            "current_patch_evidence": {
                "bazaardb": {
                    "presence": "absent",
                    "window_id": "bazaardb:2026-W18",
                    "observed_at": "2026-05-05T12:00:00Z",
                }
            },
            "canonical_presence": "absent",
            "windows_seen": 4,
            "first_seen_window": "bazaardb:2026-W12",
            "last_seen_window": "bazaardb:2026-W14",
            "evidence_refs": [],
        },
    ]

    diff = generate_diff("Karnok", evaluation(rows), {"items": []}, StaticClassifier([]), mock_mode=True)

    assert diff["proposed_changes"]["archetype_removal_candidates"] == []
    removal = diff["proposed_changes"]["item_removal_candidates"][0]
    assert removal["item"] == "Old Support"
    assert removal["catalog_bucket"] == "support_items"
    assert removal["retirement_type"] == "support_item"
    assert removal["retirement_basis"] == "bazaardb_absent_30_days"
    assert removal["actionability"] == "item_removal_candidate"
    assert removal["affected_items"] == ["Old Support"]
    assert removal["signal_evidence"] == []
    assert removal["catalog_location"] == {
        "phase": "mid",
        "archetype": "Axe",
        "item": "Old Support",
        "bucket": "support_items",
    }
    assert removal["source_presence"]["bazaardb"] == "absent"
    assert removal["current_patch_evidence"]["bazaardb"]["window_id"] == "bazaardb:2026-W18"
    assert removal["canonical_presence"] == "absent"
    assert removal["windows_seen"] == 4
    assert removal["first_seen_window"] == "bazaardb:2026-W12"
    assert removal["last_seen_window"] == "bazaardb:2026-W14"


def test_diff_routes_bucket_review_retirements_away_from_item_removal_candidates():
    rows = [
        {
            "phase": "late",
            "archetype": "Axe",
            "item": "Battle Axe",
            "threshold_result": "retirement_review_candidate",
            "threshold_reason": "bazaardb_absent_30_days",
            "catalog_bucket": "carry_items",
            "retirement_type": "whole_build_review",
            "retirement_basis": "bazaardb_absent_30_days",
            "actionability": "review_required",
            "affected_items": ["Battle Axe"],
            "affected_item_details": [
                {
                    "item": "Battle Axe",
                    "catalog_bucket": "carry_items",
                    "phase": "late",
                    "archetype": "Axe",
                }
            ],
            "affected_build_items": {
                "carry_items": ["Battle Axe", "Sawpike"],
                "core_items": ["Hidden Lake"],
                "condition_items": ["Chains"],
            },
            "review_scope": "whole_build",
            "review_priority": "normal",
            "signal_evidence": [],
            "evidence_refs": [],
        },
    ]

    diff = generate_diff("Karnok", evaluation(rows), {"items": []}, StaticClassifier([]), mock_mode=True)

    assert diff["proposed_changes"]["item_removal_candidates"] == []
    review = diff["proposed_changes"]["archetype_removal_candidates"][0]
    assert review["item"] == "Battle Axe"
    assert review["catalog_bucket"] == "carry_items"
    assert review["retirement_type"] == "whole_build_review"
    assert review["retirement_basis"] == "bazaardb_absent_30_days"
    assert review["actionability"] == "review_required"
    assert review["affected_items"] == ["Battle Axe"]
    assert review["affected_item_details"][0]["catalog_bucket"] == "carry_items"
    assert review["affected_build_items"] == {
        "carry_items": ["Battle Axe", "Sawpike"],
        "core_items": ["Hidden Lake"],
        "condition_items": ["Chains"],
    }


def test_signal_evidence_flows_to_removal_and_review_rows():
    signal = {
        "id": "removed-old-support",
        "type": "removed_card",
        "item": "Old Support",
        "effective_date": "2026-05-01",
        "source_url": "https://example.test/signal",
        "note": "removed",
    }
    rows = [
        {
            "phase": "mid",
            "archetype": "Axe",
            "item": "Old Support",
            "catalog_bucket": "support_items",
            "threshold_result": "remove_candidate",
            "threshold_reason": "game_change_removed_card",
            "retirement_type": "support_item",
            "retirement_basis": "game_change_removed_card",
            "actionability": "item_removal_candidate",
            "affected_items": ["Old Support"],
            "signal_evidence": [signal],
            "removal_blocked_by": [],
            "evidence_refs": [],
        },
        {
            "phase": "mid",
            "archetype": "Axe",
            "item": "Battle Axe",
            "catalog_bucket": "carry_items",
            "threshold_result": "retirement_review_candidate",
            "threshold_reason": "game_change_explicit_invalidation",
            "retirement_type": "whole_build_review",
            "retirement_basis": "game_change_explicit_invalidation",
            "actionability": "review_required",
            "affected_items": ["Battle Axe"],
            "review_scope": "whole_build",
            "review_priority": "high",
            "signal_evidence": [{**signal, "id": "invalid-axe", "type": "explicit_invalidation"}],
            "removal_blocked_by": [],
            "evidence_refs": [],
        },
    ]

    diff = generate_diff("Karnok", evaluation(rows), {"items": []}, StaticClassifier([]), mock_mode=True)

    item = diff["proposed_changes"]["item_removal_candidates"][0]
    review = diff["proposed_changes"]["archetype_removal_candidates"][0]
    assert item["signal_evidence"] == [signal]
    assert item["retirement_basis"] == "game_change_removed_card"
    assert review["signal_evidence"][0]["id"] == "invalid-axe"
    assert review["review_priority"] == "high"
    assert review["review_scope"] == "whole_build"


def test_source_quality_gate_coerces_carry_to_support():
    classifier = StaticClassifier([ItemClassification("Secondary Item", "carry", "high", "would carry", "top_line")])

    diff = generate_diff(
        "Karnok",
        evaluation([add_row("Secondary Item", ceiling="support_only")]),
        {"items": []},
        classifier,
    )

    item = diff["proposed_changes"]["archetype_additions"][0]["candidate_support"][0]
    assert item["llm_classification"] == "support"
    assert "capped" in item["llm_rationale"]
    assert diff["classification_mode"] == "deterministic"
    assert diff["semantic_classification"] is True
    assert diff["classifier_provider"] == "deterministic"


def test_no_llm_shadow_mode_emits_pending_candidates_without_classifier_call():
    classifier = StaticClassifier([ItemClassification("Should Not Call", "core", "high", "unused", "top_line")])

    diff = generate_diff(
        "Karnok",
        evaluation([add_row("Observed Item")]),
        {"items": []},
        classifier,
        classifier_mode="no_llm_shadow",
    )

    addition = diff["proposed_changes"]["archetype_additions"][0]
    item = addition["candidate_pending"][0]
    assert classifier.calls == []
    assert addition["candidate_core"] == []
    assert addition["candidate_support"] == []
    assert item["item"] == "Observed Item"
    assert item["llm_classification"] == "classification_pending"
    assert item["llm_confidence"] == "none"
    assert "Observation-only shadow output" in item["llm_rationale"]
    assert item["evidence_refs"] == [{"source": "bazaardb", "summary": "bazaardb:Observed Item"}]


def test_medium_confidence_goes_to_weaker_signals_and_low_to_noise():
    classifier = StaticClassifier(
        [
            ItemClassification("Medium Item", "support", "medium", "role inferred", "weaker_signal"),
            ItemClassification("Low Item", "support", "low", "thin", "suppressed"),
        ]
    )

    diff = generate_diff(
        "Karnok",
        evaluation([add_row("Medium Item"), add_row("Low Item")]),
        {"items": []},
        classifier,
    )

    assert diff["weaker_signals"][0]["item"] == "Medium Item"
    assert any(row["reason"] == "low_confidence_suppressed" for row in diff["noise"])


def test_no_llm_shadow_keeps_removal_rows_but_no_semantic_labels():
    rows = [
        add_row("New Core", existing=True),
        {
            "phase": "mid",
            "archetype": "Old",
            "item": "Old Core",
            "threshold_result": "remove_candidate",
            "threshold_reason": "bazaardb_absent_30_days",
            "evidence_refs": [],
        },
    ]

    diff = generate_diff(
        "Karnok",
        evaluation(rows),
        {"items": [{"item": "Existing", "phase": "early", "archetype": "Axe"}]},
        None,
        classifier_mode="no_llm_shadow",
    )

    item = diff["proposed_changes"]["archetype_updates"][0]["missing_items"][0]
    assert item["llm_classification"] == "classification_pending"
    assert diff["proposed_changes"]["item_removal_candidates"][0]["item"] == "Old Core"
    serialized = json.dumps(diff)
    assert '"llm_classification": "carry"' not in serialized
    assert '"llm_classification": "core"' not in serialized
    assert '"llm_classification": "support"' not in serialized


def test_insufficient_history_rows_are_suppressed_from_noise():
    rows = [
        deferred_row("Thin Sample 1", "insufficient_history", "not_enough_windows"),
        deferred_row("Thin Sample 2", "insufficient_history", "not_enough_windows"),
    ]

    diff = generate_diff("Karnok", evaluation(rows), {"items": []}, StaticClassifier([]), mock_mode=True)

    assert diff["noise"] == []


def test_deferred_no_change_and_blocked_rows_roll_up_by_reason():
    rows = [
        deferred_row("Blocked 1", "blocked", "freeze_active"),
        deferred_row("Blocked 2", "blocked", "freeze_active"),
        deferred_row("No Change 1", "no_change", "secondary_present"),
        deferred_row("No Change 2", "no_change", "secondary_present"),
        deferred_row("No Change 3", "no_change", "secondary_present"),
    ]

    diff = generate_diff("Karnok", evaluation(rows), {"items": []}, StaticClassifier([]), mock_mode=True)

    assert diff["noise"] == [
        {"reason": "freeze_active", "count": 2, "summary": True},
        {"reason": "secondary_present", "count": 3, "summary": True},
    ]


def test_reshuffle_signal_goes_to_noise_not_reserved_slot():
    row = add_row("Moved Item")
    row["archetype_reshuffle_deferred"] = True

    diff = generate_diff("Karnok", evaluation([row]), {"items": []}, StaticClassifier([]), mock_mode=True)

    assert diff["proposed_changes"]["archetype_reshuffles"] == []
    assert any(row["reason"] == "reshuffle_deferred" for row in diff["noise"])


def test_window_id_prefers_bazaardb_patch_label():
    result = evaluation([])
    result.source_health = [
        {"source": "bazaardb", "status": "healthy", "window_id": "bazaardb:2026-W18", "checked_at": "2026-05-05"},
        {"source": "mobalytics_meta_builds", "status": "healthy", "window_id": "mobalytics_meta_builds:v540", "checked_at": "2026-05-05"},
    ]

    diff = generate_diff("Karnok", result, {"items": []}, StaticClassifier([]), mock_mode=True)

    assert diff["window_id"] == "2026-W18"


def test_window_id_uses_only_healthy_useful_source_windows_without_patch_label():
    result = evaluation([])
    result.bazaardb_patch = None
    result.source_health = [
        {"source": "bazaardb", "status": "healthy", "window_id": "bazaardb:2026-W18", "checked_at": "2026-05-05"},
        {"source": "mobalytics_meta_builds", "status": "unhealthy", "window_id": "mobalytics_meta_builds:unknown", "checked_at": "2026-05-05"},
        {"source": "bazaar_builds_net", "status": "healthy", "window_id": "bazaar_builds_net:2026-W19", "checked_at": "2026-05-05"},
    ]

    diff = generate_diff("Karnok", result, {"items": []}, StaticClassifier([]), mock_mode=True)

    assert diff["window_id"] == "bazaardb:2026-W18+bazaar_builds_net:2026-W19"


def test_window_id_falls_back_to_run_id_when_no_healthy_useful_windows_remain():
    result = evaluation([])
    result.bazaardb_patch = None
    result.run_id = "run-fallback"
    result.source_health = [
        {"source": "bazaardb", "status": "unhealthy", "window_id": "bazaardb:unknown", "checked_at": "2026-05-05"},
        {"source": "mobalytics_meta_builds", "status": "healthy", "window_id": "mobalytics_meta_builds:unknown", "checked_at": "2026-05-05"},
    ]

    diff = generate_diff("Karnok", result, {"items": []}, StaticClassifier([]), mock_mode=True)

    assert diff["window_id"] == "run-fallback"


def test_arg_parser_accepts_deterministic_mode():
    parser = build_arg_parser()
    ns = parser.parse_args(
        [
            "--hero", "Karnok",
            "--evaluation", "e.json",
            "--catalog", "c.json",
            "--names-file", "n.txt",
            "--output-dir", "out",
            "--classifier-mode", "deterministic",
        ]
    )
    assert ns.classifier_mode == "deterministic"


def test_deterministic_mode_classifies_into_core_and_support():
    carry_row = add_row("Pylon", archetype="Pylon Build")
    carry_row["current_patch_evidence"] = {
        "bazaardb": {"presence": "present", "metadata": {"section": "CORE ITEMS"}, "sample_count": 40, "frequency": 1.0}
    }
    carry_row["within_patch_strength"] = "statistical_core"
    core_row = add_row("Sidekick", archetype="Pylon Build")
    core_row["current_patch_evidence"] = {
        "bazaardb": {"presence": "present", "metadata": {"section": "CORE ITEMS"}, "sample_count": 40, "frequency": 1.0}
    }
    core_row["within_patch_strength"] = "statistical_core"

    classifier = DeterministicClassifier()
    classifier.known_items = {"Pylon", "Sidekick"}

    diff = generate_diff(
        "Karnok",
        evaluation([carry_row, core_row]),
        {"items": []},
        classifier,
        classifier_mode="deterministic",
    )

    assert diff["classification_mode"] == "deterministic"
    assert diff["semantic_classification"] is True
    assert diff["classifier_provider"] == "deterministic"

    addition = diff["proposed_changes"]["archetype_additions"][0]
    core_items = {item["item"] for item in addition["candidate_core"]}
    assert core_items == {"Pylon", "Sidekick"}
    assert addition["candidate_support"] == []
    assert "candidate_pending" not in addition


def test_cli_smoke_mock_writes_diff_and_proposal(tmp_path):
    evaluation_path = tmp_path / "evaluation.json"
    catalog_path = tmp_path / "catalog.json"
    names_path = tmp_path / "names.txt"
    output_dir = tmp_path / "out"
    evaluation_path.write_text(json.dumps(evaluation([add_row("Pufferfish")]).to_dict()), encoding="utf-8")
    catalog_path.write_text(json.dumps({"items": []}), encoding="utf-8")
    names_path.write_text("Pufferfish\n", encoding="utf-8")

    assert main(["--hero", "Karnok", "--evaluation", str(evaluation_path), "--catalog", str(catalog_path), "--names-file", str(names_path), "--output-dir", str(output_dir), "--mock"]) == 0

    assert json.loads((output_dir / "Karnok_diff.json").read_text(encoding="utf-8"))["hero"] == "Karnok"
    assert "## Pipeline State" in (output_dir / "Karnok_build_update_proposal.md").read_text(encoding="utf-8")


def _multi_source_row(item: str, *, ceiling: str = "support_only", existing: bool = False, phase: str = "early", archetype: str = "Mixed") -> dict:
    """Row where two secondary (non-bazaardb, non-mobalytics_meta) sources agree.

    Uses source_presence keys that are not in the bazaardb or MOBALYTICS check
    paths so the row falls through to the source_count >= 2 branch.
    """
    return {
        "hero": "Karnok",
        "phase": phase,
        "archetype": archetype,
        "archetype_status": "existing" if existing else "candidate_new",
        "item": item,
        "catalog_membership": "missing",
        # Two sources present, neither bazaardb nor mobalytics_meta_builds
        "source_presence": {"mobalytics_build_articles": "present", "bazaar_builds_net": "present"},
        "classification_ceiling": ceiling,
        "threshold_result": "add_candidate",
        "threshold_reason": "multi_source_current",
        "removal_blocked_by": [],
        "llm_input_required": True,
        "evidence_refs": [],
        "within_patch_strength": "multi_source_current",
        "current_source_support_count": 2,
    }


def test_cross_source_promotion_off_by_default_goes_to_weaker_signals():
    """Without promote_cross_source, ≥2-source agreement lands in weaker_signals."""
    classifier = DeterministicClassifier()
    classifier.known_items = {"Frost Nova"}

    diff = generate_diff(
        "Karnok",
        evaluation([_multi_source_row("Frost Nova", existing=True)]),
        {"items": [{"item": "Existing", "phase": "early", "archetype": "Mixed"}]},
        classifier,
        classifier_mode="deterministic",
    )

    # Default: should be weaker_signal, not top_line
    assert not diff["proposed_changes"]["archetype_updates"], "Should not appear in updates without promotion"
    assert any(w.get("item") == "Frost Nova" for w in diff["weaker_signals"]), (
        "Frost Nova should be in weaker_signals when promote_cross_source=False"
    )


def test_cross_source_promotion_surfaces_multi_source_to_top_line():
    """With promote_cross_source=True, ≥2-source agreement is promoted to top_line as support."""
    classifier = DeterministicClassifier(promote_cross_source=True)
    classifier.known_items = {"Frost Nova"}

    diff = generate_diff(
        "Karnok",
        evaluation([_multi_source_row("Frost Nova", existing=True)]),
        {"items": [{"item": "Existing", "phase": "early", "archetype": "Mixed"}]},
        classifier,
        classifier_mode="deterministic",
    )

    assert diff["proposed_changes"]["archetype_updates"], "Should appear in archetype_updates with promotion"
    update = diff["proposed_changes"]["archetype_updates"][0]
    items = {i["item"]: i for i in update["missing_items"]}
    assert "Frost Nova" in items, "Frost Nova should be in top_line items"
    assert items["Frost Nova"]["llm_classification"] == "support"
    assert items["Frost Nova"]["llm_confidence"] == "high"
    assert not diff["weaker_signals"], "Should not remain in weaker_signals"


def test_cross_source_promotion_never_promotes_to_core():
    """promote_cross_source lifts surfacing only; agreement alone never promotes to core."""
    classifier = DeterministicClassifier(promote_cross_source=True)
    classifier.known_items = {"Frost Nova"}

    diff = generate_diff(
        "Karnok",
        evaluation([_multi_source_row("Frost Nova", existing=True)]),
        {"items": [{"item": "Existing", "phase": "early", "archetype": "Mixed"}]},
        classifier,
        classifier_mode="deterministic",
    )

    update = diff["proposed_changes"]["archetype_updates"][0]
    for item in update["missing_items"]:
        assert item["llm_classification"] != "core", (
            f"cross-source agreement must not promote {item['item']} to core"
        )
        assert item["llm_classification"] != "carry", (
            f"cross-source agreement must not promote {item['item']} to carry"
        )


def test_support_only_ceiling_cap_still_holds_with_promotion():
    """support_only ceiling cap remains intact even when promote_cross_source=True."""
    # Item has support_only ceiling; classifier would try to classify as carry/core
    # but the ceiling cap in diff.py must coerce it back to support.
    # With promote_cross_source, the item reaches top_line but stays as support.
    classifier = DeterministicClassifier(promote_cross_source=True)
    classifier.known_items = {"Ice Barrier"}

    row = _multi_source_row("Ice Barrier", ceiling="support_only", existing=True)

    diff = generate_diff(
        "Karnok",
        evaluation([row]),
        {"items": [{"item": "Existing", "phase": "early", "archetype": "Mixed"}]},
        classifier,
        classifier_mode="deterministic",
    )

    # With support_only ceiling + promotion: item should reach top_line AS support
    updates = diff["proposed_changes"]["archetype_updates"]
    assert updates, "support_only + promoted item should still reach top_line"
    items = {i["item"]: i for i in updates[0]["missing_items"]}
    assert "Ice Barrier" in items
    assert items["Ice Barrier"]["llm_classification"] == "support", (
        "support_only ceiling cap must keep classification as support"
    )


def test_cross_source_promotion_rationale_includes_marker():
    """Promoted items carry 'cross-source agreement' in their rationale."""
    classifier = DeterministicClassifier(promote_cross_source=True)
    classifier.known_items = {"Frost Nova"}

    diff = generate_diff(
        "Karnok",
        evaluation([_multi_source_row("Frost Nova", existing=True)]),
        {"items": [{"item": "Existing", "phase": "early", "archetype": "Mixed"}]},
        classifier,
        classifier_mode="deterministic",
    )

    update = diff["proposed_changes"]["archetype_updates"][0]
    item = next(i for i in update["missing_items"] if i["item"] == "Frost Nova")
    assert "cross-source agreement" in item["llm_rationale"].lower(), (
        f"Expected 'cross-source agreement' in rationale, got: {item['llm_rationale']}"
    )
