"""Proposal-only helper for manually supplied game-change source text.

Reads a local source file and a local candidate JSON file; writes a
strict-compatible candidate signal document. Makes no network calls.

Usage:
    python -m automated_builds_pipeline.game_change_proposals \\
        --source-file artifacts/manual_patch_notes.md \\
        --candidate-file artifacts/manual_candidates.json \\
        --output artifacts/game_change_signal_candidates.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Optional

from automated_builds_pipeline.game_changes import (
    SIGNAL_TYPES,
    GameChangeSignalError,
    parse_signals,
)

SCHEMA_VERSION = 1

_OPTIONAL_METADATA_KEYS = ("source_kind", "source_title", "source_accessed_at", "confidence")
_REQUIRED_CANDIDATE_FIELDS = frozenset({"type", "item", "note"})
_CANDIDATE_VALID_FIELDS = frozenset(
    {"type", "item", "note", "hero", "replacement_item", "confidence", "uncertainty_note"}
)


def _slugify(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def _make_signal_id(effective_date: str, item: str, signal_type: str, seen: dict[str, int]) -> str:
    type_slug = signal_type.replace("_", "-")
    base = f"proposed-{effective_date}-{_slugify(item)}-{type_slug}"
    count = seen.get(base, 0) + 1
    seen[base] = count
    return base if count == 1 else f"{base}-{count}"


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build_signal_record(
    candidate: dict[str, Any],
    *,
    index: int,
    source_url: str,
    effective_date: str,
    patch: Optional[str],
    source_kind: Optional[str],
    source_title: Optional[str],
    source_accessed_at: Optional[str],
    top_confidence: Optional[str],
    source_file: str,
    source_sha256: str,
    seen_ids: dict[str, int],
) -> dict[str, Any]:
    prefix = f"candidate[{index}]"

    unknown = set(candidate) - _CANDIDATE_VALID_FIELDS
    if unknown:
        raise ValueError(f"{prefix} has unknown fields: {', '.join(sorted(unknown))}")

    missing = _REQUIRED_CANDIDATE_FIELDS - set(candidate)
    if missing:
        raise ValueError(f"{prefix} missing required fields: {', '.join(sorted(missing))}")

    signal_type = candidate["type"]
    if not isinstance(signal_type, str) or signal_type not in SIGNAL_TYPES:
        raise ValueError(f"{prefix} has unsupported type: {signal_type!r}")

    item = candidate["item"]
    if not isinstance(item, str) or not item:
        raise ValueError(f"{prefix}.item must be a non-empty string")

    note = candidate["note"]
    if not isinstance(note, str) or not note:
        raise ValueError(f"{prefix}.note must be a non-empty string")

    signal_id = _make_signal_id(effective_date, item, signal_type, seen_ids)

    # Candidate-level confidence overrides top-level when present.
    confidence = candidate.get("confidence") or top_confidence
    uncertainty_note = candidate.get("uncertainty_note")

    metadata: dict[str, Any] = {
        "status": "proposed",
        "curator": "manual",
        "source_file": source_file,
        "source_sha256": source_sha256,
    }
    if source_kind is not None:
        metadata["source_kind"] = source_kind
    if source_title is not None:
        metadata["source_title"] = source_title
    if source_accessed_at is not None:
        metadata["source_accessed_at"] = source_accessed_at
    if confidence is not None:
        metadata["confidence"] = confidence
    if uncertainty_note is not None:
        metadata["uncertainty_note"] = uncertainty_note

    missing_meta = [k for k in _OPTIONAL_METADATA_KEYS if k not in metadata]
    if missing_meta:
        metadata["missing_metadata"] = missing_meta

    signal: dict[str, Any] = {
        "id": signal_id,
        "type": signal_type,
        "item": item,
        "effective_date": effective_date,
        "source_url": source_url,
        "note": note,
    }

    hero = candidate.get("hero")
    if isinstance(hero, str) and hero:
        signal["hero"] = hero

    replacement_item = candidate.get("replacement_item")
    if isinstance(replacement_item, str) and replacement_item:
        signal["replacement_item"] = replacement_item

    if patch is not None:
        signal["patch"] = patch

    signal["metadata"] = metadata
    return signal


def build_candidate_signals(source_file: Path, candidate_file: Path) -> dict[str, Any]:
    """Read source + candidate files; return a strict-compatible signal document."""
    if not source_file.is_file():
        raise ValueError(f"Source file does not exist: {source_file}")
    if not candidate_file.is_file():
        raise ValueError(f"Candidate file does not exist: {candidate_file}")

    try:
        raw = json.loads(candidate_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Malformed candidate JSON: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError("Candidate file must be a JSON object")

    source_url = raw.get("source_url")
    if not isinstance(source_url, str) or not source_url:
        raise ValueError("Candidate file missing required field: source_url")

    effective_date = raw.get("effective_date")
    if not isinstance(effective_date, str) or not effective_date:
        raise ValueError("Candidate file missing required field: effective_date")

    candidates = raw.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("Candidate file must include a non-empty 'candidates' list")

    source_kind: Optional[str] = raw.get("source_kind") or None
    source_title: Optional[str] = raw.get("source_title") or None
    source_accessed_at: Optional[str] = raw.get("source_accessed_at") or None
    patch: Optional[str] = raw.get("patch") or None
    top_confidence: Optional[str] = raw.get("confidence") or None

    sha256 = _file_sha256(source_file)
    seen_ids: dict[str, int] = {}
    signals = []

    for i, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            raise ValueError(f"candidate[{i}] must be an object")
        signals.append(
            _build_signal_record(
                candidate,
                index=i,
                source_url=source_url,
                effective_date=effective_date,
                patch=patch,
                source_kind=source_kind,
                source_title=source_title,
                source_accessed_at=source_accessed_at,
                top_confidence=top_confidence,
                source_file=str(source_file),
                source_sha256=sha256,
                seen_ids=seen_ids,
            )
        )

    return {"schema_version": SCHEMA_VERSION, "signals": signals}


def run(args: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Draft candidate game-change signal records from manually supplied source text."
    )
    parser.add_argument("--source-file", required=True, type=Path)
    parser.add_argument("--candidate-file", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--force", action="store_true")
    ns = parser.parse_args(args)

    if ns.output.exists() and not ns.force:
        print(f"ERROR: Output already exists: {ns.output}. Use --force to overwrite.", file=sys.stderr)
        return 1

    try:
        doc = build_candidate_signals(ns.source_file, ns.candidate_file)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    try:
        parse_signals(doc, source=str(ns.output))
    except GameChangeSignalError as exc:
        print(f"ERROR: Generated output failed validation: {exc}", file=sys.stderr)
        return 1

    ns.output.parent.mkdir(parents=True, exist_ok=True)
    tmp = ns.output.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        tmp.replace(ns.output)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise

    print(f"Wrote {len(doc['signals'])} candidate signal(s) to {ns.output}")
    return 0


if __name__ == "__main__":
    sys.exit(run())
