#!/usr/bin/env python3
"""Probe Addressables catalog GUIDs for missing Bazaar card art bundles."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable

import app_paths
from extract_bazaar_bundle_pngs import _normalize_card_name, _parse_card_texture_name
from web.card_images import NAME_ALIASES

CATALOG_PATH = Path(
    r"C:\Program Files (x86)\Steam\steamapps\common\The Bazaar"
    r"\TheBazaar_Data\StreamingAssets\aa\catalog.bin"
)
STANDALONE_DIR = Path(
    r"C:\Program Files (x86)\Steam\steamapps\common\The Bazaar"
    r"\TheBazaar_Data\StreamingAssets\aa\StandaloneWindows64"
)
GUID_RE = re.compile(r"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])")
READABLE_RE = re.compile(rb"[ -~]{8,}")
BUNDLE_RE = re.compile(r"[A-Za-z0-9_./\\-]+\.bundle", re.IGNORECASE)


def _load_latest_cards() -> list[dict]:
    cards_path = app_paths.static_cache_dir() / "cards.json"
    data = json.loads(cards_path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        latest = list(data.values())[-1]
    else:
        latest = data
    if not isinstance(latest, list):
        raise ValueError(f"Unexpected cards.json shape in {cards_path}")
    return latest


def _load_manifest_keys() -> set[str]:
    manifest_path = app_paths.image_cache_dir() / "manifest.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    keys = set((data.get("by_card_key") or {}).keys())
    keys.update(alias for alias in NAME_ALIASES if NAME_ALIASES[alias] in keys)
    return keys


def _iter_strings(value) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for nested in value.values():
            yield from _iter_strings(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _iter_strings(nested)


def _card_art_guids(card: dict) -> set[str]:
    guids: set[str] = set()
    for value in _iter_strings(card.get("ArtKey")):
        if GUID_RE.fullmatch(value):
            guids.add(value)
    return guids


def _missing_item_guid_cards() -> dict[str, list[dict]]:
    manifest_keys = _load_manifest_keys()
    by_guid: dict[str, list[dict]] = {}
    for card in _load_latest_cards():
        if card.get("Type") != "Item":
            continue
        internal_name = card.get("InternalName") or ""
        if _normalize_card_name(internal_name) in manifest_keys:
            continue
        for guid in _card_art_guids(card):
            by_guid.setdefault(guid, []).append(card)
    return by_guid


def _readable_strings(context: bytes) -> list[str]:
    strings: list[str] = []
    for raw in READABLE_RE.findall(context):
        try:
            strings.append(raw.decode("ascii", errors="ignore"))
        except UnicodeDecodeError:
            continue
    return strings


def _bundle_candidates(strings: Iterable[str]) -> set[str]:
    candidates: set[str] = set()
    for value in strings:
        for match in BUNDLE_RE.findall(value):
            candidates.add(match.replace("\\", "/").split("/")[-1])
    return candidates


def _bundle_path(name: str) -> Path | None:
    direct = STANDALONE_DIR / name
    if direct.is_file():
        return direct
    matches = list(STANDALONE_DIR.rglob(name))
    if matches:
        return matches[0]
    return None


def _inspect_bundle(path: Path) -> tuple[int, list[str]]:
    try:
        import UnityPy
    except ImportError:
        return 0, ["UnityPy not installed"]

    try:
        env = UnityPy.load(str(path))
    except Exception as exc:
        return 0, [f"UnityPy load failed: {exc}"]

    names: list[str] = []
    for obj in env.objects:
        try:
            if obj.type.name != "Texture2D":
                continue
            data = obj.read()
            tex_name = getattr(data, "m_Name", None) or getattr(data, "name", None) or ""
            if _parse_card_texture_name(tex_name):
                names.append(tex_name)
        except Exception:
            continue
    return len(names), names[:10]


def main() -> int:
    if not CATALOG_PATH.is_file():
        print(f"catalog.bin not found: {CATALOG_PATH}")
        return 2

    by_guid = _missing_item_guid_cards()
    wanted_guids = set(by_guid)
    print(f"Missing Item cards with GUID ArtKeys: {sum(len(v) for v in by_guid.values())}")
    print(f"Unique missing GUID ArtKeys: {len(wanted_guids)}")

    catalog = CATALOG_PATH.read_bytes()
    catalog_guid_offsets: dict[str, list[int]] = {}
    for match in GUID_RE.finditer(catalog.decode("latin1")):
        guid = match.group(0)
        if guid in wanted_guids:
            catalog_guid_offsets.setdefault(guid, []).append(match.start())

    print(f"GUIDs found in catalog.bin: {len(catalog_guid_offsets)}/{len(wanted_guids)}")

    adjacent_bundles: dict[str, set[str]] = {}
    for guid in sorted(catalog_guid_offsets):
        card_names = sorted(
            str(card.get("InternalName") or card.get("Name") or card.get("Id") or "?")
            for card in by_guid[guid]
        )
        print()
        print(f"GUID {guid} ({len(by_guid[guid])} card(s)): {', '.join(card_names[:5])}")
        for offset in catalog_guid_offsets[guid][:5]:
            start = max(0, offset - 200)
            end = min(len(catalog), offset + 32 + 200)
            strings = _readable_strings(catalog[start:end])
            bundles = _bundle_candidates(strings)
            if bundles:
                adjacent_bundles.setdefault(guid, set()).update(bundles)
            print(f"  offset {offset}:")
            for value in strings:
                print(f"    {value}")

    all_bundles = sorted({name for names in adjacent_bundles.values() for name in names})
    print()
    print(f"Adjacent bundle name candidates: {len(all_bundles)}")
    for name in all_bundles:
        path = _bundle_path(name)
        if path is None:
            print(f"  {name}: not found under {STANDALONE_DIR}")
            continue
        count, examples = _inspect_bundle(path)
        print(f"  {name}: found, card textures={count}")
        for example in examples:
            print(f"    {example}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
