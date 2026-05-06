"""Generate read-only automated build proposal diff artifacts."""

from __future__ import annotations

import argparse
import json
from dataclasses import fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from automated_builds_pipeline.evaluator import EvaluationResult
from automated_builds_pipeline.llm import DEFAULT_MODEL, ItemClassification, LLMClassifier
from automated_builds_pipeline.proposal import render_proposal


def generate_diff(
    hero: str,
    evaluation: EvaluationResult,
    catalog: dict[str, Any],
    classifier: LLMClassifier,
    *,
    mock_mode: bool = False,
) -> dict[str, Any]:
    catalog_index = _catalog_index(catalog)
    rows = list(evaluation.rows)
    proposed_changes = {
        "archetype_updates": [],
        "archetype_additions": [],
        "archetype_removal_candidates": [],
        "item_removal_candidates": [],
        "archetype_reshuffles": [],
    }
    weaker_signals: list[dict[str, Any]] = []
    noise = _initial_noise(evaluation)

    for row in rows:
        if _has_reshuffle_signal(row):
            noise.append({"reason": "reshuffle_deferred", "item": row.get("item"), "phase": row.get("phase"), "archetype": row.get("archetype")})
            continue
        if row.get("threshold_result") == "archetype_remove_candidate":
            proposed_changes["archetype_removal_candidates"].append(_archetype_removal_row(row))
        elif row.get("threshold_result") == "remove_candidate":
            proposed_changes["item_removal_candidates"].append(_removal_row(row))

    for key, group in _group_llm_rows(rows).items():
        phase, archetype = key
        existing_buckets = catalog_index.get(key, _empty_buckets())
        classifications = _classify_group(hero, phase, archetype, existing_buckets, group, classifier, mock_mode)
        top_line: list[dict[str, Any]] = []
        for classification in classifications:
            if classification.classification == "invalid":
                noise.append({"reason": "invalid_llm_item", "item": classification.item, "archetype": archetype})
                continue
            row = _row_for_item(group, classification.item)
            emitted = _classification_to_diff(classification, row)
            if row.get("classification_ceiling") == "support_only" and emitted["llm_classification"] in {"carry", "core"}:
                emitted["llm_classification"] = "support"
                emitted["llm_rationale"] = f"{emitted['llm_rationale']} Source-quality gate capped this item at support."
            if classification.surface == "top_line" or mock_mode:
                top_line.append(emitted)
            elif classification.surface == "weaker_signal":
                weaker_signals.append({"phase": phase, "archetype": archetype, **emitted})
            else:
                noise.append({"reason": "low_confidence_suppressed", "item": classification.item, "archetype": archetype})
        if not top_line:
            continue
        if key in catalog_index:
            proposed_changes["archetype_updates"].append(
                {
                    "phase": phase,
                    "archetype": archetype,
                    "sample_count_latest": _sample_count(group),
                    "missing_items": top_line,
                }
            )
        else:
            proposed_changes["archetype_additions"].append(
                {
                    "tag": archetype or "unknown",
                    "candidate_phase": phase,
                    "candidate_core": [item for item in top_line if item["llm_classification"] in {"carry", "core"}],
                    "candidate_support": [item for item in top_line if item["llm_classification"] == "support"],
                    "evidence": _addition_evidence(group),
                }
            )

    return {
        "schema_version": 1,
        "hero": hero,
        "generated_at": _now_iso(),
        "window_id": _window_id(evaluation),
        "source_window": _source_window(evaluation),
        "freeze_state": {
            "removals_frozen": bool(evaluation.freeze_active),
            "patch_label": (evaluation.bazaardb_patch or {}).get("label") if evaluation.bazaardb_patch else None,
        },
        "source_health": list(evaluation.source_health),
        "proposed_changes": proposed_changes,
        "weaker_signals": weaker_signals,
        "noise": noise,
    }


def load_evaluation(path: Path) -> EvaluationResult:
    data = json.loads(path.read_text(encoding="utf-8"))
    allowed = {field.name for field in fields(EvaluationResult)}
    payload = {key: value for key, value in data.items() if key in allowed}
    payload.setdefault("freeze_active", False)
    payload.setdefault("generated_at", data.get("generated_at", _now_iso()))
    payload.setdefault("run_id", data.get("run_id", "unknown"))
    payload.setdefault("rows", data.get("rows", []))
    return EvaluationResult(**payload)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate automated build diff and proposal artifacts.")
    parser.add_argument("--hero", required=True)
    parser.add_argument("--evaluation", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--names-file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--mock", action="store_true")
    parser.add_argument("--api-key-env", default="CLAUDE_API_KEY")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    evaluation = load_evaluation(args.evaluation)
    classifier = LLMClassifier(DEFAULT_MODEL, known_items_path=args.names_file, api_key_env=args.api_key_env)
    classifier.known_items.update(_all_catalog_names(catalog))
    diff = generate_diff(args.hero, evaluation, catalog, classifier, mock_mode=args.mock)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    diff_path = args.output_dir / f"{args.hero}_diff.json"
    proposal_path = args.output_dir / f"{args.hero}_build_update_proposal.md"
    diff_path.write_text(json.dumps(diff, indent=2, sort_keys=True), encoding="utf-8")
    proposal_path.write_text(render_proposal(diff), encoding="utf-8")
    return 0


def _classify_group(
    hero: str,
    phase: Optional[str],
    archetype: Optional[str],
    existing_buckets: dict[str, list[str]],
    rows: list[dict[str, Any]],
    classifier: LLMClassifier,
    mock_mode: bool,
) -> list[ItemClassification]:
    if mock_mode:
        return [
            ItemClassification(str(row["item"]), "support", "low", "mock_mode", "top_line")
            for row in rows
        ]
    return classifier.classify_archetype(
        hero,
        phase,
        archetype,
        existing_buckets,
        rows,
        {"rows": rows, "evidence_refs": [ref for row in rows for ref in row.get("evidence_refs", [])]},
        _mobalytics_description(rows),
    )


def _group_llm_rows(rows: list[dict[str, Any]]) -> dict[tuple[Optional[str], Optional[str]], list[dict[str, Any]]]:
    grouped: dict[tuple[Optional[str], Optional[str]], list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("llm_input_required") is True and row.get("threshold_result") == "add_candidate":
            grouped.setdefault((row.get("phase"), row.get("archetype")), []).append(row)
    return grouped


def _catalog_index(catalog: dict[str, Any]) -> dict[tuple[Optional[str], Optional[str]], dict[str, list[str]]]:
    indexed: dict[tuple[Optional[str], Optional[str]], dict[str, list[str]]] = {}

    game_phases = catalog.get("game_phases")
    if isinstance(game_phases, dict):
        for phase_name, phase_data in game_phases.items():
            if not isinstance(phase_data, dict):
                continue
            for archetype in phase_data.get("archetypes", []) or []:
                if not isinstance(archetype, dict):
                    continue
                key = (phase_name, archetype.get("name"))
                indexed[key] = {
                    "carry_items": _str_list(archetype.get("carry_items", [])),
                    "core_items": _str_list(archetype.get("core_items", [])),
                    "support_items": _str_list(archetype.get("support_items", [])),
                    "condition_items": _str_list(archetype.get("condition_items", [])),
                }
            phase_supports: list[str] = []
            for bucket in ("universal_utility_items", "economy_items"):
                items = phase_data.get(bucket)
                if isinstance(items, list):
                    phase_supports.extend(str(i) for i in items if i)
            if phase_supports:
                key = (phase_name, None)
                buckets = indexed.setdefault(key, _empty_buckets())
                buckets["support_items"].extend(phase_supports)

    if isinstance(catalog.get("items"), list):
        for item in catalog["items"]:
            if not isinstance(item, dict):
                continue
            key = (item.get("phase"), item.get("archetype"))
            indexed.setdefault(key, _empty_buckets())["support_items"].append(str(item.get("item")))
    for archetype in catalog.get("archetypes", []) or []:
        if not isinstance(archetype, dict):
            continue
        key = (archetype.get("phase"), archetype.get("archetype") or archetype.get("tag"))
        indexed[key] = {
            "carry_items": _str_list(archetype.get("carry_items", [])),
            "core_items": _str_list(archetype.get("core_items", [])),
            "support_items": _str_list(archetype.get("support_items", [])),
            "condition_items": _str_list(archetype.get("condition_items", [])),
        }
    return indexed


def _classification_to_diff(classification: ItemClassification, row: dict[str, Any]) -> dict[str, Any]:
    emitted = classification.to_diff_dict()
    emitted.update(
        {
            "windows_seen": row.get("windows_seen"),
            "first_seen_window": row.get("first_seen_window"),
            "evidence_by_source": _evidence_by_source(row),
            "evidence_refs": list(row.get("evidence_refs", [])),
        }
    )
    return emitted


def _removal_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "phase": row.get("phase"),
        "archetype": row.get("archetype"),
        "item": row.get("item"),
        "reason": row.get("threshold_reason"),
        "windows_seen_recently": row.get("windows_seen_recently", 0),
        "removal_blocked_by": list(row.get("removal_blocked_by", [])),
        "freeze_blocked": "freeze_removals" in row.get("removal_blocked_by", []),
        "evidence_refs": list(row.get("evidence_refs", [])),
    }


def _archetype_removal_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "phase": row.get("phase"),
        "archetype": row.get("archetype"),
        "reason": row.get("threshold_reason"),
        "last_seen_window": row.get("last_seen_window"),
        "removal_blocked_by": list(row.get("removal_blocked_by", [])),
        "freeze_blocked": "freeze_removals" in row.get("removal_blocked_by", []),
        "evidence_refs": list(row.get("evidence_refs", [])),
    }


def _source_window(evaluation: EvaluationResult) -> dict[str, Any]:
    checked = [row.get("checked_at") for row in evaluation.source_health if row.get("checked_at")]
    return {
        "start": min(checked) if checked else None,
        "end": max(checked) if checked else evaluation.generated_at,
        "n_windows_history": len({row.get("window_id") for row in evaluation.source_health if row.get("window_id")}),
    }


def _window_id(evaluation: EvaluationResult) -> str:
    if evaluation.bazaardb_patch and evaluation.bazaardb_patch.get("label"):
        return str(evaluation.bazaardb_patch["label"])
    ids = [row.get("window_id") for row in evaluation.source_health if row.get("window_id")]
    return "+".join(ids) if ids else evaluation.run_id


def _initial_noise(evaluation: EvaluationResult) -> list[Any]:
    noise: list[Any] = []
    for item in evaluation.unresolved:
        noise.append({"reason": "unresolved", "detail": item})
    for row in evaluation.rows:
        if row.get("threshold_result") in {"no_change", "insufficient_history", "blocked"} and row.get("threshold_reason") not in {"none", ""}:
            noise.append({"reason": row.get("threshold_reason"), "item": row.get("item"), "archetype": row.get("archetype")})
    return noise


def _has_reshuffle_signal(row: dict[str, Any]) -> bool:
    if row.get("archetype_reshuffle_deferred"):
        return True
    unresolved = row.get("unresolved", [])
    return isinstance(unresolved, list) and "archetype_reshuffle_deferred" in unresolved


def _addition_evidence(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "windows_seen": max((row.get("windows_seen") or 0 for row in rows), default=0),
        "sample_count_total": sum(row.get("sample_count_latest") or 0 for row in rows),
    }


def _sample_count(rows: list[dict[str, Any]]) -> int:
    return max((row.get("sample_count_latest") or 0 for row in rows), default=0)


def _evidence_by_source(row: dict[str, Any]) -> dict[str, Any]:
    if isinstance(row.get("evidence_by_source"), dict):
        return row["evidence_by_source"]
    presence = row.get("source_presence", {})
    return {source: {"presence": value} for source, value in presence.items()} if isinstance(presence, dict) else {}


def _mobalytics_description(rows: list[dict[str, Any]]) -> str:
    descriptions = [str(row.get("mobalytics_description")) for row in rows if row.get("mobalytics_description")]
    return "\n\n".join(descriptions)


def _row_for_item(rows: list[dict[str, Any]], item: str) -> dict[str, Any]:
    return next((row for row in rows if row.get("item") == item), {})


def _empty_buckets() -> dict[str, list[str]]:
    return {"carry_items": [], "core_items": [], "support_items": [], "condition_items": []}


def _all_catalog_names(catalog: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for buckets in _catalog_index(catalog).values():
        for bucket in buckets.values():
            names.update(bucket)
    return names


def _str_list(value: Any) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
