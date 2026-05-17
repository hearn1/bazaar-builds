from pathlib import Path

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
