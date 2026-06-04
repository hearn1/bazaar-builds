"""Curated game-change retirement signals."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Optional

SCHEMA_VERSION = 1
SIGNAL_TYPES = frozenset(
    {
        "removed_card",
        "renamed_card",
        "explicit_invalidation",
        "major_nerf",
        "watchlist",
    }
)
REQUIRED_SIGNAL_FIELDS = frozenset({"id", "type", "item", "effective_date", "source_url", "note"})
OPTIONAL_SIGNAL_FIELDS = frozenset({"hero", "replacement_item", "patch", "metadata"})
SIGNAL_FIELDS = REQUIRED_SIGNAL_FIELDS | OPTIONAL_SIGNAL_FIELDS
DEFAULT_SIGNALS_PATH = Path(__file__).resolve().parents[1] / "game_changes" / "signals.json"


class GameChangeSignalError(ValueError):
    """Raised when a configured game-change signal file fails validation."""


@dataclass(frozen=True)
class GameChangeSignal:
    id: str
    type: str
    item: str
    effective_date: date
    source_url: str
    note: str
    hero: Optional[str] = None
    replacement_item: Optional[str] = None
    patch: Optional[str] = None
    metadata: dict[str, Any] | None = None

    @property
    def effective_date_iso(self) -> str:
        return self.effective_date.isoformat()

    def applies_to_hero(self, hero: Optional[str]) -> bool:
        if self.hero is None:
            return True
        if hero is None:
            return False
        return self.hero.casefold() == hero.casefold()

    def matches_item(self, item: str) -> bool:
        return self.item.casefold() == item.casefold()

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "type": self.type,
            "item": self.item,
            "effective_date": self.effective_date_iso,
            "source_url": self.source_url,
            "note": self.note,
        }
        if self.hero is not None:
            data["hero"] = self.hero
        if self.replacement_item is not None:
            data["replacement_item"] = self.replacement_item
        if self.patch is not None:
            data["patch"] = self.patch
        if self.metadata is not None:
            data["metadata"] = dict(self.metadata)
        return data


@dataclass(frozen=True)
class GameChangeSignals:
    signals: tuple[GameChangeSignal, ...] = ()

    def for_hero(self, hero: str) -> list[GameChangeSignal]:
        return [signal for signal in self.signals if signal.applies_to_hero(hero)]

    def for_item(self, item: str, *, hero: Optional[str] = None) -> list[GameChangeSignal]:
        return [
            signal
            for signal in self.signals
            if signal.matches_item(item) and (hero is None or signal.applies_to_hero(hero))
        ]

    def matching(
        self,
        *,
        hero: Optional[str] = None,
        item: Optional[str] = None,
        signal_types: Optional[Iterable[str]] = None,
    ) -> list[GameChangeSignal]:
        allowed_types = None if signal_types is None else {signal_type for signal_type in signal_types}
        if allowed_types is not None:
            unknown = allowed_types - SIGNAL_TYPES
            if unknown:
                raise GameChangeSignalError(f"Unknown signal type filter: {', '.join(sorted(unknown))}")

        return [
            signal
            for signal in self.signals
            if (hero is None or signal.applies_to_hero(hero))
            and (item is None or signal.matches_item(item))
            and (allowed_types is None or signal.type in allowed_types)
        ]


def load_default_signals(*, repo_root: Optional[Path] = None) -> GameChangeSignals:
    path = (repo_root / "game_changes" / "signals.json") if repo_root is not None else DEFAULT_SIGNALS_PATH
    if not path.exists():
        return GameChangeSignals()
    return load_signals(path)


def load_signals(path: str | Path) -> GameChangeSignals:
    signal_path = Path(path)
    if not signal_path.exists():
        raise GameChangeSignalError(f"Game-change signal file does not exist: {signal_path}")
    if not signal_path.is_file():
        raise GameChangeSignalError(f"Game-change signal path is not a file: {signal_path}")

    try:
        raw = json.loads(signal_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise GameChangeSignalError(f"Malformed game-change signal JSON in {signal_path}: {exc}") from exc
    return parse_signals(raw, source=signal_path)


def parse_signals(raw: Any, *, source: str | Path = "<memory>") -> GameChangeSignals:
    if not isinstance(raw, dict):
        raise GameChangeSignalError(f"{source}: signal document must be a JSON object")

    unknown_top_level = set(raw) - {"schema_version", "signals"}
    if unknown_top_level:
        raise GameChangeSignalError(f"{source}: unknown top-level fields: {', '.join(sorted(unknown_top_level))}")

    schema_version = raw.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        raise GameChangeSignalError(f"{source}: unsupported schema_version {schema_version!r}")

    signals = raw.get("signals")
    if not isinstance(signals, list):
        raise GameChangeSignalError(f"{source}: signals must be a list")

    parsed = tuple(_parse_signal(record, source=source, index=index) for index, record in enumerate(signals))
    return GameChangeSignals(parsed)


def _parse_signal(record: Any, *, source: str | Path, index: int) -> GameChangeSignal:
    prefix = f"{source}: signals[{index}]"
    if not isinstance(record, dict):
        raise GameChangeSignalError(f"{prefix} must be an object")

    unknown = set(record) - SIGNAL_FIELDS
    if unknown:
        raise GameChangeSignalError(f"{prefix} has unknown fields: {', '.join(sorted(unknown))}")

    missing = REQUIRED_SIGNAL_FIELDS - set(record)
    if missing:
        raise GameChangeSignalError(f"{prefix} missing required fields: {', '.join(sorted(missing))}")

    signal_type = _required_str(record, "type", prefix)
    if signal_type not in SIGNAL_TYPES:
        raise GameChangeSignalError(f"{prefix} has unsupported type: {signal_type}")

    metadata = record.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise GameChangeSignalError(f"{prefix}.metadata must be an object when present")

    return GameChangeSignal(
        id=_required_str(record, "id", prefix),
        type=signal_type,
        item=_required_str(record, "item", prefix),
        effective_date=_required_date(record, "effective_date", prefix),
        source_url=_required_str(record, "source_url", prefix),
        note=_required_str(record, "note", prefix),
        hero=_optional_str(record, "hero", prefix),
        replacement_item=_optional_str(record, "replacement_item", prefix),
        patch=_optional_str(record, "patch", prefix),
        metadata=dict(metadata) if metadata is not None else None,
    )


def _required_str(record: dict[str, Any], field: str, prefix: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value:
        raise GameChangeSignalError(f"{prefix}.{field} must be a non-empty string")
    return value


def _optional_str(record: dict[str, Any], field: str, prefix: str) -> Optional[str]:
    value = record.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise GameChangeSignalError(f"{prefix}.{field} must be a non-empty string when present")
    return value


def _required_date(record: dict[str, Any], field: str, prefix: str) -> date:
    value = _required_str(record, field, prefix)
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise GameChangeSignalError(f"{prefix}.{field} must be an ISO date") from exc
