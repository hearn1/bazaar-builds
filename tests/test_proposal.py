from __future__ import annotations

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
    assert "Blocked by: mobalytics_meta_builds" in markdown
    assert "reshuffle_deferred" in markdown


def test_renderer_warns_for_no_llm_shadow_and_lists_pending_candidates():
    item = {
        "item": "Pufferfish",
        "llm_classification": "classification_pending",
        "llm_confidence": "none",
        "llm_rationale": "Deterministic shadow observation only.",
        "evidence_refs": [{"summary": "bazaardb:puffer"}],
    }
    diff = {
        "hero": "Karnok",
        "classification_mode": "no_llm_shadow",
        "semantic_classification": False,
        "llm_provider": "none",
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
    assert "LLM provider: none" in markdown
    assert "operational observation evidence only" in markdown
    assert "Pending semantic classification:" in markdown
    assert "classification_pending (none)" in markdown
