"""Curator-owned promotion workflow for proposal-only game-change signal candidates.

Promotes selected proposal-only candidate signal records into the curated
game_changes/signals.json format. Requires explicit curator metadata,
selected candidate IDs, and review attestation. Makes no network calls.

Usage:
    python -m automated_builds_pipeline.game_change_promotions \\
        --candidate-file artifacts/game_change_signal_candidates.json \\
        --signals-file game_changes/signals.json \\
        --select proposed-2026-06-01-crystal-blade-removed-card \\
        --curator hearn1 \\
        --reviewed-at 2026-06-06 \\
        --output artifacts/promoted_signals_preview.json

To write directly to game_changes/signals.json (requires --force):
    python -m automated_builds_pipeline.game_change_promotions \\
        --candidate-file artifacts/game_change_signal_candidates.json \\
        --signals-file game_changes/signals.json \\
        --select proposed-2026-06-01-crystal-blade-removed-card \\
        --curator hearn1 \\
        --reviewed-at 2026-06-06 \\
        --output game_changes/signals.json \\
        --force
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

from automated_builds_pipeline.game_changes import (
    GameChangeSignalError,
    parse_signals,
)

SCHEMA_VERSION = 1
_VALID_CONFIDENCE = frozenset({"high", "medium", "low"})


class PromotionError(ValueError):
    """Raised when a candidate signal cannot be promoted."""


def _load_json_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise PromotionError(f"File does not exist: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PromotionError(f"Malformed JSON in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise PromotionError(f"Expected a JSON object in {path}")
    return raw


def _candidate_by_id(signals: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {s["id"]: s for s in signals if isinstance(s, dict) and isinstance(s.get("id"), str)}


def _dedupe_key(signal: dict[str, Any]) -> tuple[str, Optional[str], str, str]:
    return (
        signal.get("type", ""),
        signal.get("hero"),
        signal.get("item", ""),
        signal.get("effective_date", ""),
    )


def _validate_promotable(signal: dict[str, Any], *, allow_missing_metadata: bool) -> None:
    signal_id = signal.get("id", "<unknown>")
    metadata = signal.get("metadata") or {}

    status = metadata.get("status")
    if status != "proposed":
        raise PromotionError(
            f"Signal {signal_id!r}: metadata.status must be 'proposed', got {status!r}"
        )

    if not allow_missing_metadata and metadata.get("missing_metadata"):
        raise PromotionError(
            f"Signal {signal_id!r}: has unresolved missing_metadata "
            f"{metadata['missing_metadata']!r}. Use --allow-missing-metadata to promote anyway."
        )

    confidence = metadata.get("confidence")
    if confidence is None:
        raise PromotionError(f"Signal {signal_id!r}: missing required metadata.confidence")
    if confidence not in _VALID_CONFIDENCE:
        raise PromotionError(
            f"Signal {signal_id!r}: metadata.confidence must be one of "
            f"{sorted(_VALID_CONFIDENCE)}, got {confidence!r}"
        )

    signal_type = signal.get("type", "")
    uncertainty_note = metadata.get("uncertainty_note")

    if signal_type == "watchlist" and not uncertainty_note:
        raise PromotionError(
            f"Signal {signal_id!r}: type 'watchlist' requires metadata.uncertainty_note"
        )
    if confidence == "low" and not uncertainty_note:
        raise PromotionError(
            f"Signal {signal_id!r}: metadata.confidence 'low' requires metadata.uncertainty_note"
        )


def _promote_metadata(
    metadata: dict[str, Any],
    *,
    curator: str,
    reviewed_at: str,
    allow_missing_metadata: bool,
) -> dict[str, Any]:
    promoted = dict(metadata)
    promoted["status"] = "reviewed"
    promoted["curator"] = curator
    promoted["reviewed_at"] = reviewed_at
    # Remove missing_metadata when not needed — if allow_missing_metadata is True,
    # the fields are still absent so we preserve it as a marker.
    if not allow_missing_metadata:
        promoted.pop("missing_metadata", None)
    return promoted


def promote_candidate_signals(
    *,
    candidate_file: Path,
    signals_file: Path,
    selected_ids: list[str],
    curator: str,
    reviewed_at: str,
    allow_missing_metadata: bool = False,
) -> dict[str, Any]:
    """Promote selected candidates into the curated signal document.

    Returns the merged signal document with promoted records appended after
    existing signals. Does not write to disk.
    """
    if not selected_ids:
        raise PromotionError("At least one selected ID is required")

    candidate_raw = _load_json_object(candidate_file)
    try:
        parse_signals(candidate_raw, source=str(candidate_file))
    except GameChangeSignalError as exc:
        raise PromotionError(f"Candidate file failed signal validation: {exc}") from exc

    signals_raw = _load_json_object(signals_file)
    try:
        parse_signals(signals_raw, source=str(signals_file))
    except GameChangeSignalError as exc:
        raise PromotionError(f"Signals file failed validation: {exc}") from exc

    candidate_signals = candidate_raw.get("signals", [])
    existing_signals = signals_raw.get("signals", [])

    by_id = _candidate_by_id(candidate_signals)
    existing_ids = {s["id"] for s in existing_signals if isinstance(s, dict)}
    existing_dedupe_keys = {_dedupe_key(s) for s in existing_signals if isinstance(s, dict)}

    promoted = []
    for selected_id in selected_ids:
        if selected_id not in by_id:
            raise PromotionError(
                f"Selected ID {selected_id!r} not found in candidate document "
                f"(available: {sorted(by_id)})"
            )
        signal = by_id[selected_id]

        if selected_id in existing_ids:
            raise PromotionError(
                f"Selected ID {selected_id!r} already exists in the curated signals file"
            )

        key = _dedupe_key(signal)
        if key in existing_dedupe_keys:
            raise PromotionError(
                f"Selected signal {selected_id!r} has duplicate (type, hero, item, effective_date) "
                f"already present in the curated signals file"
            )

        _validate_promotable(signal, allow_missing_metadata=allow_missing_metadata)

        new_signal = dict(signal)
        new_signal["metadata"] = _promote_metadata(
            signal.get("metadata") or {},
            curator=curator,
            reviewed_at=reviewed_at,
            allow_missing_metadata=allow_missing_metadata,
        )
        promoted.append(new_signal)

        # Track within-selection duplicates
        existing_ids.add(selected_id)
        existing_dedupe_keys.add(key)

    output_doc: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "signals": list(existing_signals) + promoted,
    }

    try:
        parse_signals(output_doc, source="<promoted output>")
    except GameChangeSignalError as exc:
        raise PromotionError(f"Promoted output failed final validation: {exc}") from exc

    return output_doc


def run(args: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Promote selected proposal-only game-change candidates into the curated signal document."
        )
    )
    parser.add_argument("--candidate-file", required=True, type=Path)
    parser.add_argument("--signals-file", required=True, type=Path)
    parser.add_argument("--select", action="append", dest="selected_ids", metavar="ID")
    parser.add_argument("--curator", required=True)
    parser.add_argument("--reviewed-at", required=True, dest="reviewed_at")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--allow-missing-metadata",
        action="store_true",
        dest="allow_missing_metadata",
    )
    ns = parser.parse_args(args)

    if not ns.selected_ids:
        print("ERROR: At least one --select ID is required.", file=sys.stderr)
        return 1

    if ns.output.exists() and not ns.force:
        print(
            f"ERROR: Output already exists: {ns.output}. Use --force to overwrite.",
            file=sys.stderr,
        )
        return 1

    try:
        doc = promote_candidate_signals(
            candidate_file=ns.candidate_file,
            signals_file=ns.signals_file,
            selected_ids=ns.selected_ids,
            curator=ns.curator,
            reviewed_at=ns.reviewed_at,
            allow_missing_metadata=ns.allow_missing_metadata,
        )
    except PromotionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    ns.output.parent.mkdir(parents=True, exist_ok=True)
    tmp = ns.output.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        tmp.replace(ns.output)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise

    print(f"Promoted {len(ns.selected_ids)} candidate signal(s) to {ns.output}")
    return 0


if __name__ == "__main__":
    sys.exit(run())
