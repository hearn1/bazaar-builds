"""Render automated build diff JSON into terse curator proposal markdown."""

from __future__ import annotations

from typing import Any


def render_proposal(diff: dict[str, Any]) -> str:
    lines: list[str] = []
    _pipeline_state(lines, diff)
    _archetype_updates(lines, diff)
    _archetype_additions(lines, diff)
    _removals(lines, diff)
    _weaker(lines, diff)
    _noise(lines, diff)
    return "\n".join(lines).rstrip() + "\n"


def _pipeline_state(lines: list[str], diff: dict[str, Any]) -> None:
    freeze = diff.get("freeze_state", {})
    window = diff.get("source_window", {})
    lines.extend(
        [
            "## Pipeline State",
            f"- Hero: {diff.get('hero', 'unknown')}",
            f"- Window: {window.get('start', 'unknown')} to {window.get('end', 'unknown')} ({window.get('n_windows_history', 0)} windows)",
            f"- Freeze: {'Removals frozen' if freeze.get('removals_frozen') else 'not frozen'}",
            f"- Patch: {freeze.get('patch_label') or 'none'}",
        ]
    )
    health = diff.get("source_health", [])
    if health:
        lines.append("")
        lines.append("| Source | Status | Window |")
        lines.append("| --- | --- | --- |")
        for row in health:
            lines.append(f"| {row.get('source', '')} | {row.get('status', '')} | {row.get('window_id', '')} |")
    lines.append("")


def _archetype_updates(lines: list[str], diff: dict[str, Any]) -> None:
    lines.append("## Existing Archetype Updates")
    updates = diff.get("proposed_changes", {}).get("archetype_updates", [])
    if not updates:
        _empty(lines)
        return
    for update in updates:
        lines.append(f"### {update.get('phase') or 'unknown'} / {update.get('archetype') or 'unknown'}")
        for item in update.get("missing_items", []):
            lines.append(_item_line(item))
            _evidence_lines(lines, item)
    lines.append("")


def _archetype_additions(lines: list[str], diff: dict[str, Any]) -> None:
    lines.append("## New Archetype Candidates")
    additions = diff.get("proposed_changes", {}).get("archetype_additions", [])
    if not additions:
        _empty(lines)
        return
    for addition in additions:
        lines.append(f"### {addition.get('candidate_phase') or 'unknown'} / {addition.get('tag') or 'unknown'}")
        _bucket_lines(lines, "Core", addition.get("candidate_core", []))
        _bucket_lines(lines, "Support", addition.get("candidate_support", []))
    lines.append("")


def _removals(lines: list[str], diff: dict[str, Any]) -> None:
    lines.append("## Removal Candidates")
    changes = diff.get("proposed_changes", {})
    archetypes = changes.get("archetype_removal_candidates", [])
    items = changes.get("item_removal_candidates", [])
    if not archetypes and not items:
        _empty(lines)
        return
    if archetypes:
        lines.append("### Archetypes")
        for row in archetypes:
            lines.append(f"- {row.get('phase') or 'unknown'} / {row.get('archetype') or 'unknown'}: {row.get('reason') or 'review'}")
            _blocks(lines, row)
    if items:
        lines.append("### Items")
        for row in items:
            lines.append(f"- {row.get('phase') or 'unknown'} / {row.get('archetype') or 'unknown'} / {row.get('item')}: {row.get('reason') or 'review'}")
            _blocks(lines, row)
    lines.append("")


def _weaker(lines: list[str], diff: dict[str, Any]) -> None:
    lines.append("## Weaker Signals")
    weaker = diff.get("weaker_signals", [])
    if not weaker:
        _empty(lines)
        return
    for row in weaker:
        lines.append(f"- {row.get('phase') or 'unknown'} / {row.get('archetype') or row.get('tag') or 'unknown'}: {_item_line(row)}")
    lines.append("")


def _noise(lines: list[str], diff: dict[str, Any]) -> None:
    lines.append("## Noise / No Evidence")
    noise = diff.get("noise", [])
    if not noise:
        _empty(lines)
        return
    for row in noise:
        if isinstance(row, dict):
            if row.get("summary"):
                lines.append(f"- {row.get('reason', 'noise')}: {row.get('count', 0)} rows")
                continue
            lines.append(f"- {row.get('reason', 'noise')}: {row.get('item') or row.get('archetype') or row.get('detail') or row}")
        else:
            lines.append(f"- {row}")
    lines.append("")


def _item_line(item: dict[str, Any]) -> str:
    return f"- {item.get('item')}: {item.get('llm_classification')} ({item.get('llm_confidence')}) - {item.get('llm_rationale')}"


def _bucket_lines(lines: list[str], label: str, items: list[dict[str, Any]]) -> None:
    lines.append(f"{label}:")
    if not items:
        lines.append("- None")
        return
    for item in items:
        lines.append(_item_line(item))


def _evidence_lines(lines: list[str], item: dict[str, Any]) -> None:
    refs = item.get("evidence_refs", [])
    if refs:
        lines.append(f"  Evidence: {', '.join(_ref_label(ref) for ref in refs)}")


def _blocks(lines: list[str], row: dict[str, Any]) -> None:
    if row.get("removal_blocked_by"):
        lines.append(f"  Blocked by: {', '.join(row['removal_blocked_by'])}")
    if row.get("freeze_blocked"):
        lines.append("  Removals frozen.")
    refs = row.get("evidence_refs", [])
    if refs:
        lines.append(f"  Absence confirmed by: {', '.join(_ref_label(ref) for ref in refs)}")


def _ref_label(ref: Any) -> str:
    if isinstance(ref, dict):
        return str(ref.get("summary") or ref.get("artifact_ref") or ref.get("source") or ref)
    return str(ref)


def _empty(lines: list[str]) -> None:
    lines.append("No items in this section.")
    lines.append("")
