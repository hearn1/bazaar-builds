"""Read-only maintenance report for game_changes/signals.json lifecycle state."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from automated_builds_pipeline.game_changes import (
    DEFAULT_SIGNALS_PATH,
    INACTIVE_SIGNAL_STATUSES,
    GameChangeSignal,
    GameChangeSignals,
    load_signals,
)

_KNOWN_LIFECYCLE_STATUSES = frozenset({"proposed", "reviewed", "deprecated", "superseded"})
_ACTIVE_INACTIVE = frozenset({"deprecated", "superseded"})


def _metadata_status(signal: GameChangeSignal) -> str | None:
    return signal.metadata_status


def _deprecated_at(signal: GameChangeSignal) -> str | None:
    if not isinstance(signal.metadata, dict):
        return None
    v = signal.metadata.get("deprecated_at")
    return v if isinstance(v, str) and v else None


def _superseded_at(signal: GameChangeSignal) -> str | None:
    if not isinstance(signal.metadata, dict):
        return None
    v = signal.metadata.get("superseded_at")
    return v if isinstance(v, str) and v else None


def _status_note(signal: GameChangeSignal) -> str | None:
    if not isinstance(signal.metadata, dict):
        return None
    v = signal.metadata.get("status_note")
    return v if isinstance(v, str) and v else None


def run_maintenance_check(signals: GameChangeSignals) -> dict[str, Any]:
    """Return a structured report dict; never mutates signals or the file."""
    all_sigs = list(signals.signals)
    active = [s for s in all_sigs if not s.is_inactive]
    deprecated = [s for s in all_sigs if s.is_deprecated]
    superseded = [s for s in all_sigs if s.is_superseded]
    proposed = [s for s in all_sigs if _metadata_status(s) == "proposed"]
    unknown_status = [
        s
        for s in all_sigs
        if _metadata_status(s) is not None and _metadata_status(s) not in _KNOWN_LIFECYCLE_STATUSES
    ]

    errors: list[str] = []
    warnings: list[str] = []

    # Duplicate id check
    seen_ids: dict[str, int] = {}
    for s in all_sigs:
        seen_ids[s.id] = seen_ids.get(s.id, 0) + 1
    for sid, count in seen_ids.items():
        if count > 1:
            errors.append(f"duplicate id: {sid!r} appears {count} times")

    id_set = {s.id for s in all_sigs}

    for s in superseded:
        sup_by = s.superseded_by
        # superseded_by is required
        if not sup_by:
            errors.append(f"{s.id!r}: status=superseded but missing superseded_by")
            continue
        # self-supersession
        if sup_by == s.id:
            errors.append(f"{s.id!r}: superseded_by points to itself")
            continue
        # dangling reference
        if sup_by not in id_set:
            errors.append(f"{s.id!r}: superseded_by={sup_by!r} references unknown signal id")
            continue
        # superseded_at warning
        if not _superseded_at(s):
            warnings.append(f"{s.id!r}: status=superseded without superseded_at date")

    # supersession cycle detection
    by_id = {s.id: s for s in all_sigs}
    for start in superseded:
        visited: set[str] = set()
        cursor = start.superseded_by
        while cursor and cursor in by_id and cursor not in visited:
            visited.add(cursor)
            next_sig = by_id[cursor]
            if next_sig.is_superseded:
                cursor = next_sig.superseded_by
            else:
                cursor = None
        if cursor and cursor in visited:
            errors.append(f"{start.id!r}: supersession cycle detected involving {cursor!r}")

    # superseding record itself is inactive warning
    for s in superseded:
        sup_by = s.superseded_by
        if sup_by and sup_by in by_id and by_id[sup_by].is_inactive:
            warnings.append(
                f"{s.id!r}: superseded_by={sup_by!r} but that signal is also inactive"
            )

    for s in deprecated:
        if not _deprecated_at(s):
            warnings.append(f"{s.id!r}: status=deprecated without deprecated_at date")
        if not _status_note(s):
            warnings.append(f"{s.id!r}: status=deprecated without status_note")

    for s in unknown_status:
        warnings.append(f"{s.id!r}: unknown metadata.status={_metadata_status(s)!r}")

    for s in proposed:
        warnings.append(f"{s.id!r}: metadata.status=proposed (non-reviewed; belongs in game_change_proposals, not signals.json)")

    # item/type/hero mismatch between superseded and superseding
    for s in superseded:
        sup_by = s.superseded_by
        if not sup_by or sup_by not in by_id:
            continue
        replacement = by_id[sup_by]
        mismatches = []
        if s.item.casefold() != replacement.item.casefold():
            mismatches.append(f"item {s.item!r} → {replacement.item!r}")
        if s.type != replacement.type:
            mismatches.append(f"type {s.type!r} → {replacement.type!r}")
        if (s.hero or "").casefold() != (replacement.hero or "").casefold():
            mismatches.append(f"hero {s.hero!r} → {replacement.hero!r}")
        if mismatches and not _status_note(s):
            warnings.append(
                f"{s.id!r}: superseded by {sup_by!r} with mismatches ({'; '.join(mismatches)}) but no status_note"
            )

    return {
        "totals": {
            "total": len(all_sigs),
            "active": len(active),
            "deprecated": len(deprecated),
            "superseded": len(superseded),
            "proposed": len(proposed),
            "unknown_status": len(unknown_status),
        },
        "active_ids": [s.id for s in active],
        "deprecated_ids": [s.id for s in deprecated],
        "superseded_ids": [s.id for s in superseded],
        "proposed_ids": [s.id for s in proposed],
        "unknown_status_ids": [s.id for s in unknown_status],
        "errors": errors,
        "warnings": warnings,
        "ok": len(errors) == 0,
    }


def _format_markdown(report: dict[str, Any]) -> str:
    t = report["totals"]
    lines = [
        "# Game-change signal maintenance report",
        "",
        "## Summary",
        "",
        "| Status | Count |",
        "|---|---|",
        f"| Total | {t['total']} |",
        f"| Active | {t['active']} |",
        f"| Deprecated | {t['deprecated']} |",
        f"| Superseded | {t['superseded']} |",
        f"| Proposed (non-reviewed) | {t['proposed']} |",
        f"| Unknown status | {t['unknown_status']} |",
        "",
    ]
    if report["errors"]:
        lines += ["## Errors", ""]
        for e in report["errors"]:
            lines.append(f"- **ERROR**: {e}")
        lines.append("")
    if report["warnings"]:
        lines += ["## Warnings", ""]
        for w in report["warnings"]:
            lines.append(f"- WARNING: {w}")
        lines.append("")
    if not report["errors"] and not report["warnings"]:
        lines += ["## Result", "", "No issues found.", ""]
    elif not report["errors"]:
        lines += ["## Result", "", "No hard errors. Warnings above require curator attention.", ""]
    else:
        lines += ["## Result", "", f"{len(report['errors'])} hard error(s) found. Fix before merging.", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only maintenance report for game_changes/signals.json lifecycle state."
    )
    parser.add_argument(
        "--signals",
        type=Path,
        default=DEFAULT_SIGNALS_PATH,
        help="Path to signals.json (default: game_changes/signals.json in this repo)",
    )
    parser.add_argument(
        "--format",
        choices=["markdown", "json"],
        default="markdown",
        help="Output format (default: markdown)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write output to this file instead of stdout",
    )
    args = parser.parse_args(argv)

    signals = load_signals(args.signals)
    report = run_maintenance_check(signals)

    if args.format == "json":
        output = json.dumps(report, indent=2)
    else:
        output = _format_markdown(report)

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output, encoding="utf-8")
        print(f"Report written to {args.output}")
    else:
        print(output)

    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
