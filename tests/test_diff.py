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
        {"phase": "mid", "archetype": "Old", "item": "Old Core", "threshold_result": "remove_candidate", "threshold_reason": "bazaardb_absent_4_patches_21_days", "evidence_refs": []},
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
            "threshold_reason": "bazaardb_absent_4_patches_21_days",
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
