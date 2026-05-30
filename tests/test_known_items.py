import json
from pathlib import Path

import bazaar_build_enricher as enricher
from automated_builds_pipeline.known_items import catalog_item_names, load_known_items_file
from automated_builds_pipeline.deterministic_classifier import DeterministicClassifier


def test_json_array_names_file_with_stray_db_line(tmp_path: Path) -> None:
    path = tmp_path / "card_cache_names.txt"
    path.write_text(
        "[DB] Initialized at C:\\Users\\Matt\\Desktop\\bazaar_tracker\\bazaar_runs.db\n"
        "[\n"
        '  " Wish for Immortality (2nd Wish)",\n'
        '  "Atlas Stone",\n'
        '  "Fogshroom",\n'
        '  "Space Laser"\n'
        "]\n",
        encoding="utf-8",
    )

    items = load_known_items_file(path)

    assert "Atlas Stone" in items
    assert "Fogshroom" in items
    assert "Space Laser" in items
    assert "Wish for Immortality (2nd Wish)" in items  # leading space stripped
    assert "[" not in items
    assert "]" not in items
    assert not any(name.startswith("[DB]") for name in items)


def test_plaintext_names_file_still_supported(tmp_path: Path) -> None:
    path = tmp_path / "card_cache_names.txt"
    path.write_text("Pufferfish\nYo-Yo\n", encoding="utf-8")

    assert load_known_items_file(path) == {"Pufferfish", "Yo-Yo"}


def test_missing_names_file_is_empty(tmp_path: Path) -> None:
    assert load_known_items_file(tmp_path / "nope.txt") == set()
    assert load_known_items_file(None) == set()


def test_catalog_item_names_includes_item_tier_list() -> None:
    catalog = {
        "item_tier_list": {"S": ["Flying Squirrel"], "B": ["Fogshroom"], "D": ["Waystones", "Dual Reaver"]},
        "game_phases": {
            "early": {
                "universal_utility_items": ["Waterskin"],
                "archetypes": [{"name": "Axe", "carry_items": ["Anaconda"]}],
            }
        },
    }

    names = catalog_item_names(catalog)

    assert {"Flying Squirrel", "Fogshroom", "Waystones", "Dual Reaver", "Waterskin", "Anaconda"} <= names


def test_classifier_accepts_real_items_from_json_names_file(tmp_path: Path) -> None:
    path = tmp_path / "card_cache_names.txt"
    path.write_text('[\n  "Atlas Stone",\n  "Money Furnace"\n]\n', encoding="utf-8")

    classifier = DeterministicClassifier(known_items_path=path)
    result = classifier.classify_archetype(
        "Karnok",
        "early",
        "Anaconda Build",
        {},
        [{"item": "Atlas Stone", "classification_ceiling": "carry_core_support"}],
        {},
        None,
    )

    assert result[0].classification != "invalid"


# --- enricher.load_known_items tests (Fixes #108) ---

_SAMPLE_CATALOG = {
    "schema_version": 1,
    "hero": "Karnok",
    "season": 1,
    "last_updated": "2026-01-01",
    "notes": "",
    "item_tier_list": {"S": ["Anaconda"], "A": ["Pufferfish"]},
    "game_phases": {
        "early": {"universal_utility_items": ["Waterskin"], "economy_items": []},
    },
}

_DB_PREAMBLE = (
    "[DB] Initialized at C:\\Users\\Matt\\Desktop\\bazaar_tracker\\bazaar_runs.db\n"
    '["Atlas Stone", "Fogshroom"]\n'
)


def test_load_known_items_finds_catalogs_at_root(tmp_path: Path) -> None:
    (tmp_path / "karnok_builds.json").write_text(json.dumps(_SAMPLE_CATALOG), encoding="utf-8")
    (tmp_path / "card_cache_names.txt").write_text("", encoding="utf-8")

    items = enricher.load_known_items(tmp_path, tmp_path / "card_cache_names.txt")

    assert "Anaconda" in items
    assert "Pufferfish" in items


def test_load_known_items_finds_catalogs_in_builds_subdir(tmp_path: Path) -> None:
    builds = tmp_path / "builds"
    builds.mkdir()
    (builds / "karnok_builds.json").write_text(json.dumps(_SAMPLE_CATALOG), encoding="utf-8")
    (tmp_path / "card_cache_names.txt").write_text("", encoding="utf-8")

    items = enricher.load_known_items(tmp_path, tmp_path / "card_cache_names.txt")

    assert "Anaconda" in items
    assert "Pufferfish" in items


def test_load_known_items_names_file_with_db_preamble(tmp_path: Path) -> None:
    (tmp_path / "card_cache_names.txt").write_text(_DB_PREAMBLE, encoding="utf-8")

    items = enricher.load_known_items(tmp_path, tmp_path / "card_cache_names.txt")

    assert "Atlas Stone" in items
    assert "Fogshroom" in items


def test_load_known_items_names_fallback_alone_despite_db_preamble(tmp_path: Path) -> None:
    """Names file is a real fallback even when no catalogs exist and [DB] preamble is present."""
    (tmp_path / "card_cache_names.txt").write_text(_DB_PREAMBLE, encoding="utf-8")

    items = enricher.load_known_items(tmp_path, tmp_path / "card_cache_names.txt")

    assert len(items) > 0
    assert "Atlas Stone" in items
