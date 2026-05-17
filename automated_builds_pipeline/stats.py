"""Stats sidecar persistence for automated build refresh evidence."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

SCHEMA_VERSION = 1
SUPPORTED_SOURCES = frozenset(
    {
        "bazaardb",
        "mobalytics_meta_builds",
        "mobalytics_build_articles",
        "bazaar_builds_net",
        "in_house_tracker",
    }
)
DEFAULT_RETENTION_WINDOWS: dict[str, Optional[int]] = {
    "bazaardb": None,
    "mobalytics_meta_builds": 26,
    "mobalytics_build_articles": 26,
    "bazaar_builds_net": 26,
    "in_house_tracker": 26,
}


class StatsError(ValueError):
    """Raised when a stats sidecar cannot be parsed safely."""


@dataclass
class ItemWindowEvidence:
    item: str
    present: bool = True
    phase: Optional[str] = None
    archetype: Optional[str] = None
    appearances: Optional[int] = None
    sample_count: Optional[int] = None
    frequency: Optional[float] = None
    rank: Optional[int] = None
    archetypes_seen: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _drop_none(
            {
                "item": self.item,
                "present": self.present,
                "phase": self.phase,
                "archetype": self.archetype,
                "appearances": self.appearances,
                "sample_count": self.sample_count,
                "frequency": self.frequency,
                "rank": self.rank,
                "archetypes_seen": self.archetypes_seen,
                "evidence_refs": self.evidence_refs,
                "metadata": self.metadata,
            }
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ItemWindowEvidence":
        if not isinstance(data.get("item"), str) or not data["item"]:
            raise StatsError("item evidence requires a non-empty item")
        return cls(
            item=data["item"],
            present=_bool_field(data, "present", default=True),
            phase=_optional_str(data, "phase"),
            archetype=_optional_str(data, "archetype"),
            appearances=_optional_int(data, "appearances"),
            sample_count=_optional_int(data, "sample_count"),
            frequency=_optional_float(data, "frequency"),
            rank=_optional_int(data, "rank"),
            archetypes_seen=_str_list(data.get("archetypes_seen", []), "archetypes_seen"),
            evidence_refs=_str_list(data.get("evidence_refs", []), "evidence_refs"),
            metadata=_dict_field(data, "metadata", default={}),
        )


@dataclass
class WindowObservation:
    window_id: str
    observed_at: str
    items: list[ItemWindowEvidence] = field(default_factory=list)
    artifact_ref: Optional[str] = None
    health_status: str = "healthy"
    details: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return _drop_none(
            {
                "window_id": self.window_id,
                "observed_at": self.observed_at,
                "artifact_ref": self.artifact_ref,
                "health_status": self.health_status,
                "details": self.details,
                "items": [item.to_dict() for item in self.items],
            }
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WindowObservation":
        if not isinstance(data.get("window_id"), str) or not data["window_id"]:
            raise StatsError("window observation requires a non-empty window_id")
        if not isinstance(data.get("observed_at"), str) or not data["observed_at"]:
            raise StatsError("window observation requires observed_at")
        raw_items = data.get("items", [])
        if not isinstance(raw_items, list):
            raise StatsError("window observation items must be a list")
        return cls(
            window_id=data["window_id"],
            observed_at=data["observed_at"],
            items=[ItemWindowEvidence.from_dict(item) for item in raw_items],
            artifact_ref=_optional_str(data, "artifact_ref"),
            health_status=_str_field(data, "health_status", default="healthy"),
            details=_str_list(data.get("details", []), "details"),
        )


@dataclass
class WindowItemStats:
    window_id: str
    observed_at: str
    present: bool
    phase: Optional[str] = None
    archetype: Optional[str] = None
    appearances: Optional[int] = None
    sample_count: Optional[int] = None
    frequency: Optional[float] = None
    rank: Optional[int] = None
    archetypes_seen: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _drop_none(
            {
                "window_id": self.window_id,
                "observed_at": self.observed_at,
                "present": self.present,
                "phase": self.phase,
                "archetype": self.archetype,
                "appearances": self.appearances,
                "sample_count": self.sample_count,
                "frequency": self.frequency,
                "rank": self.rank,
                "archetypes_seen": self.archetypes_seen,
                "evidence_refs": self.evidence_refs,
                "metadata": self.metadata,
            }
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WindowItemStats":
        if not isinstance(data.get("window_id"), str) or not data["window_id"]:
            raise StatsError("per-window row requires window_id")
        if not isinstance(data.get("observed_at"), str) or not data["observed_at"]:
            raise StatsError("per-window row requires observed_at")
        return cls(
            window_id=data["window_id"],
            observed_at=data["observed_at"],
            present=_bool_field(data, "present", default=True),
            phase=_optional_str(data, "phase"),
            archetype=_optional_str(data, "archetype"),
            appearances=_optional_int(data, "appearances"),
            sample_count=_optional_int(data, "sample_count"),
            frequency=_optional_float(data, "frequency"),
            rank=_optional_int(data, "rank"),
            archetypes_seen=_str_list(data.get("archetypes_seen", []), "archetypes_seen"),
            evidence_refs=_str_list(data.get("evidence_refs", []), "evidence_refs"),
            metadata=_dict_field(data, "metadata", default={}),
        )


@dataclass
class SourceItemHistory:
    first_seen_window: Optional[str] = None
    last_seen_window: Optional[str] = None
    windows_seen: int = 0
    windows_observed: int = 0
    per_window: list[WindowItemStats] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "first_seen_window": self.first_seen_window,
            "last_seen_window": self.last_seen_window,
            "windows_seen": self.windows_seen,
            "windows_observed": self.windows_observed,
            "per_window": [row.to_dict() for row in self.per_window],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SourceItemHistory":
        raw_rows = data.get("per_window", [])
        if not isinstance(raw_rows, list):
            raise StatsError("per_window must be a list")
        history = cls(
            first_seen_window=_optional_str(data, "first_seen_window"),
            last_seen_window=_optional_str(data, "last_seen_window"),
            windows_seen=_int_field(data, "windows_seen", default=0),
            windows_observed=_int_field(data, "windows_observed", default=0),
            per_window=[WindowItemStats.from_dict(row) for row in raw_rows],
        )
        history.recalculate()
        return history

    def recalculate(self) -> None:
        self.windows_observed = len(self.per_window)
        seen = [row for row in self.per_window if row.present]
        self.windows_seen = len(seen)
        self.first_seen_window = seen[0].window_id if seen else None
        self.last_seen_window = seen[-1].window_id if seen else None


@dataclass
class PerItemHistory:
    per_source: dict[str, SourceItemHistory] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "per_source": {
                source: history.to_dict()
                for source, history in sorted(self.per_source.items())
            }
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PerItemHistory":
        raw_sources = data.get("per_source", {})
        if not isinstance(raw_sources, dict):
            raise StatsError("per_source must be an object")
        per_source = {}
        for source, history in raw_sources.items():
            _validate_source(source)
            if not isinstance(history, dict):
                raise StatsError(f"history for source {source} must be an object")
            per_source[source] = SourceItemHistory.from_dict(history)
        return cls(per_source=per_source)


@dataclass
class SourceWindowSummary:
    window_id: str
    observed_at: str
    health_status: str = "healthy"
    artifact_ref: Optional[str] = None
    details: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return _drop_none(
            {
                "window_id": self.window_id,
                "observed_at": self.observed_at,
                "health_status": self.health_status,
                "artifact_ref": self.artifact_ref,
                "details": self.details,
            }
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SourceWindowSummary":
        if not isinstance(data.get("window_id"), str) or not data["window_id"]:
            raise StatsError("source window requires window_id")
        if not isinstance(data.get("observed_at"), str) or not data["observed_at"]:
            raise StatsError("source window requires observed_at")
        return cls(
            window_id=data["window_id"],
            observed_at=data["observed_at"],
            health_status=_str_field(data, "health_status", default="healthy"),
            artifact_ref=_optional_str(data, "artifact_ref"),
            details=_str_list(data.get("details", []), "details"),
        )


@dataclass
class HeroStats:
    hero: str
    schema_version: int = SCHEMA_VERSION
    generated_at: Optional[str] = None
    last_classifier_mode: Optional[str] = None
    classifier_started_at: Optional[str] = None
    retention_windows: dict[str, Optional[int]] = field(
        default_factory=lambda: dict(DEFAULT_RETENTION_WINDOWS)
    )
    source_windows: dict[str, list[SourceWindowSummary]] = field(default_factory=dict)
    items: dict[str, PerItemHistory] = field(default_factory=dict)

    def item_history(self, item: str, source: str, limit: Optional[int] = None) -> list[WindowItemStats]:
        _validate_source(source)
        rows = self.items.get(item, PerItemHistory()).per_source.get(source, SourceItemHistory()).per_window
        if limit is None:
            return list(rows)
        return list(rows[-limit:])

    def source_window_count(self, source: str) -> int:
        _validate_source(source)
        return len(self.source_windows.get(source, []))

    def last_window_id(self, source: str) -> Optional[str]:
        _validate_source(source)
        rows = self.source_windows.get(source, [])
        return rows[-1].window_id if rows else None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": self.schema_version,
            "hero": self.hero,
            "generated_at": self.generated_at or _utc_now(),
            "retention_windows": {
                source: self.retention_windows.get(source)
                for source in sorted(SUPPORTED_SOURCES)
                if source in self.retention_windows
            },
            "source_windows": {
                source: [row.to_dict() for row in rows]
                for source, rows in sorted(self.source_windows.items())
            },
            "items": {
                item: history.to_dict()
                for item, history in sorted(self.items.items())
            },
        }
        if self.last_classifier_mode is not None:
            data["last_classifier_mode"] = self.last_classifier_mode
        if self.classifier_started_at is not None:
            data["classifier_started_at"] = self.classifier_started_at
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HeroStats":
        version = _int_field(data, "schema_version")
        if version > SCHEMA_VERSION:
            raise StatsError(
                f"Stats sidecar schema_version {version} is newer than supported {SCHEMA_VERSION}"
            )
        if version < SCHEMA_VERSION:
            raise StatsError(
                f"Stats sidecar schema_version {version} is older than supported {SCHEMA_VERSION}"
            )
        hero = _str_field(data, "hero")
        raw_retention = _dict_field(data, "retention_windows", default={})
        retention = dict(DEFAULT_RETENTION_WINDOWS)
        for source, cap in raw_retention.items():
            _validate_source(source)
            if cap is not None and (not isinstance(cap, int) or cap < 1):
                raise StatsError(f"retention cap for {source} must be null or a positive integer")
            retention[source] = cap

        raw_source_windows = _dict_field(data, "source_windows", default={})
        source_windows = {}
        for source, rows in raw_source_windows.items():
            _validate_source(source)
            if not isinstance(rows, list):
                raise StatsError(f"source_windows.{source} must be a list")
            source_windows[source] = [SourceWindowSummary.from_dict(row) for row in rows]

        raw_items = _dict_field(data, "items", default={})
        items = {}
        for item, history in raw_items.items():
            if not isinstance(item, str) or not item:
                raise StatsError("item history keys must be non-empty strings")
            if not isinstance(history, dict):
                raise StatsError(f"history for item {item} must be an object")
            items[item] = PerItemHistory.from_dict(history)

        return cls(
            hero=hero,
            schema_version=version,
            generated_at=_optional_str(data, "generated_at"),
            last_classifier_mode=_optional_str(data, "last_classifier_mode"),
            classifier_started_at=_optional_str(data, "classifier_started_at"),
            retention_windows=retention,
            source_windows=source_windows,
            items=items,
        )


def load_stats(hero: str, stats_dir: Path) -> HeroStats:
    path = stats_path(hero, stats_dir)
    if not path.exists():
        return HeroStats(hero=hero)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise StatsError(f"Could not parse stats sidecar {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise StatsError(f"Stats sidecar {path} must contain a JSON object")
    stats = HeroStats.from_dict(data)
    if _hero_slug(stats.hero) != _hero_slug(hero):
        raise StatsError(f"Stats sidecar hero {stats.hero!r} does not match requested hero {hero!r}")
    return stats


def append_window(stats: HeroStats, source: str, window: WindowObservation) -> None:
    _validate_source(source)
    _validate_window_id(source, window.window_id)

    summary = SourceWindowSummary(
        window_id=window.window_id,
        observed_at=window.observed_at,
        health_status=window.health_status,
        artifact_ref=window.artifact_ref,
        details=list(window.details),
    )
    summaries = [row for row in stats.source_windows.get(source, []) if row.window_id != window.window_id]
    summaries.append(summary)
    stats.source_windows[source] = _apply_summary_retention(summaries, stats.retention_windows.get(source))

    for item in window.items:
        item_history = stats.items.setdefault(item.item, PerItemHistory())
        source_history = item_history.per_source.setdefault(source, SourceItemHistory())
        source_history.per_window = [
            row for row in source_history.per_window if row.window_id != window.window_id
        ]
        source_history.per_window.append(
            WindowItemStats(
                window_id=window.window_id,
                observed_at=window.observed_at,
                present=item.present,
                phase=item.phase,
                archetype=item.archetype,
                appearances=item.appearances,
                sample_count=item.sample_count,
                frequency=item.frequency,
                rank=item.rank,
                archetypes_seen=list(item.archetypes_seen),
                evidence_refs=list(item.evidence_refs),
                metadata=dict(item.metadata),
            )
        )
        source_history.recalculate()

    _trim_source_to_retention(stats, source)


def save_stats(
    stats: HeroStats,
    stats_dir: Path,
    *,
    _before_replace: Optional[Callable[[Path, Path], None]] = None,
) -> None:
    stats_dir.mkdir(parents=True, exist_ok=True)
    stats.generated_at = _utc_now()
    path = stats_path(stats.hero, stats_dir)
    payload = json.dumps(stats.to_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    _write_atomic(path, payload, before_replace=_before_replace)


def stats_path(hero: str, stats_dir: Path) -> Path:
    return stats_dir / f"{_hero_slug(hero)}_stats.json"


def inspect_summary(hero: str, stats_dir: Path) -> str:
    stats = load_stats(hero, stats_dir)
    lines = [f"{stats.hero} stats sidecar", f"schema_version: {stats.schema_version}", f"items: {len(stats.items)}"]
    lines.append("sources:")
    for source in sorted(SUPPORTED_SOURCES):
        source_item_count = sum(1 for item in stats.items.values() if source in item.per_source)
        lines.append(
            f"  {source}: windows={stats.source_window_count(source)} "
            f"items={source_item_count} last_window={stats.last_window_id(source) or '-'}"
        )
    return "\n".join(lines)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect automated builds stats sidecars.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect = subparsers.add_parser("inspect", help="Print a sidecar summary for one hero.")
    inspect.add_argument("hero", help="Hero name, e.g. Karnok")
    inspect.add_argument("--stats-dir", type=Path, required=True, help="Directory containing *_stats.json files.")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.command == "inspect":
        print(inspect_summary(args.hero, args.stats_dir))
        return 0
    parser.error(f"unknown command: {args.command}")
    return 2


def _write_atomic(
    path: Path,
    payload: str,
    *,
    before_replace: Optional[Callable[[Path, Path], None]] = None,
) -> None:
    tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if before_replace:
            before_replace(tmp_path, path)
        os.replace(tmp_path, path)
        _fsync_directory(path.parent)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _apply_summary_retention(rows: list[SourceWindowSummary], cap: Optional[int]) -> list[SourceWindowSummary]:
    if cap is None:
        return rows
    return rows[-cap:]


def _trim_source_to_retention(stats: HeroStats, source: str) -> None:
    if stats.retention_windows.get(source) is None:
        return
    retained_ids = {row.window_id for row in stats.source_windows.get(source, [])}
    empty_items = []
    for item, item_history in stats.items.items():
        source_history = item_history.per_source.get(source)
        if source_history is None:
            continue
        source_history.per_window = [
            row for row in source_history.per_window if row.window_id in retained_ids
        ]
        source_history.recalculate()
        if not source_history.per_window:
            del item_history.per_source[source]
        if not item_history.per_source:
            empty_items.append(item)
    for item in empty_items:
        del stats.items[item]


def _validate_source(source: str) -> None:
    if source not in SUPPORTED_SOURCES:
        raise StatsError(f"Unsupported source {source!r}")


def _validate_window_id(source: str, window_id: str) -> None:
    if not window_id.startswith(f"{source}:"):
        raise StatsError(f"window_id {window_id!r} must start with {source!r} plus ':'")


def _hero_slug(hero: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", hero.casefold()).strip("_")
    if not slug:
        raise StatsError("hero must produce a non-empty stats filename")
    return slug


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _drop_none(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if value is not None}


def _str_field(data: dict[str, Any], field_name: str, default: Optional[str] = None) -> str:
    value = data.get(field_name, default)
    if not isinstance(value, str) or not value:
        raise StatsError(f"{field_name} must be a non-empty string")
    return value


def _optional_str(data: dict[str, Any], field_name: str) -> Optional[str]:
    value = data.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise StatsError(f"{field_name} must be a string or null")
    return value


def _int_field(data: dict[str, Any], field_name: str, default: Optional[int] = None) -> int:
    value = data.get(field_name, default)
    if not isinstance(value, int):
        raise StatsError(f"{field_name} must be an integer")
    return value


def _optional_int(data: dict[str, Any], field_name: str) -> Optional[int]:
    value = data.get(field_name)
    if value is None:
        return None
    if not isinstance(value, int):
        raise StatsError(f"{field_name} must be an integer or null")
    return value


def _optional_float(data: dict[str, Any], field_name: str) -> Optional[float]:
    value = data.get(field_name)
    if value is None:
        return None
    if not isinstance(value, (int, float)):
        raise StatsError(f"{field_name} must be a number or null")
    return float(value)


def _bool_field(data: dict[str, Any], field_name: str, default: bool) -> bool:
    value = data.get(field_name, default)
    if not isinstance(value, bool):
        raise StatsError(f"{field_name} must be a boolean")
    return value


def _dict_field(data: dict[str, Any], field_name: str, default: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    value = data.get(field_name, default)
    if not isinstance(value, dict):
        raise StatsError(f"{field_name} must be an object")
    return value


def _str_list(value: Any, field_name: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise StatsError(f"{field_name} must be a list of strings")
    return list(value)


if __name__ == "__main__":
    raise SystemExit(main())
