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
    semantic_classification = diff.get("semantic_classification")
    lines.extend(
        [
            "## Pipeline State",
            f"- Hero: {diff.get('hero', 'unknown')}",
            f"- Window: {window.get('start', 'unknown')} to {window.get('end', 'unknown')} ({window.get('n_windows_history', 0)} windows)",
            f"- Freeze: {'Removals frozen' if freeze.get('removals_frozen') else 'not frozen'}",
            f"- Patch: {freeze.get('patch_label') or 'none'}",
        ]
    )
    if semantic_classification is False:
        lines.append(f"- Classification mode: {diff.get('classification_mode', 'unknown')}")
        lines.append(f"- Classifier provider: {diff.get('classifier_provider', 'unknown')}")
        lines.append(
            "- Warning: observation-only shadow output is operational evidence only; it does not validate semantic catalog acceptance."
        )
    invalid_names = _invalid_classifier_item_count(diff)
    if invalid_names:
        lines.append(
            f"- Warning: {invalid_names} item name(s) were dropped as invalid_classifier_item; "
            f"the coach card_cache_names.txt may be stale (regenerate via "
            f"coach.py refresh-images)."
        )
    health = diff.get("source_health", [])
    if health:
        lines.append("")
        lines.append("| Source | Status | Window |")
        lines.append("| --- | --- | --- |")
        for row in health:
            lines.append(f"| {row.get('source', '')} | {row.get('status', '')} | {row.get('window_id', '')} |")
    lines.append("")


def _invalid_classifier_item_count(diff: dict[str, Any]) -> int:
    """Count items dropped as ``invalid_classifier_item`` in the diff noise.

    Counts individual rows plus any ``{"summary": true, "count": N}`` rollups so
    the stale-name signal survives diff noise summarization.
    """
    total = 0
    for row in diff.get("noise", []):
        if not isinstance(row, dict) or row.get("reason") not in {"invalid_classifier_item", "invalid_llm_item"}:
            continue
        if row.get("summary"):
            count = row.get("count", 0)
            total += count if isinstance(count, int) else 0
        else:
            total += 1
    return total


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
        if "candidate_pending" in addition:
            _bucket_lines(lines, "Pending semantic classification", addition.get("candidate_pending", []))
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
        lines.append("### Retirement Reviews")
        for row in archetypes:
            lines.append(_retirement_line(row, include_item=True))
            _affected_build_lines(lines, row)
            _signal_lines(lines, row)
            _blocks(lines, row)
    if items:
        lines.append("### Items")
        for row in items:
            lines.append(_retirement_line(row, include_item=True))
            _signal_lines(lines, row)
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
    classification = item.get("classification", item.get("llm_classification"))
    confidence = item.get("confidence", item.get("llm_confidence"))
    rationale = item.get("rationale", item.get("llm_rationale"))
    return f"- {item.get('item')}: {classification} ({confidence}) - {rationale}"


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
        lines.append(f"  Secondary/blocking context: {', '.join(row['removal_blocked_by'])}")
    if row.get("freeze_blocked"):
        lines.append("  Removals frozen.")
    refs = row.get("evidence_refs", [])
    if refs:
        lines.append(f"  Absence confirmed by: {', '.join(_ref_label(ref) for ref in refs)}")


def _retirement_line(row: dict[str, Any], *, include_item: bool) -> str:
    fields = [
        _location(row, include_item=include_item),
        f"bucket: {row.get('catalog_bucket') or 'unknown'}",
        f"type: {row.get('retirement_type') or 'review'}",
        f"basis: {row.get('retirement_basis') or row.get('reason') or 'review'}",
        f"action: {row.get('actionability') or 'review'}",
        _current_bazaardb(row),
        _secondary_context(row),
        _freeze_state(row),
        _signal_label(row),
    ]
    details = _retirement_review_details(row)
    if details:
        fields.append(details)
    return "- " + " | ".join(field for field in fields if field)


def _location(row: dict[str, Any], *, include_item: bool) -> str:
    pieces = [row.get("phase") or "unknown", row.get("archetype") or "unknown"]
    if include_item:
        pieces.append(row.get("item") or "unknown")
    return " / ".join(str(piece) for piece in pieces)


def _current_bazaardb(row: dict[str, Any]) -> str:
    evidence = row.get("current_patch_evidence")
    if isinstance(evidence, dict):
        bazaardb = evidence.get("bazaardb")
        if isinstance(bazaardb, dict):
            presence = bazaardb.get("presence")
            window = bazaardb.get("window_id")
            if presence:
                suffix = f" ({window})" if window else ""
                return f"current BazaarDB: {presence}{suffix}"
    presence = row.get("source_presence")
    if isinstance(presence, dict) and presence.get("bazaardb"):
        return f"current BazaarDB: {presence['bazaardb']}"
    return ""


def _secondary_context(row: dict[str, Any]) -> str:
    current = row.get("source_presence")
    if not isinstance(current, dict):
        return ""
    present = [
        source
        for source, status in sorted(current.items())
        if source != "bazaardb" and status == "present"
    ]
    if present:
        label = "secondary blocker" if row.get("removal_blocked_by") else "secondary context"
        return f"{label}: {', '.join(present)}"
    known = [source for source, status in sorted(current.items()) if source != "bazaardb" and status not in {"skipped", "unknown", None}]
    if known:
        return "secondary current: clear"
    return ""


def _freeze_state(row: dict[str, Any]) -> str:
    if row.get("freeze_blocked") or row.get("actionability") == "freeze_blocked":
        return "freeze: blocked"
    return ""


def _signal_label(row: dict[str, Any]) -> str:
    signals = row.get("signal_evidence")
    if not isinstance(signals, list) or not signals:
        return ""
    first = next((signal for signal in signals if isinstance(signal, dict)), None)
    if not first:
        return ""
    label = first.get("id") or first.get("type")
    return f"signal: {label}" if label else ""


def _retirement_review_details(row: dict[str, Any]) -> str:
    affected = row.get("affected_items", [])
    if not affected:
        return ""
    return f"affected: {', '.join(str(item) for item in affected)}"


def _affected_build_lines(lines: list[str], row: dict[str, Any]) -> None:
    build_items = row.get("affected_build_items")
    if not isinstance(build_items, dict) or not build_items:
        return
    for bucket in ("carry_items", "core_items", "condition_items"):
        items = build_items.get(bucket)
        if items:
            lines.append(f"  {bucket}: {', '.join(str(item) for item in items)}")


def _signal_lines(lines: list[str], row: dict[str, Any]) -> None:
    signals = row.get("signal_evidence")
    if not isinstance(signals, list) or not signals:
        return
    for signal in signals:
        if not isinstance(signal, dict):
            continue
        bits = [
            str(signal.get("id") or "unknown"),
            str(signal.get("type") or "signal"),
            str(signal.get("effective_date") or "unknown-date"),
        ]
        if signal.get("replacement_item"):
            bits.append(f"replacement: {signal['replacement_item']}")
        if signal.get("source_url"):
            bits.append(str(signal["source_url"]))
        lines.append(f"  Signal: {'; '.join(bits)}")


def _ref_label(ref: Any) -> str:
    if isinstance(ref, dict):
        return str(ref.get("summary") or ref.get("artifact_ref") or ref.get("source") or ref)
    return str(ref)


def _empty(lines: list[str]) -> None:
    lines.append("No items in this section.")
    lines.append("")
