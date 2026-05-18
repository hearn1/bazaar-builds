"""Shared known-items loading for deterministic classifiers.

The coach/tracker repo writes ``card_cache_names.txt`` as a JSON array, often
preceded by a stray stdout line such as ``[DB] Initialized at ...``. Parsing it
line-by-line leaves JSON punctuation (``"Atlas Stone",``, ``[``) in the set, so
every real item fails the ``item in known_items`` gate. Older fixtures and
hand-written lists are plain newline-delimited, so both shapes are supported.

Catalog names must include ``item_tier_list``: that is where per-hero catalogs
keep their full item universe, and ``diff._catalog_index`` deliberately ignores
it (it only drives archetype bucketing).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional


def load_known_items_file(path: Optional[Path]) -> set[str]:
    if path is None or not path.exists():
        return set()
    raw = path.read_text(encoding="utf-8", errors="ignore")
    parsed = _parse_json_array(raw)
    if parsed is not None:
        return {
            s.strip()
            for s in parsed
            if isinstance(s, str) and s.strip() and not s.strip().startswith("[")
        }
    return {
        ln.strip()
        for ln in raw.splitlines()
        if ln.strip() and not ln.strip().startswith("[")
    }


def _parse_json_array(raw: str) -> Optional[list]:
    # The coach writes the array preceded by a stray stdout line that itself
    # contains a "[" (e.g. "[DB] Initialized ..."), so the first "[" is not the
    # array opener. Walk every "[" until the remainder parses as a JSON list.
    idx = raw.find("[")
    while idx != -1:
        try:
            parsed = json.loads(raw[idx:])
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            return parsed
        idx = raw.find("[", idx + 1)
    return None


def catalog_item_names(catalog: Any) -> set[str]:
    names: set[str] = set()
    if not isinstance(catalog, dict):
        return names

    tier_list = catalog.get("item_tier_list")
    if isinstance(tier_list, dict):
        for value in tier_list.values():
            if isinstance(value, list):
                names.update(str(i) for i in value if i)

    game_phases = catalog.get("game_phases")
    if isinstance(game_phases, dict):
        for phase in game_phases.values():
            if not isinstance(phase, dict):
                continue
            for key in ("universal_utility_items", "economy_items"):
                value = phase.get(key, [])
                if isinstance(value, list):
                    names.update(str(i) for i in value if i)
            for arch in phase.get("archetypes", []) or []:
                if not isinstance(arch, dict):
                    continue
                for key in ("carry_items", "core_items", "support_items", "condition_items"):
                    value = arch.get(key, [])
                    if isinstance(value, list):
                        names.update(
                            str(i) for i in value if i and not str(i).startswith("TODO")
                        )

    raw_items = catalog.get("items", [])
    if isinstance(raw_items, list):
        for item in raw_items:
            if isinstance(item, dict) and isinstance(item.get("item"), str):
                names.add(item["item"])
    for entry in catalog.get("archetypes", []) or []:
        if not isinstance(entry, dict):
            continue
        for key in ("carry_items", "core_items", "support_items", "condition_items"):
            value = entry.get(key, [])
            if isinstance(value, list):
                names.update(str(i) for i in value if i)

    return names
