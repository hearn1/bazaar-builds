from __future__ import annotations

import copy

import pytest

from automated_builds_pipeline.applier import (
    ApplierError,
    apply_proposed_changes,
    ensure_supported_schema_version,
    serialize_catalog,
    validate_catalog,
)
from automated_builds_pipeline.classification import ItemClassification
from automated_builds_pipeline.diff import generate_diff

from test_diff import StaticClassifier, add_row, evaluation


def small_catalog() -> dict:
    return {
        "schema_version": 1,
        "hero": "Karnok",
        "season": 13,
        "last_updated": "2026-05-05",
        "notes": "test",
        "item_tier_list": {"description": "x"},
        "game_phases": {
            "early": {
                "day_range": "Day 1-4",
                "description": "early",
                "universal_utility_items": [],
                "economy_items": [],
            },
            "early_mid": {
                "day_range": "Day 4-7",
                "description": "early_mid",
                "archetypes": [
                    {
                        "name": "Axe",
                        "phase": "early_mid",
                        "carry_items": ["Battle Axe"],
                        "support_items": ["Bagpipes"],
                        "notes": "n",
                    }
                ],
            },
            "late": {
                "day_range": "Day 7-13",
                "description": "late",
                "archetypes": [],
            },
        },
    }


def diff_with(proposed: dict, *, semantic: bool = True) -> dict:
    return {
        "schema_version": 1,
        "hero": "Karnok",
        "semantic_classification": semantic,
        "proposed_changes": {
            "archetype_updates": [],
            "archetype_additions": [],
            "archetype_removal_candidates": [],
            "item_removal_candidates": [],
            "archetype_reshuffles": [],
            **proposed,
        },
    }


def item(name: str, classification: str) -> dict:
    return {"item": name, "llm_classification": classification}


def support_removal(
    item_name: str = "Bagpipes",
    *,
    phase: str | None = "early_mid",
    archetype: str | None = "Axe",
    catalog_bucket: str | None = "support_items",
    retirement_type: str | None = "support_item",
    actionability: str | None = "item_removal_candidate",
    blocked_by: list[str] | None = None,
    freeze_blocked: bool = False,
) -> dict:
    return {
        "phase": phase,
        "archetype": archetype,
        "item": item_name,
        "catalog_bucket": catalog_bucket,
        "retirement_type": retirement_type,
        "retirement_basis": "bazaardb_absent_30_days",
        "actionability": actionability,
        "removal_blocked_by": blocked_by or [],
        "freeze_blocked": freeze_blocked,
    }


# --- serialization -----------------------------------------------------------


def test_serialize_catalog_is_canonical_and_idempotent():
    catalog = small_catalog()
    once = serialize_catalog(catalog)
    assert once.endswith("\n")
    assert once.startswith("{\n  ")  # indent=2
    # Round-tripping a canonical catalog is byte-stable.
    import json

    assert serialize_catalog(json.loads(once)) == once


def test_serialize_preserves_key_order_not_sorted():
    catalog = {"schema_version": 1, "hero": "Z", "aaa": 1}
    out = serialize_catalog(catalog)
    assert out.index('"schema_version"') < out.index('"hero"') < out.index('"aaa"')


# --- archetype_updates -------------------------------------------------------


def test_update_routes_classifications_to_buckets():
    catalog = small_catalog()
    diff = diff_with(
        {
            "archetype_updates": [
                {
                    "phase": "early_mid",
                    "archetype": "Axe",
                    "missing_items": [
                        item("Sawpike", "carry"),
                        item("Adrenaline Shot", "support"),
                    ],
                }
            ]
        }
    )
    result = apply_proposed_changes(catalog, diff)
    axe = result.catalog["game_phases"]["early_mid"]["archetypes"][0]
    assert axe["carry_items"] == ["Battle Axe", "Sawpike"]
    assert axe["support_items"] == ["Bagpipes", "Adrenaline Shot"]
    assert result.changed is True


def test_update_does_not_duplicate_existing_item():
    catalog = small_catalog()
    diff = diff_with(
        {
            "archetype_updates": [
                {
                    "phase": "early_mid",
                    "archetype": "Axe",
                    "missing_items": [item("Battle Axe", "carry")],
                }
            ]
        }
    )
    result = apply_proposed_changes(catalog, diff)
    axe = result.catalog["game_phases"]["early_mid"]["archetypes"][0]
    assert axe["carry_items"] == ["Battle Axe"]
    assert result.changed is False


def test_update_creates_missing_core_items_key_in_canonical_order():
    catalog = small_catalog()
    diff = diff_with(
        {
            "archetype_updates": [
                {
                    "phase": "early_mid",
                    "archetype": "Axe",
                    "missing_items": [item("Hidden Lake", "core")],
                }
            ]
        }
    )
    result = apply_proposed_changes(catalog, diff)
    axe = result.catalog["game_phases"]["early_mid"]["archetypes"][0]
    assert axe["core_items"] == ["Hidden Lake"]
    # New key inserted in canonical position (core_items before carry_items).
    assert list(axe.keys()) == [
        "name",
        "phase",
        "core_items",
        "carry_items",
        "support_items",
        "notes",
    ]


def test_pure_content_change_leaves_existing_key_order_untouched():
    catalog = small_catalog()
    # Deliberately non-canonical existing key order.
    catalog["game_phases"]["early_mid"]["archetypes"][0] = {
        "name": "Axe",
        "support_items": ["Bagpipes"],
        "carry_items": ["Battle Axe"],
    }
    diff = diff_with(
        {
            "archetype_updates": [
                {
                    "phase": "early_mid",
                    "archetype": "Axe",
                    "missing_items": [item("Sawpike", "carry")],
                }
            ]
        }
    )
    result = apply_proposed_changes(catalog, diff)
    axe = result.catalog["game_phases"]["early_mid"]["archetypes"][0]
    assert list(axe.keys()) == ["name", "support_items", "carry_items"]
    assert axe["carry_items"] == ["Battle Axe", "Sawpike"]


def test_update_unknown_archetype_is_skipped_not_crash():
    catalog = small_catalog()
    diff = diff_with(
        {
            "archetype_updates": [
                {
                    "phase": "early_mid",
                    "archetype": "Nonexistent",
                    "missing_items": [item("X", "carry")],
                }
            ]
        }
    )
    result = apply_proposed_changes(catalog, diff)
    assert result.changed is False
    assert any("not found" in s for s in result.skipped)


# --- archetype_additions -----------------------------------------------------


def test_addition_appends_new_archetype_in_canonical_order():
    catalog = small_catalog()
    diff = diff_with(
        {
            "archetype_additions": [
                {
                    "tag": "Burn",
                    "candidate_phase": "late",
                    "candidate_core": [
                        item("Hunter's Sled", "core"),
                        item("Burn Scar", "carry"),
                    ],
                    "candidate_support": [item("Caustic Solvent", "support")],
                }
            ]
        }
    )
    result = apply_proposed_changes(catalog, diff)
    late = result.catalog["game_phases"]["late"]["archetypes"]
    assert len(late) == 1
    burn = late[0]
    assert list(burn.keys()) == [
        "name",
        "phase",
        "core_items",
        "carry_items",
        "support_items",
    ]
    assert burn["name"] == "Burn"
    assert burn["core_items"] == ["Hunter's Sled"]
    assert burn["carry_items"] == ["Burn Scar"]
    assert burn["support_items"] == ["Caustic Solvent"]


def test_addition_with_existing_name_merges_instead_of_duplicating():
    catalog = small_catalog()
    diff = diff_with(
        {
            "archetype_additions": [
                {
                    "tag": "Axe",
                    "candidate_phase": "early_mid",
                    "candidate_core": [item("Sawpike", "carry")],
                    "candidate_support": [],
                }
            ]
        }
    )
    result = apply_proposed_changes(catalog, diff)
    archetypes = result.catalog["game_phases"]["early_mid"]["archetypes"]
    assert len(archetypes) == 1  # not duplicated
    assert archetypes[0]["carry_items"] == ["Battle Axe", "Sawpike"]


def test_addition_with_only_support_classifications_is_refused():
    # Mirror of coach's test_archetypes_have_commitment_bucket: an archetype
    # whose evidence collapsed to support-only must not be added to the
    # catalog, since it would score 1.0 on bare item ownership in the coach
    # scorer. See hearn1/bazaar-builds#104.
    catalog = small_catalog()
    diff = diff_with(
        {
            "archetype_additions": [
                {
                    "tag": "Night Vision / Chains / Fairy Circle",
                    "candidate_phase": "late",
                    "candidate_core": [],
                    "candidate_support": [
                        item("Dual Reaver", "support"),
                        item("Fogshroom", "support"),
                        item("Waystones", "support"),
                    ],
                }
            ]
        }
    )
    before = copy.deepcopy(catalog)
    result = apply_proposed_changes(catalog, diff)
    assert result.catalog == before
    assert result.changed is False
    assert any(
        "no core or carry classifications" in s for s in result.skipped
    ), result.skipped


def test_addition_to_early_phase_is_skipped():
    catalog = small_catalog()
    diff = diff_with(
        {
            "archetype_additions": [
                {
                    "tag": "Whatever",
                    "candidate_phase": "early",
                    "candidate_core": [item("X", "carry")],
                    "candidate_support": [],
                }
            ]
        }
    )
    before = copy.deepcopy(catalog)
    result = apply_proposed_changes(catalog, diff)
    assert result.catalog == before
    assert any("early" in s for s in result.skipped)


# --- removals / gates --------------------------------------------------------


def test_support_item_removal_applies_from_exact_archetype_support_items():
    catalog = small_catalog()
    diff = diff_with(
        {"item_removal_candidates": [support_removal("Bagpipes")]}
    )

    result = apply_proposed_changes(catalog, diff)
    axe = result.catalog["game_phases"]["early_mid"]["archetypes"][0]

    assert axe["support_items"] == []
    assert result.changed is True
    assert result.applied == ["'Axe'/'early_mid': removed 'Bagpipes' from support_items"]


def test_support_item_removal_is_idempotent_on_rerun_with_clear_skip():
    catalog = small_catalog()
    diff = diff_with(
        {"item_removal_candidates": [support_removal("Bagpipes")]}
    )

    first = apply_proposed_changes(catalog, diff)
    second = apply_proposed_changes(first.catalog, diff)

    assert first.changed is True
    assert second.changed is False
    assert serialize_catalog(first.catalog) == serialize_catalog(second.catalog)
    assert any("item not found in support_items" in s for s in second.skipped)


@pytest.mark.parametrize(
    ("entry", "expected_reason"),
    [
        (support_removal("Bagpipes", phase="missing"), "phase missing"),
        (support_removal("Bagpipes", archetype="Missing"), "archetype not found"),
        (support_removal("Missing Support"), "item not found in support_items"),
    ],
)
def test_support_item_removal_missing_exact_target_skips(entry, expected_reason):
    catalog = small_catalog()
    before = copy.deepcopy(catalog)
    diff = diff_with({"item_removal_candidates": [entry]})

    result = apply_proposed_changes(catalog, diff)

    assert result.catalog == before
    assert result.changed is False
    assert any(expected_reason in s for s in result.skipped), result.skipped


@pytest.mark.parametrize(
    ("entry", "expected_reason"),
    [
        (
            support_removal(
                "Battle Axe",
                catalog_bucket="carry_items",
                retirement_type="whole_build_review",
                actionability="review_required",
            ),
            "unsupported catalog_bucket 'carry_items'",
        ),
        (
            support_removal(
                "Hidden Lake",
                catalog_bucket="core_items",
                retirement_type="core_item_review",
                actionability="review_required",
            ),
            "unsupported catalog_bucket 'core_items'",
        ),
        (
            support_removal(
                "Chains",
                catalog_bucket="condition_items",
                retirement_type="condition_item_review",
                actionability="review_required",
            ),
            "unsupported catalog_bucket 'condition_items'",
        ),
        (
            support_removal(
                "Crow's Nest",
                phase="early",
                archetype=None,
                catalog_bucket="universal_utility_items",
                retirement_type="support_like_phase_item",
                actionability="review_required",
            ),
            "unsupported catalog_bucket 'universal_utility_items'",
        ),
        (
            support_removal(
                "Investment",
                phase="early",
                archetype=None,
                catalog_bucket="economy_items",
                retirement_type="support_like_phase_item",
                actionability="review_required",
            ),
            "unsupported catalog_bucket 'economy_items'",
        ),
        (
            support_removal(
                "Bagpipes",
                actionability="freeze_blocked",
                blocked_by=["freeze_removals"],
                freeze_blocked=True,
            ),
            "freeze-blocked retirement candidate is review-only",
        ),
        (
            support_removal("Bagpipes", actionability="review_required"),
            "unsupported actionability 'review_required'",
        ),
        (
            support_removal("Bagpipes", retirement_type="catalog_item"),
            "unsupported retirement_type 'catalog_item'",
        ),
        (
            support_removal("Bagpipes", archetype=None),
            "missing exact archetype",
        ),
    ],
)
def test_non_supported_item_removal_candidates_skip_with_explicit_reasons(entry, expected_reason):
    catalog = small_catalog()
    before = copy.deepcopy(catalog)
    diff = diff_with({"item_removal_candidates": [entry]})

    result = apply_proposed_changes(catalog, diff)

    assert result.catalog == before
    assert result.changed is False
    assert any(expected_reason in s for s in result.skipped), result.skipped


def test_archetype_and_phase_level_removal_candidates_skip_with_explicit_reasons():
    catalog = small_catalog()
    diff = diff_with(
        {
            "archetype_removal_candidates": [
                {
                    "phase": "early_mid",
                    "archetype": "Axe",
                    "item": "Battle Axe",
                    "catalog_bucket": "carry_items",
                    "actionability": "review_required",
                },
                {
                    "phase": "early",
                    "archetype": None,
                    "item": "Investment",
                    "catalog_bucket": "economy_items",
                    "actionability": "review_required",
                },
            ],
        }
    )
    before = copy.deepcopy(catalog)

    result = apply_proposed_changes(catalog, diff)

    assert result.catalog == before
    assert result.changed is False
    assert any("archetype_removal_candidate 'Battle Axe'" in s for s in result.skipped)
    assert any("bucket='carry_items'" in s for s in result.skipped)
    assert any("archetype_removal_candidate 'Investment'" in s for s in result.skipped)
    assert any("bucket='economy_items'" in s for s in result.skipped)


def test_semantic_classification_false_is_noop():
    catalog = small_catalog()
    diff = diff_with(
        {
            "archetype_updates": [
                {
                    "phase": "early_mid",
                    "archetype": "Axe",
                    "missing_items": [item("Sawpike", "carry")],
                }
            ],
            "item_removal_candidates": [support_removal("Bagpipes")],
        },
        semantic=False,
    )
    before = copy.deepcopy(catalog)
    result = apply_proposed_changes(catalog, diff)
    assert result.catalog == before
    assert result.changed is False


def test_input_is_never_mutated():
    catalog = small_catalog()
    snapshot = copy.deepcopy(catalog)
    diff = diff_with(
        {
            "archetype_updates": [
                {
                    "phase": "early_mid",
                    "archetype": "Axe",
                    "missing_items": [item("Sawpike", "carry")],
                }
            ],
            "item_removal_candidates": [support_removal("Bagpipes")],
        }
    )
    apply_proposed_changes(catalog, diff)
    assert catalog == snapshot


def test_apply_is_idempotent():
    catalog = small_catalog()
    diff = diff_with(
        {
            "archetype_updates": [
                {
                    "phase": "early_mid",
                    "archetype": "Axe",
                    "missing_items": [item("Sawpike", "carry")],
                }
            ]
        }
    )
    first = apply_proposed_changes(catalog, diff).catalog
    second = apply_proposed_changes(first, diff).catalog
    assert serialize_catalog(first) == serialize_catalog(second)


# --- exact delta (the required assertion) ------------------------------------


def test_known_proposed_changes_produces_exact_catalog_delta():
    catalog = {
        "schema_version": 1,
        "hero": "Karnok",
        "season": 13,
        "last_updated": "2026-05-05",
        "notes": "n",
        "item_tier_list": {"description": "d"},
        "game_phases": {
            "early": {
                "day_range": "Day 1-4",
                "description": "e",
                "universal_utility_items": [],
                "economy_items": [],
            },
            "early_mid": {
                "day_range": "Day 4-7",
                "description": "em",
                "archetypes": [
                    {"name": "Axe", "carry_items": ["Battle Axe"], "support_items": []}
                ],
            },
            "late": {"day_range": "Day 7-13", "description": "l", "archetypes": []},
        },
    }
    diff = diff_with(
        {
            "archetype_updates": [
                {
                    "phase": "early_mid",
                    "archetype": "Axe",
                    "missing_items": [item("Sawpike", "carry")],
                }
            ],
            "archetype_additions": [
                {
                    "tag": "Burn",
                    "candidate_phase": "late",
                    "candidate_core": [item("Burn Scar", "carry")],
                    "candidate_support": [item("Torch", "support")],
                }
            ],
        }
    )

    expected = copy.deepcopy(catalog)
    expected["game_phases"]["early_mid"]["archetypes"][0]["carry_items"] = [
        "Battle Axe",
        "Sawpike",
    ]
    expected["game_phases"]["late"]["archetypes"] = [
        {
            "name": "Burn",
            "phase": "late",
            "carry_items": ["Burn Scar"],
            "support_items": ["Torch"],
        }
    ]

    result = apply_proposed_changes(catalog, diff)
    assert result.catalog == expected
    assert serialize_catalog(result.catalog) == serialize_catalog(expected)


# --- schema validation (fail closed) -----------------------------------------

_TINY_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": ["schema_version", "hero"],
    "properties": {
        "schema_version": {"type": "integer", "minimum": 1},
        "hero": {"type": "string", "minLength": 1},
    },
}


def test_validate_catalog_raises_on_violation():
    with pytest.raises(ApplierError):
        validate_catalog({"schema_version": 1}, _TINY_SCHEMA)  # missing hero


def test_validate_catalog_accepts_valid():
    validate_catalog({"schema_version": 1, "hero": "Karnok"}, _TINY_SCHEMA)


def test_ensure_supported_schema_version_fails_closed():
    with pytest.raises(ApplierError):
        ensure_supported_schema_version({"schema_version": 2})
    with pytest.raises(ApplierError):
        ensure_supported_schema_version({})
    ensure_supported_schema_version({"schema_version": 1})


# --- integration with the real diff generator --------------------------------


def test_applier_consumes_real_generate_diff_output():
    catalog = small_catalog()
    rows = [add_row("Sawpike", phase="early_mid", archetype="Axe", existing=True)]
    classifier = StaticClassifier(
        [ItemClassification("Sawpike", "carry", "high", "reason", "top_line")]
    )
    diff_json = generate_diff(
        "Karnok", evaluation(rows), catalog, classifier, classifier_mode="deterministic"
    )
    assert diff_json["semantic_classification"] is True

    result = apply_proposed_changes(catalog, diff_json)
    axe = result.catalog["game_phases"]["early_mid"]["archetypes"][0]
    assert "Sawpike" in axe["carry_items"]
