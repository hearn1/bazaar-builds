import json
import sqlite3
from pathlib import Path

from automated_builds_pipeline.hero_items import load_hero_item_ownership


def write_catalog(tracker: Path, hero: str, items: list[str]) -> None:
    builds_dir = tracker / "builds"
    builds_dir.mkdir(exist_ok=True)
    payload = {
        "schema_version": 1,
        "hero": hero,
        "item_tier_list": {"high": items},
        "game_phases": {},
    }
    (builds_dir / f"{hero.casefold()}_builds.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def test_catalog_fallback_blocks_items_owned_by_another_hero(tmp_path):
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    write_catalog(tracker, "Karnok", ["Messenger Sparrow"])
    write_catalog(tracker, "Stelle", [])

    ownership = load_hero_item_ownership(tracker)

    assert ownership.allows("Karnok", "Messenger Sparrow") is True
    assert ownership.allows("Stelle", "Messenger Sparrow") is False
    assert ownership.allows("Stelle", "Launch Tower") is True


def test_explicit_ownership_sidecar_takes_precedence_over_catalog_fallback(tmp_path):
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    write_catalog(tracker, "Karnok", [])
    write_catalog(tracker, "Stelle", ["Messenger Sparrow"])
    (tracker / "hero_item_ownership.json").write_text(
        json.dumps({"items": {"Messenger Sparrow": ["Karnok"]}}),
        encoding="utf-8",
    )

    ownership = load_hero_item_ownership(tracker)

    assert ownership.owners_for("Messenger Sparrow") == frozenset({"karnok"})
    assert ownership.allows("Stelle", "Messenger Sparrow") is False


def test_static_card_cache_tags_can_supply_item_owner(tmp_path):
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    write_catalog(tracker, "Karnok", [])
    write_catalog(tracker, "Stelle", [])
    static_cache = tracker / "static_cache"
    static_cache.mkdir()
    (static_cache / "cards.json").write_text(
        json.dumps(
            {"cards": [{"Name": "Messenger Sparrow", "Tags": ["Karnok", "Item"]}]}
        ),
        encoding="utf-8",
    )

    ownership = load_hero_item_ownership(tracker)

    assert ownership.owners_for("Messenger Sparrow") == frozenset({"karnok"})


def test_sqlite_card_cache_tags_can_supply_item_owner(tmp_path):
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    write_catalog(tracker, "Karnok", [])
    write_catalog(tracker, "Stelle", [])
    connection = sqlite3.connect(tracker / "bazaar_runs.db")
    connection.execute("CREATE TABLE card_cache (name TEXT, tags TEXT, raw_json TEXT)")
    connection.execute(
        "INSERT INTO card_cache (name, tags, raw_json) VALUES (?, ?, ?)",
        (
            "Messenger Sparrow",
            json.dumps(["Karnok", "Item"]),
            json.dumps({"Name": "Messenger Sparrow"}),
        ),
    )
    connection.commit()
    connection.close()

    ownership = load_hero_item_ownership(tracker)

    assert ownership.owners_for("Messenger Sparrow") == frozenset({"karnok"})
