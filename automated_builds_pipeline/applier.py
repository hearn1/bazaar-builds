"""Deterministically apply proposed_changes to a player-facing <hero>_builds.json.

Supported writes are intentionally narrow: additive archetype changes plus
exact archetype support_items retirements. Higher-risk retirement and reshuffle
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

# v1 artifact compatibility: llm_classification now carries deterministic
# classifier output.
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
    """Apply supported proposed_changes to a deep copy of ``catalog``.

    Returns the new catalog plus applied/skipped summaries. The input is never
    mutated. Re-running with an already-applied diff is a byte-stable no-op.
    """
    result = ApplyResult(catalog=copy.deepcopy(catalog))

    # Belt-and-suspenders semantic gate: observation-only / classification_pending
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

    for entry in proposed.get("item_removal_candidates", []) or []:
        _apply_item_removal(result, entry)

    for entry in proposed.get("archetype_removal_candidates", []) or []:
        _skip_removal_candidate(result, "archetype_removal_candidate", entry)

    for entry in proposed.get("archetype_reshuffles", []) or []:
        _skip_removal_candidate(result, "archetype_reshuffle", entry)

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
    # Catalog commitment guard: an archetype represents the player's commitment
    # to a particular core or carry shape. Adding one with only support_items
    # would emit a stub that the coach scorer can match 1.0 on bare item
    # ownership, beating well-formed archetypes. Mirror coach's
    # test_archetypes_have_commitment_bucket and refuse the addition; the
    # candidate remains visible in the proposal artifact.
    if not core and not carry:
        result.skipped.append(
            f"archetype_addition {name!r} in phase {phase!r}: "
            "no core or carry classifications; refusing to add support-only stub"
        )
        return
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


def _apply_item_removal(result: ApplyResult, entry: dict[str, Any]) -> None:
    reason = _unsupported_item_removal_reason(entry)
    if reason is not None:
        result.skipped.append(
            f"item_removal {entry.get('item')!r}: {reason}"
        )
        return

    phase = entry.get("phase")
    name = entry.get("archetype")
    item = str(entry["item"])
    archetypes = _phase_archetypes(result.catalog, phase)
    if archetypes is None:
        result.skipped.append(
            f"item_removal {item!r} in phase {phase!r}: phase missing or has no archetypes"
        )
        return
    arch = _find_archetype(archetypes, name)
    if arch is None:
        result.skipped.append(
            f"item_removal {item!r} in {name!r}/{phase!r}: archetype not found"
        )
        return
    support_items = arch.get("support_items")
    if not isinstance(support_items, list):
        result.skipped.append(
            f"item_removal {item!r} in {name!r}/{phase!r}: support_items missing or not a list"
        )
        return
    if item not in support_items:
        result.skipped.append(
            f"item_removal {item!r} in {name!r}/{phase!r}: item not found in support_items"
        )
        return

    support_items.remove(item)
    result.applied.append(
        f"{name!r}/{phase!r}: removed {item!r} from support_items"
    )


def _unsupported_item_removal_reason(entry: dict[str, Any]) -> str | None:
    if entry.get("freeze_blocked") or "freeze_removals" in (
        entry.get("removal_blocked_by") or []
    ):
        return "freeze-blocked retirement candidate is review-only"
    if entry.get("catalog_bucket") != "support_items":
        return (
            f"unsupported catalog_bucket {entry.get('catalog_bucket')!r}; "
            "only support_items removals are applier-supported"
        )
    if entry.get("retirement_type") != "support_item":
        return (
            f"unsupported retirement_type {entry.get('retirement_type')!r}; "
            "only support_item removals are applier-supported"
        )
    if entry.get("actionability") != "item_removal_candidate":
        return f"unsupported actionability {entry.get('actionability')!r}"
    if not entry.get("phase"):
        return "missing exact phase"
    if not entry.get("archetype"):
        return "missing exact archetype; phase-level removals are not applier-supported"
    if not entry.get("item"):
        return "missing item"
    return None


def _skip_removal_candidate(
    result: ApplyResult, candidate_type: str, entry: dict[str, Any]
) -> None:
    item = entry.get("item")
    phase = entry.get("phase")
    archetype = entry.get("archetype")
    catalog_location = entry.get("catalog_location")
    location_bucket = (
        catalog_location.get("bucket")
        if isinstance(catalog_location, dict)
        else None
    )
    bucket = entry.get("catalog_bucket") or location_bucket
    actionability = entry.get("actionability")
    result.skipped.append(
        f"{candidate_type} {item!r} in {archetype!r}/{phase!r}: "
        f"not applier-supported (bucket={bucket!r}, actionability={actionability!r})"
    )


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
