"""Parametrized golden tests for evaluate_hero → generate_diff round-trips.

Each sub-directory of tests/fixtures/diff/ is a self-contained case:

  sources/          one JSON file per source artifact
  catalog.json      trimmed hero catalog (game_phases shape)
  names.txt         known-items list (newline-delimited)
  expected_proposed_changes.json  the only output key asserted

Volatile fields (generated_at, window_id, observed_at, etc.) are intentionally
excluded from the assertion — only proposed_changes is compared.

Cases marked with a ``xfail`` marker in XFAIL_CASES will be expected to fail
until the relevant feature branch lands (see inline comments).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from automated_builds_pipeline.deterministic_classifier import DeterministicClassifier
from automated_builds_pipeline.diff import _all_catalog_names, generate_diff
from automated_builds_pipeline.evaluator import (
    evaluate_hero,
    load_catalog_items,
    load_source_artifacts,
)
from automated_builds_pipeline.stats import HeroStats

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "diff"

# Cases expected to fail until their corresponding feature branch lands.
# key: fixture directory name
# value: reason string surfaced in the xfail marker
XFAIL_CASES: dict[str, str] = {}


def _fixture_cases() -> list[Path]:
    if not FIXTURES_DIR.exists():
        return []
    return sorted(p for p in FIXTURES_DIR.iterdir() if p.is_dir())


def _stable_sort_key(entry: dict[str, Any]) -> str:
    """Stable sort key using the identifying fields present in expected entries."""
    return json.dumps(
        [
            entry.get("tag") or entry.get("archetype") or "",
            entry.get("candidate_phase") or entry.get("phase") or "",
            entry.get("item") or "",
        ],
        default=str,
    )


def _partial_match(actual: list[dict[str, Any]], expected: list[dict[str, Any]]) -> bool:
    """Check that every key present in each expected entry matches the actual entry.

    This allows the expected JSON to omit volatile fields (e.g. evidence_refs,
    windows_seen, threshold_reason) while still pinning the fields we care about.
    Comparison is order-independent: both lists are sorted by a stable key derived
    from the expected entries' identifying fields (tag/archetype, phase).
    """
    if len(actual) != len(expected):
        return False

    actual_sorted = sorted(actual, key=_stable_sort_key)
    expected_sorted = sorted(expected, key=_stable_sort_key)

    for act, exp in zip(actual_sorted, expected_sorted):
        for key, value in exp.items():
            if key in ("candidate_core", "candidate_support", "missing_items"):
                # Recurse for nested item lists — compare item names and llm_classification
                act_items = [i.get("item") for i in act.get(key, [])]
                exp_items = [i.get("item") for i in value]
                if sorted(act_items) != sorted(exp_items):
                    return False
                act_by_item = {i.get("item"): i for i in act.get(key, [])}
                for exp_item in value:
                    exp_name = exp_item.get("item")
                    if "llm_classification" in exp_item:
                        act_cls = act_by_item.get(exp_name, {}).get("llm_classification")
                        if act_cls != exp_item["llm_classification"]:
                            return False
            elif act.get(key) != value:
                return False
    return True


def _assert_proposed_changes(
    actual: dict[str, Any], expected: dict[str, Any], fixture_name: str
) -> None:
    for top_key in (
        "archetype_additions",
        "archetype_updates",
        "archetype_removal_candidates",
        "item_removal_candidates",
        "archetype_reshuffles",
    ):
        act_list = actual.get(top_key, [])
        exp_list = expected.get(top_key, [])
        assert len(act_list) == len(exp_list), (
            f"{fixture_name}: proposed_changes.{top_key} length mismatch: "
            f"got {len(act_list)}, expected {len(exp_list)}\n"
            f"actual={json.dumps(act_list, indent=2)}"
        )
        if exp_list:
            assert _partial_match(act_list, exp_list), (
                f"{fixture_name}: proposed_changes.{top_key} content mismatch\n"
                f"actual={json.dumps(act_list, indent=2, default=str)}\n"
                f"expected={json.dumps(exp_list, indent=2, default=str)}"
            )


def _run_fixture(fixture: Path) -> None:
    hero = _infer_hero(fixture)
    sources_dir = fixture / "sources"
    catalog_path = fixture / "catalog.json"
    names_path = fixture / "names.txt"
    expected_path = fixture / "expected_proposed_changes.json"

    source_results = load_source_artifacts(sources_dir)
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog_items = load_catalog_items(catalog_path)
    stats = HeroStats(hero=hero)

    evaluation = evaluate_hero(hero, catalog_items, stats, source_results)

    classifier = DeterministicClassifier(known_items_path=names_path)
    classifier.known_items.update(_all_catalog_names(catalog))

    diff = generate_diff(
        hero,
        evaluation,
        catalog,
        classifier,
        classifier_mode="deterministic",
    )

    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    _assert_proposed_changes(diff["proposed_changes"], expected, fixture.name)


def _infer_hero(fixture: Path) -> str:
    """Derive hero name from catalog.json if present, otherwise from fixture name."""
    catalog_path = fixture / "catalog.json"
    if catalog_path.exists():
        try:
            data = json.loads(catalog_path.read_text(encoding="utf-8"))
            hero = data.get("hero")
            if isinstance(hero, str) and hero:
                return hero
        except Exception:
            pass
    # Fallback: capitalise first segment of fixture name
    return fixture.name.split("_")[0].capitalize()


# Build parametrize list with xfail markers applied inline
_FIXTURE_CASES = _fixture_cases()
_PARAMETRIZE_ARGS = []
for _fixture in _FIXTURE_CASES:
    _name = _fixture.name
    if _name in XFAIL_CASES:
        _PARAMETRIZE_ARGS.append(
            pytest.param(
                _fixture,
                id=_name,
                marks=pytest.mark.xfail(
                    strict=True,
                    reason=XFAIL_CASES[_name],
                ),
            )
        )
    else:
        _PARAMETRIZE_ARGS.append(pytest.param(_fixture, id=_name))


@pytest.mark.parametrize("fixture", _PARAMETRIZE_ARGS)
def test_diff_fixture(fixture: Path) -> None:
    """Golden-gate test: load fixture sources + catalog, run evaluate_hero →
    generate_diff with the deterministic classifier, and assert proposed_changes
    matches the expected JSON (partial key matching — volatile fields ignored).
    """
    _run_fixture(fixture)


def test_mobalytics_editorial_support_lands_in_weaker_signals() -> None:
    """Complementary assertion: the mobalytics editorial support item must land in
    weaker_signals (not proposed_changes) when classification_ceiling is support_only.
    """
    fixture = FIXTURES_DIR / "mobalytics_editorial_support"
    if not fixture.exists():
        pytest.skip("fixture not found")

    hero = _infer_hero(fixture)
    source_results = load_source_artifacts(fixture / "sources")
    catalog = json.loads((fixture / "catalog.json").read_text(encoding="utf-8"))
    catalog_items = load_catalog_items(fixture / "catalog.json")
    stats = HeroStats(hero=hero)
    evaluation = evaluate_hero(hero, catalog_items, stats, source_results)
    classifier = DeterministicClassifier(known_items_path=fixture / "names.txt")
    classifier.known_items.update(_all_catalog_names(catalog))
    diff = generate_diff(hero, evaluation, catalog, classifier, classifier_mode="deterministic")

    weaker = diff.get("weaker_signals", [])
    assert any(w.get("item") == "Mana Vial" for w in weaker), (
        f"Expected 'Mana Vial' in weaker_signals but got: {weaker}"
    )


def test_bazaardb_new_core_addition_has_candidate_core() -> None:
    """Complementary assertion: the bazaardb core-section items must be classified
    as 'core' (not 'support') — guards the CORE ITEMS routing path.

    After the #131 cluster-routing fix, these items are routed to the existing
    Axe Warrior archetype (cross-phase match) rather than creating a new addition.
    """
    fixture = FIXTURES_DIR / "bazaardb_new_core_addition"
    if not fixture.exists():
        pytest.skip("fixture not found")

    hero = _infer_hero(fixture)
    source_results = load_source_artifacts(fixture / "sources")
    catalog = json.loads((fixture / "catalog.json").read_text(encoding="utf-8"))
    catalog_items = load_catalog_items(fixture / "catalog.json")
    stats = HeroStats(hero=hero)
    evaluation = evaluate_hero(hero, catalog_items, stats, source_results)
    classifier = DeterministicClassifier(known_items_path=fixture / "names.txt")
    classifier.known_items.update(_all_catalog_names(catalog))
    diff = generate_diff(hero, evaluation, catalog, classifier, classifier_mode="deterministic")

    updates = diff["proposed_changes"]["archetype_updates"]
    assert len(updates) == 1, f"Expected 1 archetype_update, got {len(updates)}"
    update = updates[0]
    assert update["archetype"] == "Axe Warrior"
    core_items = [i["item"] for i in update["missing_items"] if i.get("llm_classification") == "core"]
    assert "Thunder Totem" in core_items, "Thunder Totem must be classified as core"
    assert "Lightning Rod" in core_items, "Lightning Rod must be classified as core"
    assert diff["proposed_changes"]["archetype_additions"] == []
