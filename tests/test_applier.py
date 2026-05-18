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


# --- additive only / gates ---------------------------------------------------


def test_removal_buckets_are_not_applied():
    catalog = small_catalog()
    diff = diff_with(
        {
            "item_removal_candidates": [
                {"phase": "early_mid", "archetype": "Axe", "item": "Battle Axe"}
            ],
            "archetype_removal_candidates": [
                {"phase": "early_mid", "archetype": "Axe"}
            ],
        }
    )
    before = copy.deepcopy(catalog)
    result = apply_proposed_changes(catalog, diff)
    assert result.catalog == before


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
            ]
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
            ]
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
