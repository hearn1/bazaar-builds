"""Hero item ownership helpers for source evidence filtering.

The coach repo's ``card_cache_names.txt`` is a global validity list.  It can say
"Messenger Sparrow is a real item", but not "Messenger Sparrow belongs to
Karnok".  This module loads a best-effort ownership index so global sources
like BazaarDB cannot seed another hero with known hero-owned items.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from automated_builds_pipeline.known_items import catalog_item_names


@dataclass(frozen=True)
class HeroItemOwnership:
    cache_item_owners: dict[str, frozenset[str]] = field(default_factory=dict)
    catalog_item_owners: dict[str, frozenset[str]] = field(default_factory=dict)

    def owners_for(self, item: str) -> frozenset[str]:
        key = _item_key(item)
        return self.cache_item_owners.get(key) or self.catalog_item_owners.get(key) or frozenset()

    def allows(self, hero: str, item: str) -> bool:
        owners = self.owners_for(item)
        return not owners or _hero_key(hero) in owners


def load_hero_item_ownership(tracker_repo: Path) -> HeroItemOwnership:
    heroes = _known_heroes_from_catalogs(tracker_repo)
    cache_owners: dict[str, set[str]] = {}
    catalog_owners = _catalog_item_owners(tracker_repo)

    _merge_owners(cache_owners, _explicit_sidecar_owners(tracker_repo))
    _merge_owners(cache_owners, _sqlite_card_cache_owners(tracker_repo, heroes))
    for path in _static_card_cache_paths(tracker_repo):
        _merge_owners(cache_owners, _static_cache_owners(path, heroes))

    return HeroItemOwnership(
        cache_item_owners=_freeze_owners(cache_owners),
        catalog_item_owners=_freeze_owners(catalog_owners),
    )


def _catalog_item_owners(tracker_repo: Path) -> dict[str, set[str]]:
    owners: dict[str, set[str]] = {}
    for path in _catalog_paths(tracker_repo):
        try:
            catalog = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(catalog, dict):
            continue
        hero = _hero_key(catalog.get("hero") or _hero_from_catalog_filename(path))
        if not hero:
            continue
        for item in catalog_item_names(catalog):
            _add_owner(owners, item, hero)
    return owners


def _explicit_sidecar_owners(tracker_repo: Path) -> dict[str, set[str]]:
    owners: dict[str, set[str]] = {}
    for path in (
        tracker_repo / "card_cache_heroes.json",
        tracker_repo / "hero_item_ownership.json",
        tracker_repo / "static_cache" / "hero_item_ownership.json",
    ):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        heroes = data.get("heroes")
        if isinstance(heroes, dict):
            for hero, items in heroes.items():
                if isinstance(items, list):
                    for item in items:
                        if isinstance(item, str):
                            _add_owner(owners, item, _hero_key(hero))
        items = data.get("items")
        if isinstance(items, dict):
            for item, item_heroes in items.items():
                if isinstance(item, str) and isinstance(item_heroes, list):
                    for hero in item_heroes:
                        if isinstance(hero, str):
                            _add_owner(owners, item, _hero_key(hero))
    return owners


def _sqlite_card_cache_owners(tracker_repo: Path, known_heroes: set[str]) -> dict[str, set[str]]:
    owners: dict[str, set[str]] = {}
    for path in _sqlite_card_cache_paths(tracker_repo):
        if not path.exists():
            continue
        try:
            connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        try:
            rows = connection.execute("SELECT name, tags, raw_json FROM card_cache")
            for name, tags, raw_json in rows:
                _merge_sqlite_card_row(owners, name, tags, raw_json, known_heroes)
        except sqlite3.Error:
            continue
        finally:
            connection.close()
    return owners


def _merge_sqlite_card_row(
    owners: dict[str, set[str]],
    name: Any,
    tags: Any,
    raw_json: Any,
    known_heroes: set[str],
) -> None:
    raw_card = _json_value(raw_json)
    card_name = _card_name(raw_card) if isinstance(raw_card, dict) else ""
    card_name = card_name or _clean(name)
    if not card_name:
        return

    heroes: set[str] = set()
    if isinstance(raw_card, dict):
        heroes.update(_card_heroes(raw_card, known_heroes))
    heroes.update(_hero_keys_from_value(_json_value(tags), known_heroes))
    for hero in heroes:
        _add_owner(owners, card_name, hero)


def _static_cache_owners(path: Path, known_heroes: set[str]) -> dict[str, set[str]]:
    owners: dict[str, set[str]] = {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return owners
    for card in _extract_cards(data):
        if not isinstance(card, dict):
            continue
        name = _card_name(card)
        if not name:
            continue
        card_heroes = _card_heroes(card, known_heroes)
        for hero in card_heroes:
            _add_owner(owners, name, hero)
    return owners


def _extract_cards(data: Any) -> list[Any]:
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in ("cards", "items", "data"):
        value = data.get(key)
        if isinstance(value, list):
            return value
    versioned_lists = [
        value
        for key, value in data.items()
        if isinstance(key, str) and key[:1].isdigit() and isinstance(value, list)
    ]
    if versioned_lists:
        return versioned_lists[-1]
    first = next(iter(data.values()), None)
    if isinstance(first, list):
        return first
    return []


def _card_name(card: dict[str, Any]) -> str:
    title = card.get("Localization", {}).get("Title", {}).get("Text") if isinstance(card.get("Localization"), dict) else None
    return _clean(
        title
        or card.get("InternalName")
        or card.get("internalName")
        or card.get("Name")
        or card.get("name")
    )


def _card_heroes(card: dict[str, Any], known_heroes: set[str]) -> set[str]:
    heroes: set[str] = set()
    for key in (
        "Hero",
        "Heroes",
        "hero",
        "heroes",
        "HeroTags",
        "heroTags",
        "PlayableHeroes",
        "AllowedHeroes",
        "RequiredHero",
        "StartingHero",
        "Tags",
        "tags",
    ):
        heroes.update(_hero_keys_from_value(card.get(key), known_heroes))
    return heroes


def _hero_keys_from_value(value: Any, known_heroes: set[str]) -> set[str]:
    if isinstance(value, str):
        normalized = _hero_key(value)
        if normalized in known_heroes:
            return {normalized}
        tokens = set(filter(None, re.split(r"[^a-z0-9]+", value.casefold())))
        return {hero for hero in known_heroes if hero in tokens}
    if isinstance(value, dict):
        result: set[str] = set()
        for child in value.values():
            result.update(_hero_keys_from_value(child, known_heroes))
        return result
    if isinstance(value, list):
        result: set[str] = set()
        for child in value:
            result.update(_hero_keys_from_value(child, known_heroes))
        return result
    return set()


def _static_card_cache_paths(tracker_repo: Path) -> list[Path]:
    return [
        tracker_repo / "static_cache" / "cards.json",
        tracker_repo / "cards.json",
    ]


def _sqlite_card_cache_paths(tracker_repo: Path) -> list[Path]:
    return [
        tracker_repo / "bazaar_runs.db",
        tracker_repo / "card_cache.db",
    ]


def _known_heroes_from_catalogs(tracker_repo: Path) -> set[str]:
    heroes = set()
    for path in _catalog_paths(tracker_repo):
        try:
            catalog = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            catalog = {}
        if isinstance(catalog, dict) and catalog.get("hero"):
            heroes.add(_hero_key(catalog["hero"]))
        else:
            hero = _hero_from_catalog_filename(path)
            if hero:
                heroes.add(_hero_key(hero))
    return heroes


def _catalog_paths(tracker_repo: Path) -> list[Path]:
    paths = [
        *tracker_repo.glob("*_builds.json"),
        *(tracker_repo / "builds").glob("*_builds.json"),
    ]
    seen: set[str] = set()
    result: list[Path] = []
    for path in paths:
        key = str(path.resolve()).casefold()
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def _hero_from_catalog_filename(path: Path) -> str:
    return re.sub(r"_builds$", "", path.stem, flags=re.IGNORECASE)


def _merge_owners(target: dict[str, set[str]], source: dict[str, set[str]]) -> None:
    for item, heroes in source.items():
        target.setdefault(item, set()).update(heroes)


def _add_owner(owners: dict[str, set[str]], item: str, hero: str) -> None:
    item_key = _item_key(item)
    hero_key = _hero_key(hero)
    if item_key and hero_key:
        owners.setdefault(item_key, set()).add(hero_key)


def _freeze_owners(owners: dict[str, set[str]]) -> dict[str, frozenset[str]]:
    return {item: frozenset(sorted(heroes)) for item, heroes in owners.items() if heroes}


def _item_key(item: Any) -> str:
    return _clean(item).casefold()


def _hero_key(hero: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(hero or "").casefold()).strip("_")


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _json_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value
