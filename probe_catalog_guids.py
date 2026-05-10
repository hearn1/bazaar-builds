#!/usr/bin/env python3
"""Probe Addressables catalog GUIDs for missing Bazaar card art bundles.

Use --cards-file and --manifest-file to point at coach cache files.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Iterable

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
CARD_NAME_RE = re.compile(
    r"^CF_[A-Z]+_[A-Za-z]{2,5}_(.+?)(?:_D\d?|_D\s+|)\s*$"
)
CARD_D_SUFFIX_RE = re.compile(r"_D(?:\d|\s*)\s*$")

SIBLING_TRACKER_DIR = Path(__file__).resolve().parent.parent / "bazaar_coach"
DEFAULT_CARDS_FILE = SIBLING_TRACKER_DIR / "static_cache" / "cards.json"
DEFAULT_MANIFEST_FILE = SIBLING_TRACKER_DIR / "static_cache" / "images" / "manifest.json"

# Synced from bazaar_coach/web/card_images.py; re-sync if the coach's alias list updates.
NAME_ALIASES: dict[str, str] = {
    # Plural / singular mismatches
    "bagpipes": "bagpipe",
    "busybee": "busybees",
    "cinders": "cinder",
    "fang": "fangs",
    "golfclubs": "golfclub",
    "nanobot": "nanobots",
    "schematics": "schematic",
    "strawberries": "strawberry",
    # Typos / misspellings in Unity asset folder names
    "ballista": "balista",
    "beasttooth": "beaststooth",
    "businesscard": "buisnesscard",
    "colander": "collander",
    "inertialdampener": "inertiadampener",
    "jabaliandagger": "jaballiandagger",
    "jabaliandrum": "jaballiandrum",
    "ouroborosstatue": "ouroborusstatue",
    "pillbuggy": "pilbuggy",
    "sapphire": "saphire",
    # "Sat-Comm" -> "satcomm" (dash stripped); asset has double-t
    "satcomm": "sattcomm",
    # Cyrillic C in asset name strips away, leaving "seafoodracker"
    "seafoodcracker": "seafoodracker",
    # Cyrillic C at the start of "Cleaver" strips away in the asset name
    "cleaver": "leaver",
    # Game renamed these items after the Unity assets were built
    "bluenanas": "bluebananas",
    "dooltron": "dootron",
    "dooltronmainframe": "dootronmainframe",
    "dragontooth": "dragonstooth",
    "frozenflame": "frozenfire",
    "harkuvianlauncher": "hakurvanlauncher",
    "runicblade": "runeblade",
    "tommoogun": "tommygun",
    "trollosaur": "trollolor",
    "weaselpede": "iceweaselpede",
    # Word-form differences
    "banuleaves": "banuleaf",
    # "Mortar & Pestle" -> "mortarpestle"; asset spells out "and"
    "mortarpestle": "mortarandpestle",
    "recyclingbin": "recyclebin",
}


def _normalize_card_name(value: str) -> str:
    """Lowercase + strip non-alphanumerics. Used to build manifest keys."""
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _parse_card_texture_name(name: str) -> str | None:
    """Return the card folder name if ``name`` looks like card art, else None."""
    if not name:
        return None
    match = CARD_NAME_RE.match(name)
    if match is None:
        return None
    card_folder = match.group(1)
    if not _card_texture_has_d_suffix(name):
        card_folder = re.sub(r"\d+$", "", card_folder)
    return card_folder or None


def _card_texture_has_d_suffix(name: str) -> bool:
    return bool(CARD_D_SUFFIX_RE.search(name or ""))


def _load_latest_cards(cards_file: Path) -> list[dict]:
    data = json.loads(cards_file.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        latest = list(data.values())[-1]
    else:
        latest = data
    if not isinstance(latest, list):
        raise ValueError(f"Unexpected cards.json shape in {cards_file}")
    return latest


def _load_manifest_keys(manifest_file: Path) -> set[str]:
    data = json.loads(manifest_file.read_text(encoding="utf-8"))
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


def _missing_item_guid_cards(cards_file: Path, manifest_file: Path) -> dict[str, list[dict]]:
    manifest_keys = _load_manifest_keys(manifest_file)
    by_guid: dict[str, list[dict]] = {}
    for card in _load_latest_cards(cards_file):
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


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe Addressables catalog GUIDs for missing Bazaar card art bundles.",
    )
    parser.add_argument(
        "--cards-file",
        type=Path,
        default=DEFAULT_CARDS_FILE,
        help=f"Path to coach static_cache/cards.json (default: {DEFAULT_CARDS_FILE})",
    )
    parser.add_argument(
        "--manifest-file",
        type=Path,
        default=DEFAULT_MANIFEST_FILE,
        help=f"Path to coach image manifest.json (default: {DEFAULT_MANIFEST_FILE})",
    )
    return parser.parse_args()


def _resolve_input_path(path: Path) -> Path:
    return path.expanduser().resolve()


def main() -> int:
    args = _parse_args()
    cards_file = _resolve_input_path(args.cards_file)
    manifest_file = _resolve_input_path(args.manifest_file)

    if not CATALOG_PATH.is_file():
        print(f"catalog.bin not found: {CATALOG_PATH}")
        return 2

    by_guid = _missing_item_guid_cards(cards_file, manifest_file)
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
