"""Deterministically apply proposed_changes to a player-facing <hero>_builds.json.

Additive only: archetype_additions + archetype_updates. Removal and reshuffle
buckets are surfaced to the curator via proposal evidence, never auto-applied.
"""

from __future__ import annotations

import argparse
import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema

SUPPORTED_SCHEMA_VERSION = 1

# Canonical archetype key order. Used only when emitting a new archetype object
# or inserting a previously-absent item bucket into an existing archetype, so
# the change lands in a deterministic, reviewable position. Unknown keys are
# preserved after these, in their original insertion order.
_ARCHETYPE_KEY_ORDER = (
    "name",
    "phase",
    "timing_profile",
    "condition",
    "condition_items",
    "core_items",
    "carry_items",
    "support_items",
    "enchants",
    "skill_trainers",
    "hidden_lake",
    "pivot_from",
    "notes",
)

# llm_classification -> archetype item bucket.
_BUCKET_BY_CLASSIFICATION = {
    "carry": "carry_items",
    "core": "core_items",
    "support": "support_items",
}


class ApplierError(Exception):
    """Raised when the catalog cannot be safely applied (fail closed)."""


@dataclass
class ApplyResult:
    catalog: dict[str, Any]
    applied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.applied)


def ensure_supported_schema_version(catalog: dict[str, Any]) -> None:
    """Fail closed on catalogs we do not understand (newer/missing version)."""
    version = catalog.get("schema_version")
    if version != SUPPORTED_SCHEMA_VERSION:
        raise ApplierError(
            f"unsupported catalog schema_version {version!r}; "
            f"applier supports {SUPPORTED_SCHEMA_VERSION}"
        )


def validate_catalog(catalog: dict[str, Any], schema: dict[str, Any]) -> None:
    """Validate against builds_schema.json; raise ApplierError on any violation."""
    try:
        jsonschema.Draft7Validator(schema).validate(catalog)
    except jsonschema.ValidationError as exc:
        raise ApplierError(f"catalog failed schema validation: {exc.message}") from exc


def serialize_catalog(catalog: dict[str, Any]) -> str:
    """Canonical serialization for minimal, reviewable, idempotent diffs.

    sort_keys=False is deliberate: catalog key order is curator-meaningful and
    json.load preserves insertion order. (The machine-artifact serializers in
    pipeline.py / diff.py use sort_keys=True; the catalog must not.)
    """
    return json.dumps(catalog, indent=2, ensure_ascii=False, sort_keys=False) + "\n"


def apply_proposed_changes(
    catalog: dict[str, Any], diff_json: dict[str, Any]
) -> ApplyResult:
    """Apply additive proposed_changes to a deep copy of ``catalog``.

    Returns the new catalog plus applied/skipped summaries. The input is never
    mutated. Re-running with an already-applied diff is a byte-stable no-op.
    """
    result = ApplyResult(catalog=copy.deepcopy(catalog))

    # Belt-and-suspenders semantic gate: no_llm_shadow / classification_pending
    # output must never mutate the catalog, even if called outside the phase gate.
    if not diff_json.get("semantic_classification"):
        result.skipped.append(
            "semantic_classification is false; no changes applied"
        )
        return result

    proposed = diff_json.get("proposed_changes") or {}

    for entry in proposed.get("archetype_updates", []) or []:
        _apply_update(result, entry)

    for entry in proposed.get("archetype_additions", []) or []:
        _apply_addition(result, entry)

    # item_removal_candidates / archetype_removal_candidates / archetype_reshuffles
    # are intentionally not applied (additive only).
    return result


def _apply_update(result: ApplyResult, entry: dict[str, Any]) -> None:
    phase = entry.get("phase")
    name = entry.get("archetype")
    archetypes = _phase_archetypes(result.catalog, phase)
    if archetypes is None:
        result.skipped.append(
            f"archetype_update {name!r} in phase {phase!r}: phase missing or has no archetypes"
        )
        return
    arch = _find_archetype(archetypes, name)
    if arch is None:
        result.skipped.append(
            f"archetype_update {name!r} in phase {phase!r}: archetype not found"
        )
        return
    for missing in entry.get("missing_items", []) or []:
        _merge_item(result, archetypes, arch, missing, phase, name)


def _apply_addition(result: ApplyResult, entry: dict[str, Any]) -> None:
    phase = entry.get("candidate_phase")
    name = entry.get("tag") or "unknown"
    archetypes = _phase_archetypes(result.catalog, phase)
    if archetypes is None:
        result.skipped.append(
            f"archetype_addition {name!r} in phase {phase!r}: phase missing or has no archetypes"
        )
        return

    items = list(entry.get("candidate_core", []) or []) + list(
        entry.get("candidate_support", []) or []
    )

    existing = _find_archetype(archetypes, name)
    if existing is not None:
        # Idempotent: an addition whose name already exists merges into it
        # rather than appending a duplicate archetype.
        for item in items:
            _merge_item(result, archetypes, existing, item, phase, name)
        return

    new_arch: dict[str, Any] = {"name": name, "phase": phase}
    core = [
        str(i["item"])
        for i in items
        if i.get("llm_classification") == "core" and i.get("item")
    ]
    carry = [
        str(i["item"])
        for i in items
        if i.get("llm_classification") == "carry" and i.get("item")
    ]
    support = [
        str(i["item"])
        for i in items
        if i.get("llm_classification") == "support" and i.get("item")
    ]
    if core:
        new_arch["core_items"] = _dedupe(core)
    # carry_items / support_items are schema-required: always emit (possibly []).
    new_arch["carry_items"] = _dedupe(carry)
    new_arch["support_items"] = _dedupe(support)
    archetypes.append(_reorder_archetype(new_arch))
    result.applied.append(
        f"archetype_addition: added {name!r} to phase {phase!r}"
    )


def _merge_item(
    result: ApplyResult,
    archetypes: list[Any],
    arch: dict[str, Any],
    item_entry: dict[str, Any],
    phase: Any,
    name: Any,
) -> None:
    item = item_entry.get("item")
    classification = item_entry.get("llm_classification")
    bucket = _BUCKET_BY_CLASSIFICATION.get(classification)
    if not item or bucket is None:
        result.skipped.append(
            f"item {item!r} in {name!r}/{phase!r}: unmapped classification {classification!r}"
        )
        return
    item = str(item)
    key_created = bucket not in arch
    target = arch.setdefault(bucket, [])
    if item in target:
        return  # already present -> idempotent no-op
    target.append(item)
    result.applied.append(
        f"{name!r}/{phase!r}: added {item!r} to {bucket}"
    )
    if key_created:
        # New bucket key: re-emit the archetype in canonical key order so the
        # added key lands deterministically. Only done when a key is created;
        # pure content changes leave existing key order untouched.
        idx = archetypes.index(arch)
        archetypes[idx] = _reorder_archetype(arch)


def _phase_archetypes(catalog: dict[str, Any], phase: Any) -> list[Any] | None:
    game_phases = catalog.get("game_phases")
    if not isinstance(game_phases, dict):
        return None
    phase_data = game_phases.get(phase)
    if not isinstance(phase_data, dict):
        return None
    archetypes = phase_data.get("archetypes")
    if not isinstance(archetypes, list):
        return None
    return archetypes


def _find_archetype(archetypes: list[Any], name: Any) -> dict[str, Any] | None:
    for arch in archetypes:
        if isinstance(arch, dict) and arch.get("name") == name:
            return arch
    return None


def _reorder_archetype(arch: dict[str, Any]) -> dict[str, Any]:
    ordered: dict[str, Any] = {}
    for key in _ARCHETYPE_KEY_ORDER:
        if key in arch:
            ordered[key] = arch[key]
    for key, value in arch.items():
        if key not in ordered:
            ordered[key] = value
    return ordered


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _normalize(path: Path) -> int:
    catalog = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(serialize_catalog(catalog), encoding="utf-8")
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Catalog applier utilities (one-time canonical normalization)."
    )
    parser.add_argument(
        "--normalize",
        type=Path,
        required=True,
        metavar="CATALOG",
        help="Rewrite a <hero>_builds.json in canonical serialization form.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    return _normalize(args.normalize)


if __name__ == "__main__":
    raise SystemExit(main())
